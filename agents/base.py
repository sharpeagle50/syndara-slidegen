"""BaseAgent: enforces tool permissions and wraps Claude API calls."""
from __future__ import annotations
import json
import os
import re
import socket
import time as _time
import threading
from pathlib import Path
from typing import Optional

import anthropic
import httpx
import contextvars


# ── Per-run cost/usage capture ───────────────────────────────────────────────
# A context-scoped sink the orchestrator (web_runner) opens around a generation
# run. Every API call reports its token usage here, and the agentic builder
# reports its exact total_cost_usd. asyncio.to_thread propagates the context
# into agent worker threads, and each concurrent run/task gets its own copy, so
# capture is both complete and per-run isolated. This module only COLLECTS usage
# (no pricing) — turning tokens into dollars lives in the private layer.
_cost_sink: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "syndara_cost_sink", default=None
)
# Tags each captured entry with the module being built (None = course-level work
# like the course planner / plan review). Set inside each module's coroutine, so
# concurrently-built modules each tag their own costs in isolation.
_cost_module: "contextvars.ContextVar[Optional[object]]" = contextvars.ContextVar(
    "syndara_cost_module", default=None
)


def cost_capture_begin() -> None:
    """Open a fresh cost-capture sink in the current context (one per run)."""
    _cost_sink.set([])


def cost_set_module(module) -> None:
    """Tag subsequent captured costs with a module id (call inside the module's
    coroutine). None = course-level / unattributed."""
    _cost_module.set(module)


def cost_capture_entries() -> list[dict]:
    """Return everything reported since cost_capture_begin() (empty if unopened)."""
    sink = _cost_sink.get()
    return list(sink) if sink is not None else []


def _report_usage(label: str, model: str, usage) -> None:
    sink = _cost_sink.get()
    if sink is None or usage is None:
        return
    sink.append({
        "kind": "usage",
        "stage": label or "",
        "module": _cost_module.get(),
        "model": model,
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "server_tool_use": {
            "web_search_requests": int(
                getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
            ),
        },
    })


def report_usage(label: str, model: str, usage) -> None:
    """Public: report token usage from a direct (non-_retry_api_call) Anthropic
    call. No-op when no run sink is open; never raises."""
    try:
        _report_usage(label, model, usage)
    except Exception:
        pass


