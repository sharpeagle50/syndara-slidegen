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

from .. import keyring
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


# Live in-flight cost: a global registry of currently-open run sinks keyed by job_id, so the
# owner dashboard can price spend that is STILL accumulating (run_costs rows are only written
# when a run finishes). Best-effort, in-process, resets on restart.
_active_runs: dict = {}
_active_runs_lock = threading.Lock()


def cost_capture_begin(job_id=None, run_type: str = "") -> None:
    """Open a fresh cost-capture sink in the current context (one per run). When job_id is
    given, also register the sink in the live in-flight registry so the dashboard can price
    it while it accumulates; pair with cost_unregister_run(job_id) when the run finishes."""
    sink: list = []
    _cost_sink.set(sink)
    if job_id is not None:
        with _active_runs_lock:
            _active_runs[job_id] = {"sink": sink, "run_type": run_type, "at": _time.time()}


def cost_unregister_run(job_id) -> None:
    """Drop a run from the live in-flight registry (call once its cost has been persisted)."""
    with _active_runs_lock:
        _active_runs.pop(job_id, None)


def active_run_sinks() -> list:
    """Snapshot of currently-open run sinks: [{job_id, run_type, at, entries}]. `entries` is a
    shallow copy so the caller can price it without racing the appending builder threads."""
    with _active_runs_lock:
        items = list(_active_runs.items())
    return [
        {"job_id": jid, "run_type": v.get("run_type", ""), "at": v.get("at"),
         "entries": list(v["sink"])}
        for jid, v in items
    ]


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


def report_tts_usage(chars: int, model: str, *, external: bool = False) -> None:
    """Report TTS character usage (priced separately in the private layer). No-op when no run
    sink is open; never raises. external=True marks usage billed to the CREATOR'S own account
    (own-key ElevenLabs): recorded and priced as an external ESTIMATE for the run detail view,
    never counted in Syndara COGS totals."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "tts", "stage": "tts", "module": _cost_module.get(),
                     "model": model, "chars": int(chars or 0), "external": bool(external)})
    except (TypeError, ValueError):
        pass


def report_video_usage(renders: int = 1, seconds: float = 0.0, *, test: bool = False,
                       is_custom: bool = False) -> None:
    """Report avatar/presenter video render usage (priced separately in the private layer). No-op
    when no run sink is open; never raises. `test` (watermarked) and `is_custom` (creator's own
    Synthesia key/quota) renders cost us nothing and are priced to $0 downstream."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "video", "stage": "video", "module": _cost_module.get(),
                     "renders": int(renders or 0), "seconds": float(seconds or 0.0),
                     "test": bool(test), "is_custom": bool(is_custom)})
    except (TypeError, ValueError):
        pass


def report_gpu_usage(gpu: str, seconds: float) -> None:
    """Report GPU compute time (wall-clock seconds) for notebook test-execution on
    a Modal GPU, priced per-second in the private layer. No-op when no run sink is
    open; never raises."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "gpu", "stage": "notebook_gpu", "module": _cost_module.get(),
                     "gpu": str(gpu or "").upper(), "seconds": float(seconds or 0.0)})
    except (TypeError, ValueError):
        pass


def report_render_usage(seconds: float) -> None:
    """Report Manim animation render time (wall-clock seconds) on Modal CPU, priced per-second in
    the private layer (Concept Animation). No-op when no run sink is open; never raises."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "render", "stage": "animation_render", "module": _cost_module.get(),
                     "seconds": float(seconds or 0.0)})
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


