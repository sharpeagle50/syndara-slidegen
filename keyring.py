"""Per-run API-key override for generation.

Lets a single build run against a caller-supplied Anthropic / OpenAI key instead of the process
ANTHROPIC_API_KEY / OPENAI_API_KEY, without affecting other concurrent builds. The override lives
in ContextVars, which are isolated per asyncio task and propagate through `await` and
`asyncio.to_thread` — so the sync agents (planner, Claude Code builder, VisualQA) run under the
same override as the async clients. Falls back to the process env keys when no override is set.

No secrets live in this file — callers set the keys at runtime (e.g. a build runner that decrypts
the owner's saved key for that course).
"""
from __future__ import annotations

import contextvars
import os
from typing import Optional

_anthropic_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_anthropic_key", default=None)
_openai_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_openai_key", default=None)


def set_keys(*, anthropic: Optional[str] = None, openai: Optional[str] = None) -> None:
    """Set per-run key overrides for the current context. Call once at the top of a build task.
    Passing a falsy value leaves the corresponding env key in effect for that provider."""
    _anthropic_key.set(anthropic or None)
    _openai_key.set(openai or None)


def anthropic_key() -> Optional[str]:
    """The Anthropic key for the current run: the override if set, else ANTHROPIC_API_KEY."""
    return _anthropic_key.get() or os.environ.get("ANTHROPIC_API_KEY")


def anthropic_override() -> Optional[str]:
    """The per-run Anthropic override if one is set (else None), ignoring the env fallback.
    Used where the process env is forwarded separately, e.g. the Claude Code subprocess."""
    return _anthropic_key.get()


def openai_key() -> Optional[str]:
    """The OpenAI key for the current run: the override if set, else OPENAI_API_KEY."""
    return _openai_key.get() or os.environ.get("OPENAI_API_KEY")


def async_anthropic(**kwargs):
    """anthropic.AsyncAnthropic bound to the current run's key."""
    import anthropic
    return anthropic.AsyncAnthropic(api_key=anthropic_key(), **kwargs)


def sync_anthropic(**kwargs):
    """anthropic.Anthropic bound to the current run's key."""
    import anthropic
    return anthropic.Anthropic(api_key=anthropic_key(), **kwargs)