def report_tts_usage(chars: int, model: str) -> None:
    """Report OpenAI TTS character usage (priced separately in the private
    layer). No-op when no run sink is open; never raises."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "tts", "stage": "tts", "module": _cost_module.get(),
                     "model": model, "chars": int(chars or 0)})
    except (TypeError, ValueError):
        pass


def report_exact_cost(label: str, usd) -> None:
    """Record an exact dollar cost (the agentic builder's total_cost_usd)."""
    sink = _cost_sink.get()
    if sink is None or usd is None:
        return
    try:
        sink.append({"kind": "exact", "stage": "builder", "module": _cost_module.get(),
                     "label": label, "usd": float(usd)})
    except (TypeError, ValueError):
        pass


# ── Punctuation style: kill the em-dash "AI tell" ────────────────────────────
# A system-prompt rule (appended to every content agent) asks the model to vary
# its punctuation rather than swap em dashes for hyphens; strip_em_dashes is the
# deterministic backstop for the stragglers the model still emits. It targets
# ONLY the Unicode dashes (em U+2014, en U+2013, horizontal bar U+2015) — never
# the ASCII hyphen — so URLs, file paths, and code are never altered.
STYLE_RULE = (
    "\n\nPUNCTUATION STYLE (required): Write with varied, natural punctuation. "
    "Never use em dashes. Do not lean on hyphens as a substitute either. Prefer "
    "commas, periods, colons, or rephrasing the sentence, mixed naturally so no "
    "single punctuation mark dominates."
)

_UNICODE_DASHES = "—–―"


def strip_em_dashes(text):
    """Replace Unicode dashes with varied, dash-free punctuation. Best-effort
    cleanup for the few the model emits despite STYLE_RULE; only touches em/en
    dashes (prose), so ASCII content (URLs, paths, code) is left intact."""
    if not isinstance(text, str) or not any(d in text for d in _UNICODE_DASHES):
        return text
    t = re.sub(r"(\d)\s*[—–―]\s*(\d)", r"\1-\2", text)   # 5–10 -> 5-10
    t = re.sub(r"\s*[—–―]\s+", ", ", t)                  # clause/parenthetical -> comma
    t = re.sub(r"\s+[—–―]\s*", ", ", t)
    for d in _UNICODE_DASHES:                                            # any remaining tight dash -> hyphen
        t = t.replace(d, "-")
    t = re.sub(r",\s*,", ", ", t)                                       # tidy artifacts
    t = re.sub(r",\s*([.;:!?])", r"\1", t)
    return t


def strip_em_dashes_deep(obj):
    """Apply strip_em_dashes to every string value in a nested dict/list
    (e.g. an exercise or assessment payload). Keys are left unchanged."""
    if isinstance(obj, str):
        return strip_em_dashes(obj)
    if isinstance(obj, list):
        return [strip_em_dashes_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: strip_em_dashes_deep(v) for k, v in obj.items()}
    return obj


def _keepalive_socket_options() -> list[tuple]:
    """TCP keepalive options so a long-idle connection (e.g. the ~4-5 min the
    planner waits on a single non-streaming call) isn't silently dropped by a
    router/NAT/Wi-Fi idle timeout. Probes keep the connection — and the NAT
    mapping — alive; if the network is genuinely dead, the probes fail in a few
    minutes so the call errors fast (and our retry reconnects) instead of
    hanging to the read timeout. Cross-platform: TCP_KEEPIDLE on Linux,
    TCP_KEEPALIVE on macOS."""
    opts: list[tuple] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(socket, "TCP_KEEPALIVE", None)  # macOS name
    if idle_opt is not None:
        opts.append((socket.IPPROTO_TCP, idle_opt, 60))   # first probe after 60s idle
    if hasattr(socket, "TCP_KEEPINTVL"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30))  # then every 30s
    if hasattr(socket, "TCP_KEEPCNT"):
        opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5))     # 5 misses → drop
    return opts


def _build_http_client() -> httpx.Client:
    """httpx client with TCP keepalive enabled on every connection."""
    return httpx.Client(
        timeout=httpx.Timeout(connect=15.0, read=1800.0, write=120.0, pool=120.0),
        transport=httpx.HTTPTransport(socket_options=_keepalive_socket_options()),
    )


# Track the MINIMUM remaining value we've seen across all concurrent calls so
# we can see how close we got to the rate-limit ceiling over the course of a
# run, not just the last call. Thread-safe because multiple builder threads
# can hit Anthropic concurrently.
_rl_mins_lock = threading.Lock()
_rl_mins: dict[str, dict] = {}
# Live observability (in-process, resets on restart): the latest rate-limit headroom
# snapshot per model, plus a rolling log of 429-hit timestamps, so an owner dashboard
# can show how close generation is running to the API ceiling while ramping up.
from collections import deque as _deque
_rl_latest: dict[str, dict] = {}
_rl_429_hits = _deque(maxlen=1000)  # rolling timestamps of recent 429 hits


