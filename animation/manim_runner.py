"""Modal renderer for Manim animation scenes (Concept Animation feature — Phase 0 spike).

A STANDALONE Modal app, deployed once, that the application calls BY NAME to render a Manim
Scene → MP4 without carrying the Manim/Cairo/Pango toolchain (and, if math mode is ever enabled,
a multi-GB LaTeX install) in the main application image. This file is never imported by the
running app — you deploy it, and manim_render.py (same package) looks the function up by name.

ACTIVATION (do this once, when you want Manim rendering off-box):
  1. pip install modal            (included in the `.[animation]` extra)
  2. modal token new              (creates ~/.modal.toml; copy the token id/secret into the app env
                                   vars MODAL_TOKEN_ID and MODAL_TOKEN_SECRET)
  3. modal deploy production/manim_runner.py
  4. In the Modal dashboard, set a workspace spending limit (your hard monthly cap).

SCOPE (Phase 0): Pango text only — NO LaTeX. Manim's Text/MarkupText render via Pango, which is in
the image below; MathTex/Tex need a TeX install. To enable "math mode" later, add
`texlive texlive-latex-extra texlive-fonts-extra` to .apt_install() and expect a much larger image
and slower cold starts.

COST/CAPS:
  - Billed per-second only while running — a short Pango render is typically a few cents (CPU only;
    no GPU needed).
  - Timeout: _RENDER_TIMEOUT per scene. That plus the dashboard spending limit are the caps.
"""
import subprocess
import tempfile
from pathlib import Path

import modal

app = modal.App("syndara-manim-runner")

# Debian + the native libs Manim needs to rasterize (Cairo) and lay out text (Pango), plus ffmpeg to
# encode the mp4. Pin manim so a rebuild can't silently pull a breaking release.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "build-essential", "pkg-config", "python3-dev",
        "libcairo2-dev", "libpango1.0-dev",
        "ffmpeg",
        # sox: manim-voiceover only shells out to it when global_speed != 1.0 (we never change
        # speed, so it's unused today) — but it probes for `sox` at import and logs a scary
        # "sox: not found" otherwise. Cheap to include; also guards any future speed change.
        "sox",
    )
    # manim-voiceover bakes SYNCED narration into a VoiceoverScene via OpenAI TTS (see the
    # ManimBuilder). [openai] pulls the OpenAI client + pydub (ffmpeg already present).
    # Pre-install every dep our render path touches. manim-voiceover otherwise "installs missing
    # deps on the fly, asking permission" — which HANGS in a headless container. We use the base
    # SpeechService + VoiceoverScene + pydub (audio) + slugify (cache names); NO transcription
    # (transcription_model stays None) so whisper is never needed.
    #
    # setuptools: manim-voiceover 0.3.7 does `import pkg_resources` at module load, and manim loads
    # it as a plugin at STARTUP — so on Python 3.12 + debian_slim (neither ships setuptools anymore)
    # every `manim` invocation crashes with ModuleNotFoundError: No module named 'pkg_resources'
    # BEFORE rendering a single frame. Pinned <81 to stay ahead of setuptools removing the
    # long-deprecated pkg_resources shim.
    .pip_install("manim==0.20.1", "manim-voiceover[openai]==0.3.7", "pydub", "python-slugify",
                 "setuptools<81")
)

_RENDER_TIMEOUT = 600  # per-scene wall-clock cap (10 min)
_QUALITY_FLAG = {"l": "-ql", "m": "-qm", "h": "-qh"}

