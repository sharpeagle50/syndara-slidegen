"""Render a Manim Scene → MP4 bytes from inside the Syndara app.

Prefers the deployed Modal renderer (`syndara-manim-runner`, see manim_runner.py in this package)
so the heavy Manim/Cairo/Pango toolchain stays off the main application image; falls back to a local `manim`
CLI when Modal isn't configured (dev machines) or the remote lookup fails. Returns the mp4 bytes;
raises RuntimeError carrying the tail of manim's stderr on failure, so the ManimBuilder agent can
feed the error straight back to itself and self-correct the scene.

Narrated animations use manim-voiceover, but the narration audio is PRE-GENERATED in Syndara's own
(metered) TTS layer and shipped IN to the render as `voiceover=[(sentence, mp3_bytes), …]`. A tiny
custom SpeechService (injected as a preamble) returns that audio, so the render never calls OpenAI
and needs NO API key in the sandbox — and the TTS cost is metered like every other slide's.

Both an async (`render_manim`) and a sync (`render_manim_sync`) entrypoint are exposed — the agents
run synchronously (BaseAgent.call is sync), the runner is async.

Local dev: point MANIM_BIN at a venv's `manim` (manim 0.20+ needs Python >= 3.11); otherwise
`manim` on PATH is used.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_QUALITY_FLAG = {"l": "-ql", "m": "-qm", "h": "-qh"}

# The local manim CLI executes LLM-generated scene code. Strip credential-bearing env vars
# so that code can't read the app's secrets — by DENYLIST, not allowlist, so manim's
# Cairo/Pango/ffmpeg toolchain keeps the system env it needs to render correctly.
_SECRET_ENV_MARKERS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
    "DATABASE_URL", "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY", "JWT",
)


def _secret_scrubbed_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not any(m in k.upper() for m in _SECRET_ENV_MARKERS)}


# Preamble prepended to a narrated scene. Defines a SpeechService that returns the PRE-GENERATED
# audio Syndara made (keyed by the exact sentence, via the _SCRIPT list + a segment dir, both passed
# by env). transcription_model defaults to None → manim-voiceover never invokes Whisper. This is the
# whole reason no OpenAI key (or network) is needed in the render sandbox.
VOICEOVER_PREAMBLE = '''import os as _os, json as _json, shutil as _sh, hashlib as _hl
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


def _prepare_voiceover(scene_code: str, voiceover, workdir: str) -> tuple[str, dict]:
    """Write the pre-generated segment audio into workdir and prepend the SpeechService preamble.
    voiceover is [(sentence, mp3_bytes|base64), …]. Returns (code_with_preamble, env_additions)."""
    vo_dir = Path(workdir) / "syndara_vo"
    vo_dir.mkdir(parents=True, exist_ok=True)
    texts: list[str] = []
    for i, (text, audio) in enumerate(voiceover):
        data = audio if isinstance(audio, (bytes, bytearray)) else base64.b64decode(audio)
        (vo_dir / f"seg_{i:03d}.mp3").write_bytes(data)
        texts.append(text)
    env = {"SYNDARA_VO_DIR": str(vo_dir), "SYNDARA_VO_SCRIPT": json.dumps(texts)}
    return VOICEOVER_PREAMBLE + "\n\n" + scene_code, env


def _render_cli(scene_code: str, scene_name: str, quality: str, voiceover=None) -> bytes:
    """Render with a local `manim` binary. Used for dev and as the Modal fallback."""
    manim_bin = os.environ.get("MANIM_BIN", "manim")
    flag = _QUALITY_FLAG.get(quality, "-ql")
    env = _secret_scrubbed_env()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        if voiceover:
            scene_code, extra_env = _prepare_voiceover(scene_code, voiceover, str(d))
            env.update(extra_env)
        scene_file = d / "scene.py"
        scene_file.write_text(scene_code)
        media = d / "media"
        proc = subprocess.run(
            # --disable_caching: required by manim-voiceover (a caching bug corrupts VoiceoverScene
            # renders otherwise) and harmless for plain scenes (each render uses a fresh media_dir).
            [manim_bin, "render", flag, "--disable_caching", "--format", "mp4",
             "--media_dir", str(media), str(scene_file), scene_name],
            capture_output=True, text=True, cwd=str(d),
            env=env,
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


def _render_modal(scene_code: str, scene_name: str, quality: str, voiceover=None) -> bytes:
    """Render on the deployed Modal app. Looks the function up by name (no import coupling)."""
    import modal

    # from_name is the current lookup for deployed functions (Modal removed the eager .lookup()).
    fn = modal.Function.from_name("syndara-manim-runner", "render_scene")
    if voiceover:
        # Ship the pre-generated audio (base64) so the render bakes it in — no key, no network TTS.
        manifest = [[t, base64.b64encode(a if isinstance(a, (bytes, bytearray)) else base64.b64decode(a)).decode()]
                    for t, a in voiceover]
        return fn.remote(scene_code, scene_name, quality, manifest)
    # 3-arg call for silent/concept scenes — also works on a pre-voiceover deployment.
    return fn.remote(scene_code, scene_name, quality)


def _dispatch(scene_code: str, scene_name: str, quality: str, prefer_modal: bool | None,
              voiceover=None) -> bytes:
    """Modal-or-local dispatch, synchronous. Modal failures degrade to local so a dev box without
    Modal (or a transient Modal error) still renders."""
    if prefer_modal is None:
        prefer_modal = bool(os.environ.get("MODAL_TOKEN_ID"))
    if prefer_modal:
        try:
            return _render_modal(scene_code, scene_name, quality, voiceover)
        except Exception as e:
            # Only degrade to local manim if a binary actually exists (dev boxes). In the deployed
            # container there is no local manim, so re-raise the Modal error as a RuntimeError the
            # builder can retry — never a confusing 'manim not found' that crashes the whole beat.
            if not shutil.which(os.environ.get("MANIM_BIN", "manim")):
                raise RuntimeError(f"Modal render failed and no local manim available: {e}") from e
            print(f"[manim_render] Modal render failed ({type(e).__name__}: {e}); "
                  "falling back to local manim", flush=True)
    return _render_cli(scene_code, scene_name, quality, voiceover)


def render_manim_sync(scene_code: str, scene_name: str, quality: str = "l",
                      prefer_modal: bool | None = None, voiceover=None) -> bytes:
    """Synchronous render — for the (sync) agents. `voiceover` (list of (sentence, mp3_bytes)) bakes
    Syndara-generated narration in via the custom SpeechService. See render_manim."""
    return _dispatch(scene_code, scene_name, quality, prefer_modal, voiceover)


async def render_manim(scene_code: str, scene_name: str, quality: str = "l",
                       prefer_modal: bool | None = None, voiceover=None) -> bytes:
    """Render one Manim Scene to MP4 bytes. quality: 'l' preview, 'm', 'h' final.

    Chooses Modal when MODAL_TOKEN_ID is set (unless prefer_modal is forced), else local `manim`.
    Both paths raise RuntimeError with the render stderr on failure.
    """
    return await asyncio.to_thread(_dispatch, scene_code, scene_name, quality, prefer_modal, voiceover)