def _log_ratelimit(headers, label: str, model: str) -> None:
    """Print remaining-capacity values from an Anthropic response's headers.
    Also track the worst-case (smallest-remaining) we've seen per model so a
    session-wide low-water mark is visible in the logs."""
    try:
        itpm_rem = headers.get("anthropic-ratelimit-input-tokens-remaining")
        otpm_rem = headers.get("anthropic-ratelimit-output-tokens-remaining")
        rpm_rem = headers.get("anthropic-ratelimit-requests-remaining")
        itpm_lim = headers.get("anthropic-ratelimit-input-tokens-limit")
        otpm_lim = headers.get("anthropic-ratelimit-output-tokens-limit")
        rpm_lim = headers.get("anthropic-ratelimit-requests-limit")
        if not any([itpm_rem, otpm_rem, rpm_rem]):
            return

        def _pct(rem, lim):
            try:
                r, l = int(rem), int(lim)
                return f"{round(r / max(l, 1) * 100)}%"
            except (TypeError, ValueError):
                return "?"

        def _as_int(x):
            try:
                return int(x)
            except (TypeError, ValueError):
                return None

        with _rl_mins_lock:
            slot = _rl_mins.setdefault(model, {"itpm": None, "otpm": None, "rpm": None})
            for k, v in (("itpm", itpm_rem), ("otpm", otpm_rem), ("rpm", rpm_rem)):
                iv = _as_int(v)
                if iv is not None and (slot[k] is None or iv < slot[k]):
                    slot[k] = iv
            low = slot.copy()
            _rl_latest[model] = {
                "itpm_rem": _as_int(itpm_rem), "itpm_lim": _as_int(itpm_lim),
                "otpm_rem": _as_int(otpm_rem), "otpm_lim": _as_int(otpm_lim),
                "rpm_rem": _as_int(rpm_rem), "rpm_lim": _as_int(rpm_lim),
                "at": _time.time(),
            }

        print(
            f"[Ratelimit {model}] ITPM_rem={itpm_rem}/{itpm_lim} ({_pct(itpm_rem, itpm_lim)}) "
            f"· OTPM_rem={otpm_rem}/{otpm_lim} ({_pct(otpm_rem, otpm_lim)}) "
            f"· RPM_rem={rpm_rem}/{rpm_lim} ({_pct(rpm_rem, rpm_lim)}) "
            f"· session_low ITPM={low['itpm']} OTPM={low['otpm']} RPM={low['rpm']} "
            f"· agent={label}",
            flush=True,
        )
    except Exception as e:
        # Never let logging break a real request.
        print(f"[Ratelimit] log error: {e}", flush=True)


def ratelimit_snapshot() -> dict:
    """Live, in-process rate-limit observability for the owner dashboard: the latest
    headroom (remaining/limit) per model plus counts of recent 429 hits. Best-effort
    and resets on restart. Returns plain JSON-able data."""
    now = _time.time()
    with _rl_mins_lock:
        models = {m: dict(v) for m, v in _rl_latest.items()}
        hits = list(_rl_429_hits)
    def _since(secs: int) -> int:
        return sum(1 for t in hits if now - t <= secs)
    return {
        "models": models,
        "hits_1m": _since(60),
        "hits_5m": _since(300),
        "hits_15m": _since(900),
        "last_429_at": hits[-1] if hits else None,
        "now": now,
    }