def report_build_summary(label: str, detail: dict) -> None:
    """Record what the slide-builder actually did in a run — tool usage, repeated
    actions, turns, timing — so the generation trace can show where it spun on the
    wrong approach or burned its whole tool budget. Priced-run accounting ignores
    this kind. No-op when no run sink is open; never raises."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "build", "stage": "builder", "module": _cost_module.get(),
                     "label": label, "detail": dict(detail or {})})
    except (TypeError, ValueError):
        pass


def report_gen_event(phase: str, event: str, detail=None) -> None:
    """Record a generic generation-trace event — a chart's generated code, an
    image-verify decision, a reformat retry (the model fumbled its output format),
    etc. Reported to the run sink; web_runner drains these to the generation trace.
    Priced-run accounting ignores this kind. No-op when no run sink is open."""
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        sink.append({"kind": "event", "stage": str(phase), "module": _cost_module.get(),
                     "event": str(event), "detail": dict(detail or {})})
    except (TypeError, ValueError):
        pass


def _report_agent_output(label: str, model: str, msg) -> None:
    """Verbose per-agent trace (opt-in via SYNDARA_TRACE_VERBOSE): record a truncated
    summary of what each agent produced — TEXT OUTPUT ONLY, never inputs/images — so
    the whole pipeline's decisions are inspectable. Reported to the run sink and
    drained to the trace by web_runner. OFF by default (one env check, then return),
    so normal builds store nothing extra. Never raises."""
    if os.environ.get("SYNDARA_TRACE_VERBOSE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    sink = _cost_sink.get()
    if sink is None:
        return
    try:
        text = "".join(
            (getattr(b, "text", "") or "")
            for b in (getattr(msg, "content", None) or [])
            if getattr(b, "type", None) == "text"
        )
        sink.append({"kind": "agent", "stage": "agent", "module": _cost_module.get(),
                     "label": label, "model": model,
                     "stop": getattr(msg, "stop_reason", None), "output": text[:4000]})
    except (TypeError, ValueError, AttributeError):
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
    "\n\nWRITING STYLE (required): Be concise and direct. Every sentence earns its place: "
    "no filler, no meandering setup, no restating what was just said, no decorative "
    "vocabulary when a plain word works. Use technical terms where they are the accurate "
    "choice; never use a fancy word just to sound sophisticated."
    "\n\nREFERENCES (required): Never refer to content by position number: no \"Module 3\", "
    "\"in the last module\", \"Section 2\", \"the previous deck\". Numbering is not stable "
    "(order changes, and a module is often downloaded or taken on its own), and it tells the "
    "learner nothing. When you need to point back, name the TOPIC instead: \"when we covered "
    "buffer solutions\" rather than \"in Module 2\"."
)

# Visual-ness spectrum for slide layouts (1 = most visual … 5 = most text). The create-page slider picks
# one of these; the chosen text is substituted INTO each planner's prompt at the {layout_lean} placeholder
# (it becomes the deck's layout section — NOT an appended override). 3 = Neutral is the default.
#   1 = the original visual-first philosophy (pre-7ba50ab), verbatim.
#   4 = the original text-forward base wording (the pre-slider "very text-leaning" default).
#   5 = one notch MORE text-forward than the base (lean text-forward throughout, fuller lines).
#   2 = level 1 relaxed toward text; 3 = a true neutral midpoint.
VISUAL_DIRECTIVES = {
    1: ("Lean hard on visuals. The slide is a visual anchor, not a document: only a handful of words — key "
        "phrases, big numbers, short labels, NOT sentences. If a slide has more than 3 short bullets or any "
        "bullet longer than 6 words, you're doing it wrong; push ALL depth into the speaker notes. Aim for "
        "70%+ of content slides to carry a real visual (a chart, diagram, flowchart, or image), not just the "
        "title/summary slides — text-only bullet slides are rare exceptions. Use a table only for a small "
        "structured comparison, and only if a chart or diagram doesn't fit. If you can replace text with a "
        "chart, flowchart, or diagram, do it."),
    2: ("Match the layout to the content, leaning visual. Most content slides should carry a real chart, "
        "diagram, flowchart, or image, and the slide stays a visual anchor rather than a document. Keep "
        "on-slide text tight: short phrases and labels, only a few short bullets, no full paragraphs. A "
        "clean, well-structured text-forward slide (bullets, columns, or a small table) is fine where the "
        "content is genuinely verbal or conceptual and a visual would be forced — but that's the exception, "
        "not the default."),
    3: ("Match the layout to the content with no bias either way. Where the content is spatial, relational, "
        "or quantitative, use a chart, diagram, flowchart, or image; where it's verbal or conceptual, use a "
        "clean text-forward layout (bullets, columns, or a table). Aim for a genuine balance across the deck "
        "— neither mostly diagrams nor mostly bullets — and never a bare, unstructured wall of text."),
    4: ("Match the layout to the content, not to a quota. Spatial, relational, or quantitative content "
        "belongs in a diagram, chart, or table; sequential in a flow. Verbal or conceptual content — "
        "principles, criteria, contrasts, lists a diagram would only awkwardly force — belongs in a "
        "text-forward layout: bullets, columns, or a table, whichever fits the structure. Text-forward "
        "slides are first-class, not a failure; reach for one whenever a slide wouldn't stand on its own "
        "from a diagram alone. Give most content slides a strong focal element — either a visual or a "
        "well-structured text-forward layout; what to avoid is the bare, unstructured wall of text, not "
        "text itself. Keep diagrams strong where they genuinely fit — just stop forcing them onto ideas "
        "that aren't visual, and vary the mix so the deck isn't diagrams and tables on repeat."),
    5: ("Match the layout to the content. Text-forward slides are first-class — reach for a well-structured "
        "bullet, column, or table layout for verbal or conceptual content (principles, criteria, contrasts, "
        "lists), and text may carry fuller lines. Give most content slides a strong focal element, either a "
        "visual or a clean text-forward layout; what to avoid is a bare, unstructured wall of text, not text "
        "itself. Lean text-forward throughout, with one pull toward visuals: where a slide would clearly "
        "land better as a chart, diagram, or image, use the visual instead of defaulting to bullets."),
}
VISUAL_DEFAULT_LEVEL = 3


def visual_directive(level) -> str:
    """The layout-section text for a visual-ness level (1 = most visual … 5 = most text). Substituted into
    each planner prompt at the {layout_lean} placeholder — it IS the deck's layout guidance, not an add-on."""
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        lvl = VISUAL_DEFAULT_LEVEL
    return VISUAL_DIRECTIVES.get(lvl, VISUAL_DIRECTIVES[VISUAL_DEFAULT_LEVEL])


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


