"""ManimBuilder Agent: turns one storyboard beat into a rendered Manim animation (Concept
Animation feature, Phase 1, stage 2).

The loop mirrors the slide builder's generate → render → view → self-correct pattern
(deckgen/agents/claude_code_builder.py), tightened for a single scene: write a Manim Scene, render a
fast preview, LOOK at keyframes, fix runtime errors and layout problems, then render the final.
Self-correction is the point — Manim code fails often, so the loop is what makes output reliable
rather than a demo.

Lives in the private layer (not deckgen, which is the public slidegen subtree) because it renders
through production.manim_render. Extends BaseAgent so token usage auto-meters to the open cost sink.
"""
from __future__ import annotations

import ast
import base64
import re
import subprocess
import tempfile
import time
from pathlib import Path

from ..agents.base import BaseAgent, report_render_usage
from .manim_render import render_manim_sync

SCENE_NAME = "Beat"


def _timed_render(code: str, quality: str, voiceover=None) -> bytes:
    """Render, metering the render wall-clock (Modal CPU) onto the open cost sink. Isolating the
    timing here keeps LLM latency out of the render-compute cost line. `voiceover` (pre-generated
    narration segments) is passed through to bake synced audio in."""
    t0 = time.perf_counter()
    try:
        return render_manim_sync(code, SCENE_NAME, quality, voiceover=voiceover)
    finally:
        report_render_usage(time.perf_counter() - t0)

_SYSTEM = """You write a single Manim Community (v0.20) scene that animates ONE beat of an explainer
video, in the clean, geometric 3Blue1Brown style. The base class and the narration contract are
given in the task message — follow them exactly.

CONSTRAINTS (always):
- The class is named exactly `Beat`, with a `construct(self)` method. No other top-level scene class.
- Pango text ONLY: `Text(...)` / `MarkupText(...)`. NEVER `Tex`, `MathTex`, or anything needing
  LaTeX — it is not installed and will crash the render.
- Self-contained: no image/SVG/font/data files, no `ImageMobject`, no network calls of your own.
- Stay in frame. The frame spans roughly x ∈ [-7, 7], y ∈ [-4, 4]. Keep every object inside it —
  use `.scale()`, `.to_edge()`, `.arrange()`, `.next_to()`, and `VGroup` layout so nothing clips
  or overlaps. Titles go near the top; don't stack text on top of a graphic.
- Use the colors given in the task for emphasis via `color=`. If the task includes a style
  palette, set the camera background and give every element an explicit color from it; with no
  palette, the background stays default.
- End with a brief `self.wait(0.4)`.

OUTPUT: only the Python code, in a single ```python code fence. No explanation."""

# Per-call contract for a SILENT scene (no baked audio).
_SILENT_CONTRACT = (
    "Base class: define `class Beat(Scene):`. Start the file with `from manim import *`. Animate "
    "smoothly with `self.play(...)`, aiming for about {dur} seconds total.")

# Per-call contract for a NARRATED scene. Syndara PRE-GENERATES the narration audio and injects a
# `_syndara_service()` + a `_SCRIPT` list (the segments) above the scene, so the builder never calls
# a TTS API — it just voices _SCRIPT[i] and times each beat to tracker.duration.
_NARRATED_CONTRACT = (
    "Base class: define `class Beat(VoiceoverScene):`. Start the file with EXACTLY:\n"
    "    from manim import *\n"
    "    from manim_voiceover import VoiceoverScene\n"
    "A helper `_syndara_service()` and a list `_SCRIPT` (the narration segments) are ALREADY defined "
    "ABOVE your code — do NOT redefine or import them, and do NOT import any TTS/speech service.\n"
    "As the FIRST statement in construct, call: self.set_speech_service(_syndara_service())\n"
    "Then narrate the {n} segments IN ORDER, ONE voiceover block each, timing that beat's animation "
    "to its segment:\n"
    "    with self.voiceover(text=_SCRIPT[0]) as tracker:\n"
    "        self.play(<the animation for segment 0>, run_time=tracker.duration)\n"
    "    with self.voiceover(text=_SCRIPT[1]) as tracker:\n"
    "        self.play(<the animation for segment 1>, run_time=tracker.duration)\n"
    "    ... continue through _SCRIPT[{last}].\n"
    "Reference each line as `_SCRIPT[i]` EXACTLY (never inline or retype the text). The narration "
    "audio is baked in, so do NOT render the script as on-screen text.\n\n"
    "The {n} segments, for you to animate appropriately (index: text):\n{segments}")


