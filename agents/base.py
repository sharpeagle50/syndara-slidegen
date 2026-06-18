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

        with _rl_mins_lock:
            slot = _rl_mins.setdefault(model, {"itpm": None, "otpm": None, "rpm": None})
            for k, v in (("itpm", itpm_rem), ("otpm", otpm_rem), ("rpm", rpm_rem)):
                try:
                    iv = int(v) if v is not None else None
                except (TypeError, ValueError):
                    iv = None
                if iv is not None and (slot[k] is None or iv < slot[k]):
                    slot[k] = iv
            low = slot.copy()

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
            return raw
        except anthropic.RateLimitError as e:
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
