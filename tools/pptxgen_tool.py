"""PptxGenJS tool — Python wrapper for the Node.js PptxGenJS runner.

Spawns a persistent Node subprocess that keeps the PptxGenJS presentation
object alive between calls, allowing incremental slide building (addSlide
per call) before a single writeFile at the end.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import IO, Optional


class PptxGenSession:
    """Manages a long-lived Node.js subprocess running pptxgen_runner.js."""

    _stdin: IO[str]
    _stdout: IO[str]
    _stderr: IO[str]

    def __init__(self, pptx_path: str, style: dict):
        self.pptx_path = str(Path(pptx_path).resolve())
        runner_path = str(Path(__file__).parent / "pptxgen_runner.js")
        node_modules = str(Path(__file__).parent / "node_modules")

        self.proc = subprocess.Popen(
            ["node", runner_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "NODE_PATH": node_modules},
        )
        self._stdin = self.proc.stdin  # type: ignore[assignment]
        self._stdout = self.proc.stdout  # type: ignore[assignment]
        self._stderr = self.proc.stderr  # type: ignore[assignment]
        # Send init command
        init_result = self._send({
            "cmd": "init",
            "pptx_path": self.pptx_path,
            "style": style,
        })
        if not init_result.get("success"):
            raise RuntimeError(
                f"PptxGenSession init failed: {init_result.get('error')}"
            )

    def run_code(self, code: str) -> dict:
        """Execute PptxGenJS code. Returns {success, slide_count} or {success, error}."""
        return self._send({"cmd": "run", "code": code})

    def render_icon(self, icon_name: str, icon_pack: str, out_path: str,
                    color: str = "000000", size: int = 256) -> dict:
        """Render a react-icons icon to PNG. Returns {success, base64, path, size}."""
        return self._send({
            "cmd": "render_icon",
            "icon_name": icon_name,
            "icon_pack": icon_pack,
            "out_path": out_path,
            "color": color,
            "size": size,
        })

    def snapshot(self, snap_path: str) -> dict:
        """Write a temporary copy of the PPTX for rendering without ending the session."""
        return self._send({"cmd": "snapshot", "snap_path": snap_path})

    def save(self) -> dict:
        """Save the PPTX and terminate the subprocess."""
        result = self._send({"cmd": "save"})
        try:
            self._stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        return result

    def _send(self, msg: dict) -> dict:
        try:
            self._stdin.write(json.dumps(msg) + "\n")
            self._stdin.flush()
        except (BrokenPipeError, OSError) as e:
            stderr = ""
            try:
                stderr = self._stderr.read()
            except Exception:
                pass
            return {"success": False, "error": f"Pipe error: {e}. stderr: {stderr[:1000]}"}

        line = self._stdout.readline()
        if not line:
            stderr = ""
            try:
                stderr = self._stderr.read()
            except Exception:
                pass
            return {"success": False, "error": f"Node process died: {stderr[:1000]}"}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"success": False, "error": f"Invalid JSON from runner: {line[:500]}"}


def run_pptxgen_code(
    code: str,
    pptx_path: str,
    session: Optional[PptxGenSession] = None,
) -> dict:
    """Stateless wrapper — creates a session, runs code, saves.
    For multi-call usage, use PptxGenSession directly."""
    if session:
        return session.run_code(code)
    # Fallback one-shot mode
    s = PptxGenSession(pptx_path, {})
    try:
        result = s.run_code(code)
        if result.get("success"):
            s.save()
        return result
    except Exception:
        s.save()
        raise