def extract_json(text: str) -> dict:
    """Robustly extract a JSON object from an LLM response.

    Handles:
      - bare JSON ("{...}")
      - fenced ```json blocks
      - JSON embedded in prose ("Here's the plan: {...}")
      - trailing prose after the JSON
      - empty/missing responses (raises with a useful message)
    """
    if not text or not text.strip():
        raise ValueError("LLM returned empty text")
    t = text.strip()
    # Strip fenced code block (```json ... ``` or ``` ... ```)
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    # Find the first { and the matching last } by depth counting
    start = t.find("{")
    if start < 0:
        raise ValueError(f"No JSON object found in LLM response (first 200 chars): {t[:200]!r}")
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i, ch in enumerate(t[start:], start=start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise ValueError(f"Unbalanced JSON in LLM response (first 200 chars): {t[:200]!r}")
    return json.loads(t[start:end])

SKILLS_DIR = Path(__file__).parent.parent / "skills"
MODEL_DEFAULT = os.environ.get("SYNDARA_MODEL", "claude-opus-4-8")


def load_skill(name: str) -> str:
    path = SKILLS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return ""


RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BASE_DELAY = 15  # seconds


def _retry_api_call(fn, *, label: str, model: str):
    """Call fn(), retrying on 429 / 529 / connection errors with exponential
    backoff. Returns the raw response."""
    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            raw = fn()
            _log_ratelimit(raw.headers, label, model)
            try:
                _report_usage(label, model, raw.parse().usage)
            except Exception:
                pass  # cost capture must never break the API call
            return raw
        except anthropic.RateLimitError as e:
            try:
                with _rl_mins_lock:
                    _rl_429_hits.append(_time.time())
            except Exception:
                pass
            retry_after = None
            if hasattr(e, "response") and e.response is not None:
                retry_after = e.response.headers.get("retry-after")
            delay = float(retry_after) if retry_after else RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
            delay = min(delay, 120)
            print(
                f"[RateLimit] 429 hit · agent={label} model={model} "
                f"attempt={attempt}/{RATE_LIMIT_MAX_RETRIES} · "
                f"retry-after={retry_after} · waiting {delay:.0f}s · "
                f"error={e}",
                flush=True,
            )
            if attempt == RATE_LIMIT_MAX_RETRIES:
                print(
                    f"[RateLimit] EXHAUSTED all {RATE_LIMIT_MAX_RETRIES} retries · "
                    f"agent={label} model={model} · raising",
                    flush=True,
                )
                raise
            _time.sleep(delay)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                delay = RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
                delay = min(delay, 120)
                print(
                    f"[RateLimit] 529 overloaded · agent={label} model={model} "
                    f"attempt={attempt}/{RATE_LIMIT_MAX_RETRIES} · "
                    f"waiting {delay:.0f}s · error={e}",
                    flush=True,
                )
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    print(
                        f"[RateLimit] EXHAUSTED all {RATE_LIMIT_MAX_RETRIES} retries · "
                        f"agent={label} model={model} · raising",
                        flush=True,
                    )
                    raise
                _time.sleep(delay)
            else:
                raise
        except anthropic.APIConnectionError as e:
            # Network/connection failure or read timeout (APITimeoutError is a
            # subclass, so it's covered here too). No response was received, so
            # retrying is safe — it can't abort a healthy in-flight call, and is
            # what the SDK already does internally. We surface it so a flaky
            # connection shows up in the logs instead of the agent hanging
            # silently. No timeout values are changed — logging + the same
            # backoff used for 529s.
            delay = RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
            delay = min(delay, 120)
            print(
                f"[APIConnection] connection error · agent={label} model={model} "
                f"attempt={attempt}/{RATE_LIMIT_MAX_RETRIES} · "
                f"waiting {delay:.0f}s · error={type(e).__name__}: {e}",
                flush=True,
            )
            if attempt == RATE_LIMIT_MAX_RETRIES:
                print(
                    f"[APIConnection] EXHAUSTED all {RATE_LIMIT_MAX_RETRIES} retries · "
                    f"agent={label} model={model} · raising",
                    flush=True,
                )
                raise
            _time.sleep(delay)


class ToolPermissionError(Exception):
    pass


class BaseAgent:
    """
    Base class for all Syndara production agents.
    Subclasses declare allowed_tool_names. Any tool call outside
    that list raises ToolPermissionError — enforced structurally,
    not by prompt.
    """

    allowed_tool_names: list[str] = []
    system_prompt: str = ""
    model: str = MODEL_DEFAULT

    def __init__(self, client: Optional[anthropic.Anthropic] = None):
        # Long read timeout: non-streaming opus calls legitimately run 10+ min.
        # Short connect timeout: a host that won't even connect should fail in
        # seconds, not eat the whole read budget. The SDK retries timeouts.
        # Custom http_client adds TCP keepalive so the long idle wait on a
        # non-streaming call isn't dropped by a router/NAT idle timeout.
        self.client = client or anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            timeout=httpx.Timeout(connect=15.0, read=1800.0, write=120.0, pool=120.0),
            max_retries=2,
            http_client=_build_http_client(),
        )
        self.skill_content = load_skill("practicality_mandate")

    # Dynamic-filtering web tools (web_search_20260209 / web_fetch_20260209) require
    # Opus 4.6+ / Sonnet 4.6 / Fable 5. On a smaller or older model — e.g. if SYNDARA_MODEL
    # is set to Haiku 4.5 to cut cost — those versions 400 on every call, so we fall back to
    # the basic variants. Selecting the version from self.model keeps the model overridable
    # without silently breaking web research.
    _DYNAMIC_WEBTOOL_MODELS = (
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
    )

    def _supports_dynamic_webtools(self) -> bool:
        return any(self.model.startswith(m) for m in self._DYNAMIC_WEBTOOL_MODELS)

    def web_search_tool(self, *, max_uses: int = 5) -> dict:
        """web_search server-tool definition with the right version for self.model."""
        v = "web_search_20260209" if self._supports_dynamic_webtools() else "web_search_20250305"
        return {"type": v, "name": "web_search", "max_uses": max_uses}

    def web_fetch_tool(self, *, max_uses: int = 5) -> dict:
        """web_fetch server-tool definition with the right version for self.model."""
        v = "web_fetch_20260209" if self._supports_dynamic_webtools() else "web_fetch_20250910"
        return {"type": v, "name": "web_fetch", "max_uses": max_uses}

    def _enforce_tools(self, tools: list[dict]) -> list[dict]:
        """Filter tool definitions to only allowed tools."""
        return [t for t in tools if t["name"] in self.allowed_tool_names]

    def _check_tool_call(self, tool_name: str):
        if tool_name not in self.allowed_tool_names:
            raise ToolPermissionError(
                f"Agent {self.__class__.__name__} attempted to use tool "
                f"'{tool_name}' which is not in its allowed set: {self.allowed_tool_names}"
            )

    def _build_system(self, extra: str = "") -> list[dict]:
        """Build system prompt blocks with prompt caching enabled."""
        base = self.system_prompt
        if self.skill_content:
            base += f"\n\n---\n# CONTENT STANDARD (enforce on all outputs)\n\n{self.skill_content}"
        if extra:
            base += f"\n\n{extra}"
        return [
            {
                "type": "text",
                "text": base,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def call(
        self,
        messages: list[dict],
        tools: list[dict] = None,
        max_tokens: int = 8192,
        extra_system: str = "",
    ) -> anthropic.types.Message:
        """Make a Claude API call with tool permission enforcement."""
        allowed_tools = self._enforce_tools(tools or [])
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._build_system(extra_system),
            "messages": messages,
        }
        if allowed_tools:
            kwargs["tools"] = allowed_tools
        raw = _retry_api_call(
            lambda: self.client.messages.with_raw_response.create(**kwargs),
            label=self.__class__.__name__,
            model=self.model,
        )
        return raw.parse()

    def run_tool_loop(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_handlers: dict,
        max_tokens: int = 8192,
        extra_system: str = "",
        trace_label: str = "",
        max_iterations: int = 50,
    ) -> tuple[str, list[dict]]:
        """
        Agentic tool-use loop. Keeps calling Claude until it returns
        a final text response with no more tool calls.

        `trace_label` prefixes diagnostic prints so you can distinguish
        concurrent agents in Railway logs.

        Returns (final_text, updated_messages).
        """
        allowed_tools = self._enforce_tools(tools)
        current_messages = list(messages)
        label = trace_label or self.__class__.__name__
        iteration = 0
        t0 = _time.time()
        accumulated_text: list[str] = []
        # Content blocks accumulated across pause_turn responses.
        # Tracked separately so we never create consecutive assistant messages.
        _pause_content: list = []
        # Container ID for code execution sandbox (created by _20260209
        # dynamic filtering). Must be passed back on pause_turn continuations.
        _container_id: str | None = None
        # Rotating cache breakpoint: marks the latest tool_result block so each
        # iteration reads the growing history from cache instead of re-billing it.
        _cached_block: dict | None = None

        while iteration < max_iterations:
            iteration += 1
            iter_start = _time.time()

            # If resuming from pause_turn, inject the accumulated assistant
            # content as a temporary trailing message for this API call only.
            if _pause_content:
                call_messages = current_messages + [
                    {"role": "assistant", "content": _pause_content}
                ]
            else:
                call_messages = current_messages

            create_kwargs: dict = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": self._build_system(extra_system),
                "messages": call_messages,
            }
            if allowed_tools:
                create_kwargs["tools"] = allowed_tools
            if _container_id:
                create_kwargs["container"] = _container_id

            raw = _retry_api_call(
                lambda: self.client.messages.with_raw_response.create(**create_kwargs),
                label=label,
                model=self.model,
            )
            response = raw.parse()
            llm_elapsed = _time.time() - iter_start

            # Track container ID from code execution (dynamic filtering)
            container = getattr(response, "container", None)
            if container and getattr(container, "id", None):
                _container_id = container.id

            # Collect text and tool use blocks
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            usage = getattr(response, "usage", None)
            usage_str = (
                f"in={getattr(usage, 'input_tokens', '?')} "
                f"out={getattr(usage, 'output_tokens', '?')}"
                if usage else "?"
            )
            print(
                f"[{label}] iter {iteration} · LLM {llm_elapsed:.1f}s · {usage_str} · "
                f"stop={response.stop_reason} · tool_uses={len(tool_uses)} · "
                f"total_elapsed={_time.time() - t0:.1f}s"
            )
            for tu in tool_uses:
                preview = json.dumps(tu.input)[:160]
                print(f"[{label}]   → {tu.name}({preview})")

            # Server-side tools (web_search, web_fetch) return "pause_turn"
            # when the API pauses a long-running turn. Accumulate content
            # and continue — do NOT append to current_messages (would create
            # consecutive assistant messages on the next pause_turn).
            if response.stop_reason == "pause_turn":
                for b in text_blocks:
                    accumulated_text.append(b.text)
                _pause_content = list(_pause_content) + list(response.content)
                print(f"[{label}]   ⏸ pause_turn — continuing server tool execution")
                continue

            # Merge any pause_turn content with this response's content
            if _pause_content:
                merged_content = list(_pause_content) + list(response.content)
                _pause_content = []
            else:
                merged_content = response.content

            if not tool_uses:
                for b in text_blocks:
                    accumulated_text.append(b.text)
                final_text = "\n".join(accumulated_text)
                print(
                    f"[{label}] DONE in {iteration} iterations, "
                    f"{_time.time() - t0:.1f}s total, {len(final_text)} chars output"
                )
                return final_text, current_messages

            # Append the full assistant turn (including any pause_turn content)
            current_messages.append({"role": "assistant", "content": merged_content})

            # Execute each tool call
            tool_results = []
            for tu in tool_uses:
                self._check_tool_call(tu.name)
                handler = tool_handlers.get(tu.name)
                tool_start = _time.time()
                if not handler:
                    result = {"error": f"No handler for tool {tu.name}"}
                else:
                    try:
                        result = handler(**tu.input)
                    except Exception as e:
                        result = {"error": str(e)}
                tool_elapsed = _time.time() - tool_start
                if tool_elapsed > 0.05:  # skip sub-50ms noise
                    print(f"[{label}]   ← {tu.name} returned in {tool_elapsed:.1f}s")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result) if not isinstance(result, str) else result,
                })

            # Move the cache breakpoint to the newest tool_result
            if _cached_block is not None:
                _cached_block.pop("cache_control", None)
            if tool_results:
                tool_results[-1]["cache_control"] = {"type": "ephemeral"}
                _cached_block = tool_results[-1]

            current_messages.append({"role": "user", "content": tool_results})

        print(
            f"[{label}] HIT max_iterations={max_iterations}, forcing stop after "
            f"{_time.time() - t0:.1f}s"
        )
        for b in response.content:  # type: ignore[possibly-undefined]
            if b.type == "text":
                accumulated_text.append(b.text)
        return "\n".join(accumulated_text), current_messages