# A SpeechService that returns Syndara's PRE-GENERATED narration audio (shipped in by the caller),
# so a narrated render bakes audio in WITHOUT calling any TTS API or holding any key in the sandbox.
# transcription_model defaults to None → manim-voiceover never invokes Whisper. (Kept in lockstep
# with production/manim_render.VOICEOVER_PREAMBLE — this file is standalone/Modal-deployed.)
_VOICEOVER_PREAMBLE = '''import os as _os, json as _json, shutil as _sh, hashlib as _hl
from manim_voiceover.services.base import SpeechService as _SpeechService

_SCRIPT = _json.loads(_os.environ.get("SYNDARA_VO_SCRIPT", "[]"))
_VO_DIR = _os.environ.get("SYNDARA_VO_DIR", "")


class _SyndaraVO(_SpeechService):
    """Returns Syndara's pre-generated narration audio; never calls any TTS API."""
    def generate_from_text(self, text, cache_dir=None, path=None, **kwargs):
        cd = str(cache_dir) if cache_dir else str(self.cache_dir)
        _os.makedirs(cd, exist_ok=True)
        name = "syndara_" + _hl.md5(text.encode("utf-8")).hexdigest() + ".mp3"
        i = _SCRIPT.index(text) if text in _SCRIPT else -1
        src = _os.path.join(_VO_DIR, "seg_%03d.mp3" % i) if i >= 0 else ""
        dst = _os.path.join(cd, name)
        if src and _os.path.exists(src):
            _sh.copy(src, dst)
        else:                                   # graceful: a short silence for an unmatched line
            from pydub import AudioSegment as _AS
            _AS.silent(duration=1200).export(dst, format="mp3")
        # word_boundaries=[] is present (not missing) so the wrapper never triggers Whisper and no
        # downstream lookup can KeyError; empty is fine since we use tracker.duration, not bookmarks.
        return {"input_text": text, "original_audio": name, "word_boundaries": []}


def _syndara_service():
    return _SyndaraVO()
'''


@app.function(timeout=_RENDER_TIMEOUT, image=image)
def render_scene(scene_code: str, scene_name: str, quality: str = "l", voiceover=None) -> bytes:
    """Render one Manim Scene to MP4 and return the raw bytes.

    scene_code : a complete Python module defining a `class {scene_name}(Scene)`.
    quality    : 'l' (480p15, fast preview), 'm' (720p30), or 'h' (1080p60, final).
    voiceover  : for a narrated VoiceoverScene, [[sentence, base64_mp3], …] — Syndara's PRE-GENERATED
                 narration. Written to disk and returned by the injected _SyndaraVO service, so the
                 render bakes audio in with NO OpenAI key and NO network TTS in the sandbox.

    Raises RuntimeError with the tail of manim's stderr on failure — the build agent feeds that
    straight back to itself to self-correct the scene, so keep the message intact.
    """
    return _render(scene_code, scene_name, quality, voiceover)


def _render(scene_code: str, scene_name: str, quality: str, voiceover=None) -> bytes:
    import base64
    flag = _QUALITY_FLAG.get(quality, "-ql")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        env = None
        if voiceover:
            vo_dir = d / "syndara_vo"
            vo_dir.mkdir()
            texts = []
            for i, (text, audio_b64) in enumerate(voiceover):
                (vo_dir / f"seg_{i:03d}.mp3").write_bytes(base64.b64decode(audio_b64))
                texts.append(text)
            import json as _json, os as _os
            env = dict(_os.environ)
            env["SYNDARA_VO_DIR"] = str(vo_dir)
            env["SYNDARA_VO_SCRIPT"] = _json.dumps(texts)
            scene_code = _VOICEOVER_PREAMBLE + "\n\n" + scene_code
        scene_file = d / "scene.py"
        scene_file.write_text(scene_code)
        media = d / "media"
        proc = subprocess.run(
            # --disable_caching: required by manim-voiceover (a caching bug corrupts VoiceoverScene
            # renders otherwise) and harmless for plain scenes (each render uses a fresh media_dir).
            ["manim", "render", flag, "--disable_caching", "--format", "mp4",
             "--media_dir", str(media), str(scene_file), scene_name],
            capture_output=True, text=True, cwd=str(d), env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"manim render failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}")
        # Skip partial_movie_files/ (per-animation segments manim concatenates); the final combined
        # scene mp4 sits directly in the quality dir. Take the largest survivor as a safety net.
        mp4s = [p for p in media.rglob("*.mp4") if "partial_movie_files" not in p.parts]
        if not mp4s:
            raise RuntimeError(
                "manim exited 0 but produced no final mp4:\n"
                f"{proc.stdout[-1000:]}\n{proc.stderr[-1000:]}")
        return max(mp4s, key=lambda p: p.stat().st_size).read_bytes()
