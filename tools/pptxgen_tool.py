"""PptxGenJS tool — Python wrapper for the Node.js PptxGenJS runner.

Spawns a persistent Node subprocess that keeps the PptxGenJS presentation
object alive between calls, allowing incremental slide building (addSlide
per call) before a single writeFile at the end.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Optional

# Optional V8 heap ceiling for the long-lived Node worker, via
# SYNDARA_NODE_MAX_OLD_SPACE_MB. DEFAULT: unset → don't pass the flag, let Node
# auto-size (which respects container/cgroup memory limits). Measured worker
# usage for a full 26-slide deck is ~80MB, far below Node's default heap, so a
# higher ceiling buys nothing in practice — and setting it ABOVE a container's
# memory limit would invite kernel OOM kills. The env var is only an escape
# hatch for a pathologically large deck on a generous host.
def _node_max_old_space_mb() -> Optional[int]:
    raw = os.environ.get("SYNDARA_NODE_MAX_OLD_SPACE_MB")
    if not raw:
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val >= 512 else None


class PptxGenSession:
    """Manages a long-lived Node.js subprocess running pptxgen_runner.js."""

    _stdin: IO[str]
    _stdout: IO[str]
    _stderr: IO[str]

    def __init__(self, pptx_path: str, style: dict):
        self.pptx_path = str(Path(pptx_path).resolve())
        runner_path = str(Path(__file__).parent / "pptxgen_runner.js")
        node_modules = str(Path(__file__).parent / "node_modules")

        _heap_mb = _node_max_old_space_mb()
        node_argv = ["node"]
        if _heap_mb:
            node_argv.append(f"--max-old-space-size={_heap_mb}")
        node_argv.append(runner_path)
        self.proc = subprocess.Popen(
            node_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "NODE_PATH": node_modules},
        )
        self._stdin = self.proc.stdin  # type: ignore[assignment]
        self._stdout = self.proc.stdout  # type: ignore[assignment]
        self._stderr = self.proc.stderr  # type: ignore[assignment]
        # Continuously drain stderr in a daemon thread. Without this, the OS
        # pipe buffer (~64KB) fills on a chatty/long build and Node blocks on
        # its next stderr write (deadlock); and on a crash we lose the reason
        # because stderr was only ever read post-mortem. Keep a bounded tail.
        self._stderr_lines: deque[str] = deque(maxlen=400)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
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
        """Render a react-icons icon to PNG. Returns {success, base64, path, size}.

        The sharp/react render step occasionally fails on a perfectly valid icon (a
        transient runtime hiccup), which otherwise costs the builder a whole turn to
        notice and retry — the visible symptom is a slide falling back to a numbered
        circle. Retry that ONE failure mode a couple of times with a short backoff.
        Deterministic failures (bad name/pack, missing deps) carry a different error
        and return immediately, so a real problem is never masked."""
        msg = {
            "cmd": "render_icon",
            "icon_name": icon_name,
            "icon_pack": icon_pack,
            "out_path": out_path,
            "color": color,
            "size": size,
        }
        result = self._send(msg)
        for attempt in range(2):   # up to two extra tries after the first
            if result.get("success"):
                return result
            if "sharp" not in (result.get("error") or "").lower():
                return result   # not the transient render failure — don't retry
            time.sleep(0.3 * (attempt + 1))
            result = self._send(msg)
        return result

    def find_icon(self, query: str, limit: int = 40) -> dict:
        """Search the installed react-icons names for a concept (e.g. 'handshake').
        Returns {success, matches: [{name, pack}]} — real names make_icon will render,
        so the builder never has to guess a name that may not exist."""
        return self._send({"cmd": "find_icon", "query": query, "limit": limit})

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

    def _drain_stderr(self) -> None:
        """Read the worker's stderr line by line until EOF (process exit),
        keeping a bounded tail. Runs in a daemon thread."""
        try:
            for line in self._stderr:
                self._stderr_lines.append(line.rstrip("\n"))
        except Exception:
            pass

    def _stderr_tail(self, wait: bool = False) -> str:
        """Return the most recent stderr lines. If wait=True (the process is
        expected to have died), give the drain thread a moment to flush the
        final output before reading."""
        if wait:
            # Once the process exits, the stderr pipe hits EOF and the drain
            # thread finishes — this returns promptly with the death reason.
            self._stderr_thread.join(timeout=1.0)
            exit_code = self.proc.poll()
            tail = "\n".join(list(self._stderr_lines))
            if not tail and exit_code is not None and exit_code < 0:
                # Killed by a signal with no output — almost always the OOM
                # killer (SIGKILL=-9). Make that legible instead of "empty".
                return f"(no stderr; worker killed by signal {-exit_code} — likely out of memory)"
            return tail
        return "\n".join(list(self._stderr_lines))

    def _send(self, msg: dict) -> dict:
        try:
            self._stdin.write(json.dumps(msg) + "\n")
            self._stdin.flush()
        except (BrokenPipeError, OSError) as e:
            return {"success": False, "error": f"Pipe error: {e}. stderr: {self._stderr_tail(wait=True)[:1000]}"}

        line = self._stdout.readline()
        if not line:
            return {"success": False, "error": f"Node process died: {self._stderr_tail(wait=True)[:1000]}"}
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