def text_from_response(response) -> str:
    """Join the text blocks of a Messages response, skipping non-text blocks.

    Necessary for adaptive-thinking models (e.g. Sonnet 5), whose response content
    starts with a thinking block — so ``content[0].text`` would raise. Returns "" for
    an empty/None response.
    """
    return "".join(
        getattr(b, "text", "") or ""
        for b in (getattr(response, "content", None) or [])
        if getattr(b, "type", "") == "text"
    )


# A backslash that does NOT begin a valid JSON escape (\" \\ \/ \b \f \n \r \t \uXXXX).
# LLMs writing math/code-heavy content inside JSON strings routinely slip a single-backslash
# LaTeX command (\lambda, \(, \mu) — one slip anywhere invalidates the whole document.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


def loads_lenient(s: str) -> dict:
    """json.loads with two escalating fallbacks for LLM-authored JSON: first allow raw
    control characters inside strings (long multi-line cell/prompt strings), then double
    every backslash that isn't a valid escape sequence (the LaTeX slip). Valid escape
    sequences are never touched, so a well-formed document parses identically to strict.
    NOTE: a slipped backslash that happens to FORM a valid escape (\\frac -> formfeed,
    \\theta -> tab) parses fine and can't be detected here — that residual corruption is
    the model's escaping error, not recoverable at parse time."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(s, strict=False)
    except json.JSONDecodeError:
        pass
    return json.loads(_INVALID_JSON_ESCAPE_RE.sub(r"\\\\", s), strict=False)


def extract_json(text: str) -> dict:
    """Robustly extract a JSON object from an LLM response.

    Handles:
      - bare JSON ("{...}")
      - fenced ```json blocks
      - JSON embedded in prose ("Here's the plan: {...}")
      - trailing prose after the JSON
      - invalid escapes / raw control chars in strings (via loads_lenient)
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
    return loads_lenient(t[start:end])

SKILLS_DIR = Path(__file__).parent.parent / "skills"
MODEL_DEFAULT = os.environ.get("SYNDARA_MODEL", "claude-opus-5")


def load_skill(name: str) -> str:
    path = SKILLS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return ""


# 12 modules build concurrently, each firing plan + ~30 vision-QA Opus calls, so a sustained 429
# window is normal under load. The old 5-retry / 15s budget capped total backoff at ~7.75 min and
# aborted the whole stage (and thus the module) past it. 8 retries with a 300s ceiling ≈ 20 min of
# backoff, and _retry_api_call also honors the server's Retry-After when it's longer.
RATE_LIMIT_MAX_RETRIES = int(os.environ.get("SYNDARA_RATE_LIMIT_RETRIES", "8"))
RATE_LIMIT_BASE_DELAY = 15   # seconds
RATE_LIMIT_MAX_DELAY = 300   # per-attempt ceiling


def _retry_api_call(fn, *, label: str, model: str):
    """Call fn(), retrying on 429 / 529 / connection errors with exponential
    backoff. Returns the raw response."""
    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            raw = fn()
            _log_ratelimit(raw.headers, label, model)
            try:
                _msg = raw.parse()
                _report_usage(label, model, _msg.usage)
                _report_agent_output(label, model, _msg)  # verbose trace; no-op unless SYNDARA_TRACE_VERBOSE
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
            delay = min(delay, RATE_LIMIT_MAX_DELAY)
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
                delay = min(delay, RATE_LIMIT_MAX_DELAY)
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
            delay = min(delay, RATE_LIMIT_MAX_DELAY)
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


class MaxTokensError(RuntimeError):
    """The agent's final (no-tool) turn was cut off by max_tokens, so its output is truncated.
    A subclass of RuntimeError so existing `except Exception` callers still catch it; callers that
    want to distinguish a truncated CLOSING remark from a genuine mid-run failure can catch this
    specifically (e.g. AgenticSlideBuilder, which can still save an already-complete deck)."""


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
        self.client = client or keyring.sync_anthropic(
            timeout=httpx.Timeout(connect=15.0, read=1800.0, write=120.0, pool=120.0),
            max_retries=2,
            http_client=_build_http_client(),
        )
        self.skill_content = load_skill("practicality_mandate")

    # Dynamic-filtering web tools (web_search_20260209 / web_fetch_20260209) require
    # Opus 4.6+ / Sonnet 4.6+ / Fable 5. On a smaller or older model — e.g. if SYNDARA_MODEL
    # is set to Haiku 4.5 to cut cost — those versions 400 on every call, so we fall back to
    # the basic variants. Selecting the version from self.model keeps the model overridable
    # without silently breaking web research.
    _DYNAMIC_WEBTOOL_MODELS = (
        "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
    )

    # Sonnet 5 AND Opus 5 turn adaptive thinking ON by default and return a leading thinking block,
    # whereas these agents were tuned for the non-thinking behavior of their predecessors (Sonnet
    # 4.6 / Opus 4.8, which defaulted thinking-off). Default to disabling it so output shape, text
    # extraction, latency, and cost stay unchanged; a subclass that benefits from reasoning (e.g. the
    # Reviewer, or the planner if we opt it in later) sets this True. Fable/Mythos 5 reject
    # {type:"disabled"} (thinking is always on there) so they're left alone.
    adaptive_thinking: bool = False

    # Models where thinking is always on and {type:"disabled"} is rejected (400).
    _ALWAYS_ON_THINKING = ("claude-fable-5", "claude-mythos-5", "claude-mythos-preview")

    def _maybe_disable_thinking(self, kwargs: dict, *, force: bool = False) -> None:
        if self.model.startswith(self._ALWAYS_ON_THINKING):
            return  # can't disable on these — leave thinking on
        # Sonnet 5 and Opus 5 both default adaptive thinking ON. Disable it unless the agent opted
        # in (adaptive_thinking) so behavior matches the pre-5 models these prompts were tuned for.
        # Safe because we never set effort (defaults to high) — on Opus 5, {type:"disabled"} +
        # effort xhigh/max would 400, but disabled + high is allowed.
        if force or (not self.adaptive_thinking
                     and self.model.startswith(("claude-sonnet-5", "claude-opus-5"))):
            kwargs["thinking"] = {"type": "disabled"}

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
        # With the dynamic-filtering web tools (Opus etc.), web_search runs inside a
        # code-execution sandbox and hands results back as a JSON string. Tell the model that
        # shape up front so it stops burning a turn rediscovering it every run ("search looks
        # empty -> inspect the raw value -> oh it's a JSON string -> parse -> now it works").
        # Pure guidance: it does not change what search does or which results come back.
        if "web_search" in self.allowed_tool_names and self._supports_dynamic_webtools():
            base += (
                "\n\n---\n# WEB SEARCH RESULT SHAPE\n"
                "web_search / web_fetch results are delivered into your code-execution sandbox "
                "as a JSON STRING. Call json.loads() on it immediately to read the results — the "
                "result is not empty; do not inspect the raw value or re-run the search to "
                "'check the structure' first. Use code execution only to filter those results."
            )
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
        disable_thinking: bool = False,
    ) -> anthropic.types.Message:
        """Make a Claude API call with tool permission enforcement.

        ``disable_thinking=True`` forces thinking off for this call even on a
        subclass with ``adaptive_thinking=True`` — for mechanical follow-ups
        (e.g. reformatting a verdict to JSON) that don't benefit from reasoning
        and whose small budgets thinking could otherwise truncate.
        """
        allowed_tools = self._enforce_tools(tools or [])
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._build_system(extra_system),
            "messages": messages,
        }
        self._maybe_disable_thinking(kwargs, force=disable_thinking)
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
            self._maybe_disable_thinking(create_kwargs)
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
                if response.stop_reason == "max_tokens":
                    # Truncated mid-answer. Returning this as "complete" ships partial slide plans,
                    # unparseable notebook JSON, or cut-off reviews that fail silently downstream
                    # (e.g. an empty exercise that looks successful). Surface it so the caller's
                    # error handling (retry / mark-failed) runs instead.
                    raise MaxTokensError(
                        f"{label}: response hit max_tokens after {len(final_text)} chars — output "
                        f"is truncated/incomplete")
                print(
                    f"[{label}] DONE in {iteration} iterations, "
                    f"{_time.time() - t0:.1f}s total, {len(final_text)} chars output"
                )
                return final_text, current_messages

            # Append the full assistant turn (including any pause_turn content)
            current_messages.append({"role": "assistant", "content": merged_content})

            # Execute the turn's tool calls — CONCURRENTLY when the model batched several
            # (a planner turn requesting 3 images used to run them back-to-back). Handlers
            # are sync and independent (image fetches, chart renders, each with its own
            # output path); results are re-ordered to match tool_uses, so the transcript
            # the model sees is identical to the serial version.
            def _run_one_tool(tu):
                if tu.name not in self.allowed_tool_names:
                    # The model emitted a disallowed or malformed tool call (e.g. a stray
                    # 'invoke name=' from a botched tool-call format). Don't raise — that would
                    # abort the whole loop and fail the module on a single transient glitch.
                    # Hand the model an error result so it can self-correct and keep going.
                    print(f"[{label}]   ⚠ disallowed/unknown tool {tu.name!r}; returning error to model")
                    return {
                        "error": f"Tool '{tu.name}' is not available. "
                                 f"Use only these tools: {self.allowed_tool_names}."
                    }
                handler = tool_handlers.get(tu.name)
                if not handler:
                    return {"error": f"No handler for tool {tu.name}"}
                tool_start = _time.time()
                try:
                    result = handler(**tu.input)
                except Exception as e:
                    result = {"error": str(e)}
                tool_elapsed = _time.time() - tool_start
                if tool_elapsed > 0.05:  # skip sub-50ms noise
                    print(f"[{label}]   ← {tu.name} returned in {tool_elapsed:.1f}s")
                return result

            # Whitelist gate: only tools that are stateless/independent may run concurrently.
            # find_image fetches to a uuid'd path with a lock-guarded cache. Handlers like the
            # layout-library builder's add_slide MUTATE the shared deck — running two of those
            # concurrently would race python-pptx and scramble slide ORDER, so any turn
            # containing a non-whitelisted tool executes serially, exactly as before.
            _PARALLEL_SAFE_TOOLS = {"find_image"}
            if len(tool_uses) > 1 and all(tu.name in _PARALLEL_SAFE_TOOLS for tu in tool_uses):
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(4, len(tool_uses))) as _tex:
                    _results = list(_tex.map(_run_one_tool, tool_uses))
            else:
                _results = [_run_one_tool(tu) for tu in tool_uses]
            tool_results = []
            for tu, result in zip(tool_uses, _results):
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