def _extract_code(text: str) -> str | None:
    """Pull the Python scene from the reply. Try each ```python fence (then the raw reply as a
    bare-code fallback), strip any stray fence lines, and REQUIRE the result to both define the scene
    AND parse. A reply that mixes the model's prose with code, or omits/breaks the fence, is rejected
    here and retried — never shipped to the renderer as broken source (which fails cryptically far
    downstream on Modal)."""
    candidates = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    candidates.append(text)   # bare-code fallback: model returned code with no usable fence
    # Salvage a reply that put commentary BEFORE bare code: retry from the first code-like line.
    lead = re.search(r"(?m)^(?:from |import |class |def |@)", text)
    if lead:
        candidates.append(text[lead.start():])
    for cand in candidates:
        code = "\n".join(
            ln for ln in cand.strip().splitlines() if not ln.lstrip().startswith("```")
        ).strip()
        if "class Beat" not in code or "def construct" not in code:
            continue
        try:
            ast.parse(code)
        except SyntaxError:
            continue
        return code
    return None


def _extract_frames(mp4: bytes, n: int = 3) -> list[bytes]:
    """Grab up to n evenly-spaced PNG frames from a clip (for the visual-QA pass). Best-effort."""
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        clip = d / "clip.mp4"
        clip.write_bytes(mp4)
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
                capture_output=True, text=True, timeout=30)  # timeout: a malformed clip could hang the thread forever
            dur = float((out.stdout or "0").strip())
        except (ValueError, OSError, subprocess.TimeoutExpired):
            dur = 0.0
        # Sample across the clip (skip the empty first instant).
        fracs = [0.5] if dur <= 0 else [0.2, 0.55, 0.9][:n]
        for i, f in enumerate(fracs):
            t = max(0.1, dur * f)
            png = d / f"f{i}.png"
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(clip),
                     "-frames:v", "1", str(png)], capture_output=True, timeout=30)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0 and png.exists():
                frames.append(png.read_bytes())
    return frames


def _image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


def _with_preamble(code: str, preamble: str | None) -> str:
    """Inject the shared motif library into a scene's source before rendering. The model never
    writes the library itself (it's told the constructors already exist), so the injection is what
    makes them real. Placed right after `from manim import *` so voiceover-mode injection (which
    wraps the whole source) still sits above everything."""
    if not preamble:
        return code
    marker = "from manim import *"
    block = f"\n\n# ── SHARED MOTIF LIBRARY (director-validated) ──\n{preamble}\n"
    if marker in code:
        return code.replace(marker, marker + block, 1)
    return "from manim import *" + block + "\n" + code


class ManimBuilderAgent(BaseAgent):
    allowed_tool_names: list[str] = []
    system_prompt = _SYSTEM

    def build_beat(self, beat: dict, accent: str = "#4361EE", *,
                   voiceover: list | None = None, palette: dict | None = None,
                   preamble: str | None = None,
                   quality_preview: str = "l", quality_final: str = "m",
                   max_attempts: int = 4, visual_qa: bool = True) -> tuple[bytes, str]:
        """Generate, render, self-correct, and return (final_mp4_bytes, scene_code) for one beat.

        voiceover=[(sentence, mp3_bytes), …] builds a VoiceoverScene that bakes those PRE-GENERATED
        (Syndara-metered) narration segments in, each beat timed to its segment via tracker.duration;
        None builds a silent scene. Concurrency-safe: the mode lives in the per-call user message,
        never on shared instance state.

        Raises RuntimeError if no attempt yields a rendering scene (the runner degrades that beat).
        """
        visual = beat.get("visual_direction", "")
        dur = beat.get("duration_hint", 6)
        do_narrate = bool(voiceover)
        if do_narrate:
            segs = [t for t, _ in voiceover]
            numbered = "\n".join(f"  {i}: {t}" for i, t in enumerate(segs))
            contract = _NARRATED_CONTRACT.format(n=len(segs), last=len(segs) - 1, segments=numbered)
        else:
            contract = _SILENT_CONTRACT.format(dur=dur)
        # Course-style theming: when a palette is given (slide animations + module explainer
        # videos), the scene adopts the deck's colors instead of Manim's white-on-black default —
        # background from the palette, and an EXPLICIT color on every element, since Manim's
        # default white text is invisible on light backgrounds.
        style_block = ""
        if palette:
            _bg = palette.get("bg") or "#000000"
            style_block = (
                "Style palette (the course's visual theme — match it):\n"
                f"- FIRST line of construct(): `self.camera.background_color = \"{_bg}\"`.\n"
                f"- Default color for text and shapes: {palette.get('text') or '#FFFFFF'} — set "
                "`color=` explicitly on EVERY Text and mobject.\n"
                f"- Emphasis accent: {accent}; secondary accent: "
                f"{palette.get('accent2') or accent}; muted labels: "
                f"{palette.get('subtext') or '#6B7280'}.\n"
                f"- Every element must be clearly legible against {_bg}.\n\n"
            )
        # Full-video mode: the director's motif library is injected into the rendered source, so
        # the model must CALL the constructors, never redefine them — that's what keeps recurring
        # visuals identical across a 20-minute video.
        motif_block = ""
        if preamble:
            motif_block = (
                "SHARED MOTIF LIBRARY — these constructors are ALREADY defined above your code. "
                "Call them for any recurring element; do NOT redefine them or copy their bodies:\n"
                f"```python\n{preamble}\n```\n\n")
        messages: list[dict] = [{"role": "user", "content": (
            f"Beat visual direction:\n{visual}\n\n"
            + (f"Target duration: about {dur} seconds. " if not do_narrate else "")
            + f"Accent color: {accent}.\n\n{motif_block}{style_block}{contract}")}]

        last_good: tuple[bytes, str] | None = None
        for attempt in range(max_attempts):
            resp = self.call(messages=messages, max_tokens=4096, disable_thinking=True)
            reply = "".join(b.text for b in resp.content if b.type == "text")
            code = _extract_code(reply)
            messages.append({"role": "assistant", "content": reply})
            if not code:
                messages.append({"role": "user", "content":
                    "That wasn't a valid scene. Return ONLY a ```python fence defining "
                    f"`class {SCENE_NAME}` with a `construct` method, per the contract above."})
                continue

            try:
                preview = _timed_render(_with_preamble(code, preamble), quality_preview, voiceover)
            except RuntimeError as e:
                messages.append({"role": "user", "content":
                    f"The render failed with this error — fix it and return the FULL corrected "
                    f"scene:\n\n{str(e)[-1500:]}"})
                continue

            last_good = (preview, code)
            # Final attempt, or QA disabled: accept and stop refining.
            if not visual_qa or attempt == max_attempts - 1:
                break

            frames = _extract_frames(preview)
            if not frames:
                break
            qa_content: list = [{"type": "text", "text":
                "Here are frames from your rendered beat. Check: is anything off-frame, clipped, "
                "overlapping, or unreadable? Is it a faithful, clear visual for the direction? If it "
                "is good, reply with exactly RENDER_OK and nothing else. Otherwise return the FULL "
                "corrected scene in a ```python fence."}]
            qa_content += [_image_block(f) for f in frames]
            messages.append({"role": "user", "content": qa_content})
            verdict = self.call(messages=messages, max_tokens=4096, disable_thinking=True)
            vtext = "".join(b.text for b in verdict.content if b.type == "text")
            messages.append({"role": "assistant", "content": vtext})
            if "RENDER_OK" in vtext:
                break
            fixed = _extract_code(vtext)
            if fixed:
                try:
                    last_good = (_timed_render(_with_preamble(fixed, preamble), quality_preview, voiceover), fixed)
                except RuntimeError as e:
                    messages.append({"role": "user", "content":
                        f"That correction failed to render — fix and return the full scene:\n\n{str(e)[-1500:]}"})
                    continue
            # else: no code in the verdict, keep the last good preview and stop.
            break

        if last_good is None:
            raise RuntimeError("ManimBuilder produced no rendering scene after all attempts")

        _, final_code = last_good
        final_mp4 = _timed_render(_with_preamble(final_code, preamble), quality_final, voiceover)
        return final_mp4, final_code
