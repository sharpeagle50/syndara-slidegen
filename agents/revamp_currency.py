"""Revamp Phase-2 currency agents.

Three isolated passes that power content modernization, designed to be token-safe:
  1. ModuleSummarizerAgent — distill ONE module into a compact structured extract
     (run per module, in isolation, so the full course never hits one context).
  2. CurrencyBriefAgent — research the CURRENT landscape ONCE over the aggregated
     summaries (obsolete techniques, new standards as of today).
  3. DeltaProposerAgent — given a module's full content + the brief, propose
     CONSERVATIVE content deltas. Proposals only — nothing is applied here.

Deltas are stored on course_modules.currency_deltas for creator review; only
approved ones are fed to the restyle. Golden rule: never change content unless it
is genuinely outdated.
"""
from __future__ import annotations

import json

from .base import BaseAgent, extract_json, STYLE_RULE, strip_em_dashes


# ── 1. Per-module summarizer (cheap, toolless, one module at a time) ──────────
SUMMARIZER_SYSTEM = """You distill ONE course module into a compact structured summary for a
course-modernization pass. Extract only what a currency check needs — never rewrite content.

Return ONLY valid JSON (no markdown fences):
{
  "summary": "1-3 sentence description of what this module teaches",
  "key_topics": ["..."],
  "techniques_tools_standards": ["named techniques, tools, libraries, frameworks, standards, or versions this module teaches or relies on — the things that could become outdated"],
  "key_claims_that_could_date": ["specific factual claims or 'the standard way to do X' statements that could become outdated"]
}
Be specific with names/versions (e.g. 'React class components', 'TensorFlow 1.x sessions', 'Redux'),
not vague ('state management')."""


class ModuleSummarizerAgent(BaseAgent):
    allowed_tool_names: list[str] = []
    model = "claude-sonnet-5"
    system_prompt = SUMMARIZER_SYSTEM

    def summarize(self, extracted_content: list[dict], module_title: str = "") -> dict:
        parts = []
        for s in extracted_content:
            t = (s.get("text") or "").strip()
            if t:
                parts.append(f"[Slide {s.get('slide_index', 0) + 1}] {t}")
        body = "\n".join(parts)[:24000]  # token-safe: one module's on-slide text only
        user = f"Module title: {module_title or '(untitled)'}\n\nON-SLIDE CONTENT:\n{body}"
        try:
            resp = self.call(messages=[{"role": "user", "content": user}],
                             max_tokens=2000, disable_thinking=True)
            text = "".join(b.text for b in resp.content if b.type == "text")
            d = extract_json(text)
            if isinstance(d, dict):
                return {
                    "title": module_title,
                    "summary": str(d.get("summary", ""))[:1000],
                    "key_topics": [str(x)[:200] for x in (d.get("key_topics") or [])][:20],
                    "techniques_tools_standards": [str(x)[:200] for x in (d.get("techniques_tools_standards") or [])][:40],
                    "key_claims_that_could_date": [str(x)[:400] for x in (d.get("key_claims_that_could_date") or [])][:20],
                }
        except Exception as e:
            print(f"[ModuleSummarizer] failed: {e!r}", flush=True)
        return {"title": module_title, "summary": module_title,
                "key_topics": [], "techniques_tools_standards": [], "key_claims_that_could_date": []}


# ── 2. Course-level currency brief (web research, ONCE over summaries) ─────────
BRIEF_SYSTEM = """You research the CURRENT state of a field to guide a course modernization.
Given the techniques/tools/standards a course teaches, use web_search to determine — AS OF TODAY —
which are outdated or deprecated and what the current standard replacements are. Be rigorous and
cite the reason; do NOT flag something as outdated unless you are confident it genuinely is.

Return ONLY valid JSON (no fences):
{
  "obsolete": [{"item": "...", "reason": "why it's outdated as of today", "confidence": "high|medium"}],
  "new_standards": [{"name": "...", "replaces": "the old item or null", "why": "why it matters now", "source": "the URL where you verified this (from web_search)"}],
  "notes": "any course-wide modernization guidance"
}"""


class CurrencyBriefAgent(BaseAgent):
    allowed_tool_names = ["web_search", "web_fetch"]
    system_prompt = BRIEF_SYSTEM

    def build_brief(self, module_summaries: list[dict], topic: str, today: str, direction: str = "") -> dict:
        techniques = sorted({t for m in module_summaries for t in (m.get("techniques_tools_standards") or [])})
        claims = [c for m in module_summaries for c in (m.get("key_claims_that_could_date") or [])]
        _dir = (f"\n\nCREATOR DIRECTION (audience/intent — factor in when judging what to modernize):\n"
                f"{direction[:2000]}") if (direction or "").strip() else ""
        user = (
            f"Today's date: {today}.\nCourse topic: {topic}\n\n"
            "TECHNIQUES / TOOLS / STANDARDS TAUGHT ACROSS THE COURSE:\n"
            + "\n".join(f"- {t}" for t in techniques)
            + "\n\nCLAIMS THAT MIGHT HAVE DATED:\n"
            + "\n".join(f"- {c}" for c in claims[:60])
            + _dir
            + "\n\nResearch the current landscape and produce the modernization brief."
        )
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5, "allowed_callers": ["direct"]},
        ]
        try:
            final_text, _ = self.run_tool_loop(
                messages=[{"role": "user", "content": user}], tools=tools,
                tool_handlers={}, max_tokens=8000, trace_label="CurrencyBrief")
            d = extract_json(final_text)
            if isinstance(d, dict):
                return {"obsolete": d.get("obsolete") or [],
                        "new_standards": d.get("new_standards") or [],
                        "notes": str(d.get("notes", ""))[:2000]}
        except Exception as e:
            print(f"[CurrencyBrief] failed: {e!r}", flush=True)
        return {"obsolete": [], "new_standards": [], "notes": ""}


# ── 3. Per-module delta proposer (conservative, toolless, uses the brief) ─────
PROPOSER_SYSTEM = """You propose CONSERVATIVE content updates to modernize one course module,
guided by a currency brief. The golden rule: DO NOT change content unless it is genuinely
outdated. Preserve everything that is still accurate and still worth teaching today.

Propose a delta for a slide ONLY if the brief or your knowledge shows its content is outdated
(a deprecated technique, a superseded standard, a stale fact). Never propose stylistic rewrites,
reorderings, or 'improvements' to content that is simply fine. Prefer the smallest change that
fixes the outdatedness.

Return ONLY valid JSON (no fences):
{"deltas": [
  {"slide_index": <0-based>, "kind": "update"|"remove",
   "original": "the exact outdated text/claim currently on the slide",
   "replacement": "the corrected/current text (empty string if kind=remove)",
   "reason": "why it's outdated, per the brief or current standards",
   "source": "the URL that supports the replacement — copy it from the brief's new_standards[].source when this update is based on that standard; empty string if you genuinely have none"}
]}
If nothing is outdated, return {"deltas": []}."""


# ── 6. Supplemental image suggester — which NEW slides warrant a real web image ──
IMAGE_SUGGEST_SYSTEM = """You decide which of a module's slides would genuinely benefit from a REAL
web image (a photo, a product/UI screenshot, a real-world visual) — and ONLY those. Be conservative:
most slides need NO added image. NEVER suggest replacing text or bullets; only SUPPLEMENT a slide
where a real image clearly aids understanding (e.g. what a tool's interface actually looks like, a
real-world scene the slide references). Skip slides that already have an image, pure title/section
slides, and anything a simple diagram would serve better.

Return ONLY JSON: {"images": [{"slide_index": <0-based>, "query": "the specific image to find",
"why": "one line"}]}. Return {"images": []} if none clearly warrant one — that is common."""


class SupplementalImageAgent(BaseAgent):
    allowed_tool_names: list[str] = []
    model = "claude-sonnet-5"
    system_prompt = IMAGE_SUGGEST_SYSTEM

    def suggest(self, slides: list[dict], existing_image_indices: set) -> list[dict]:
        lines = []
        for s in slides:
            idx = int(s.get("slide_index", 0) or 0)
            tag = " (already has an image)" if idx in existing_image_indices else ""
            lines.append(f"[Slide {idx + 1}{tag}] {(s.get('text') or '')[:300]}")
        user = ("SLIDES:\n" + "\n".join(lines)[:20000]
                + "\n\nSuggest supplemental web images — conservatively; skip slides that already have one.")
        try:
            resp = self.call(messages=[{"role": "user", "content": user}], max_tokens=2000, disable_thinking=True)
            text = "".join(b.text for b in resp.content if b.type == "text")
            d = extract_json(text)
            out = []
            for x in ((d.get("images") if isinstance(d, dict) else None) or []):
                if isinstance(x, dict) and isinstance(x.get("slide_index"), int):
                    out.append({"slide_index": x["slide_index"],
                                "query": str(x.get("query", ""))[:200], "why": str(x.get("why", ""))[:200]})
            return [r for r in out if r["query"]][:8]
        except Exception as e:
            print(f"[SupplementalImage] failed: {e!r}", flush=True)
            return []


# ── 5. Feedback router — split whole-course feedback to the resources it's about ──
FEEDBACK_ROUTER_SYSTEM = """You route a creator's whole-course feedback for a course REVAMP so each
piece reaches ONLY the resource it's actually about — the whole course, a specific module's slides,
or a specific exercise/resource inside a module. Be smart and autonomous: if someone says "the
RAG-for-chem demo was too long," work out that's the demo exercise in the RAG module and route it
there — don't send everything to everyone.

You get the feedback, the module list (position, title, and each module's resources), and today ({today}).

HOW TO WEIGH IT — first decide what kind of input this is:

- EXPLICIT GUIDANCE the creator wrote or already distilled themselves (a set of directives to apply).
  Then just route each directive to the resource it's about and carry it faithfully — NO aggregation,
  NO thresholds. They have already decided; your only job is to send each part to the right agent.

- A RAW TABLE of individual learner responses. Reason about it like a thoughtful instructor — no
  formulas or vote-counting, just judgment:
  * These are individual learners. Don't over-weight one voice — a lone comment is a weak signal, not a
    mandate, and never rebuild a whole piece off one person.
  * Lean toward PRESERVING the creator's original: the bar to change something is higher than the bar to
    keep it. Learners often disagree, so weigh the "leave it alone" voices against the "change it" ones.
  * Objective vs subjective. A FACTUAL problem — a code cell that doesn't run, a dead link, a wrong claim
    — is worth acting on even if only one person flags it; it's simply true. A PREFERENCE — tone,
    difficulty, pacing, wordiness — needs a real pattern (several learners saying essentially the same
    thing) before you act on it.

- EITHER WAY, balance against RECENCY. This revamp exists to make the course current as of {today}. If a
  piece of feedback is itself outdated — it only made sense when given, or the field has moved past it —
  IGNORE it, even if many said it.

Return ONLY JSON:
{
  "global": "guidance that applies to the WHOLE course (empty if none)",
  "modules": [
    {"position": N,
     "deck": "guidance about THIS module's slides (empty if none)",
     "exercise": "guidance about a specific exercise/resource in THIS module — name/describe which one (empty if none)"}
  ],
  "ignored": ["each piece you deliberately dropped + why: 'single-voice fluke' / 'outdated' / 'already current'"]
}
Only list a module if it has deck or exercise guidance. Keep each guidance concise and actionable —
it's a nudge for the authoring agent, not a spec."""


class FeedbackRouterAgent(BaseAgent):
    allowed_tool_names: list[str] = []
    system_prompt = FEEDBACK_ROUTER_SYSTEM

    def route(self, feedback: str, module_summaries: list[dict], today: str, direction: str = "") -> dict:
        self.system_prompt = FEEDBACK_ROUTER_SYSTEM.replace("{today}", today)
        mods = "\n".join(
            f"{m.get('position')}. {m.get('title') or '(untitled)'} — resources: {m.get('resources') or 'slides'}"
            for m in module_summaries)
        parts = [f"Today: {today}", f"MODULES:\n{mods}"]
        if (direction or "").strip():
            parts.append("CREATOR DIRECTION (the creator's own explicit intent — CARRY FAITHFULLY; route "
                         "course-wide items to 'global' and resource-specific ones to that module):\n"
                         f"{direction[:10000]}")
        if (feedback or "").strip():
            parts.append(f"LEARNER FEEDBACK (raw responses — aggregate with judgment):\n{feedback[:30000]}")
        parts.append("Route everything: carry the creator direction faithfully; for learner feedback use "
                     "judgment (don't over-weight one voice, lean toward keeping, act on factual issues even "
                     "from one person, require a pattern for preferences); drop outdated feedback.")
        user = "\n\n".join(parts)
        try:
            resp = self.call(messages=[{"role": "user", "content": user}], max_tokens=6000)
            text = "".join(b.text for b in resp.content if b.type == "text")
            d = extract_json(text)
            if isinstance(d, dict):
                mods_out: dict = {}
                for m in (d.get("modules") or []):
                    if isinstance(m, dict) and isinstance(m.get("position"), int):
                        mods_out[m["position"]] = {
                            "deck": str(m.get("deck", ""))[:2000],
                            "exercise": str(m.get("exercise", ""))[:2000],
                        }
                return {"global": str(d.get("global", ""))[:3000], "modules": mods_out,
                        "ignored": [str(x)[:300] for x in (d.get("ignored") or [])][:40]}
        except Exception as e:
            print(f"[FeedbackRouter] failed: {e!r}", flush=True)
        return {"global": "", "modules": {}, "ignored": []}


# ── 4. Course-structure reviewer (Phase 4 — conservative, opt-in) ─────────────
STRUCTURE_SYSTEM = """You review the STRUCTURE of an existing course during a revamp: whether any
modules should be dropped, reordered, merged, or added given how the subject is taught TODAY ({today}).

BE A SKEPTIC. The creator designed this course deliberately, so in the VAST MAJORITY of cases you
should propose NOTHING. Only propose a change you would confidently flag to a colleague — never a
stylistic or "it could flow better" tweak. When in doubt, leave it exactly as it is.

- DROP a module ONLY if its topic is wholly obsolete today (teaches a dead tool/practice with no
  modern equivalent worth keeping) — not merely "less important".
- REORDER ONLY if the current order has a real dependency problem (a module clearly depends on one
  that comes later). Otherwise keep the order.
- MERGE and ADD are ADVICE ONLY (the revamp will NOT do them automatically): note them for the
  creator. MERGE = two modules now overlap so heavily they'd be redundant. ADD = the field now has
  an essential topic that NO module covers.

Return ONLY JSON (no fences):
{
  "drop": [{"position": N, "reason": "why it is wholly obsolete today"}],
  "reorder": [ordered list of ALL KEPT module positions in the new order] or null if the order is fine,
  "advice": [{"kind": "merge"|"add", "detail": "which modules / what topic", "reason": "..."}]
}
Return {"drop": [], "reorder": null, "advice": []} if the structure is fine — that is the common case."""


class RevampStructureAgent(BaseAgent):
    allowed_tool_names = ["web_search"]
    system_prompt = STRUCTURE_SYSTEM

    def propose(self, module_summaries: list[dict], topic: str, today: str, direction: str = "") -> dict:
        self.system_prompt = STRUCTURE_SYSTEM.replace("{today}", today)
        lines = "\n".join(
            f"{m.get('position')}. {m.get('title') or '(untitled)'}: {str(m.get('summary') or '')[:300]}"
            for m in module_summaries)
        _dir = (f"\n\nCREATOR DIRECTION (audience + intent to respect — e.g. keep it beginner-friendly):\n"
                f"{direction[:3000]}") if (direction or "").strip() else ""
        user = (f"Today: {today}\nCourse: {topic}\n\nMODULES (position. title: summary):\n{lines}{_dir}\n\n"
                "Review the structure. Remember: propose nothing unless clearly warranted.")
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5, "allowed_callers": ["direct"]}]
        try:
            final_text, _ = self.run_tool_loop(
                messages=[{"role": "user", "content": user}], tools=tools,
                tool_handlers={}, max_tokens=6000, trace_label="RevampStructure")
            d = extract_json(final_text)
            if isinstance(d, dict):
                return {
                    "drop": [{"position": int(x["position"]), "reason": str(x.get("reason", ""))[:500], "status": "proposed"}
                             for x in (d.get("drop") or [])
                             if isinstance(x, dict) and isinstance(x.get("position"), int)],
                    "reorder": [int(p) for p in d["reorder"]] if isinstance(d.get("reorder"), list) else None,
                    "advice": [{"kind": x.get("kind"), "detail": str(x.get("detail", ""))[:500],
                                "reason": str(x.get("reason", ""))[:500]}
                               for x in (d.get("advice") or [])
                               if isinstance(x, dict) and x.get("kind") in ("merge", "add")],
                }
        except Exception as e:
            print(f"[RevampStructure] failed: {e!r}", flush=True)
        return {"drop": [], "reorder": None, "advice": []}


class DeltaProposerAgent(BaseAgent):
    allowed_tool_names: list[str] = []
    system_prompt = PROPOSER_SYSTEM

    def propose(self, extracted_content: list[dict], currency_brief: dict, module_title: str = "") -> list[dict]:
        self.system_prompt = PROPOSER_SYSTEM + STYLE_RULE
        parts = []
        for s in extracted_content:
            t = (s.get("text") or "").strip()
            parts.append(f"[Slide {s.get('slide_index', 0) + 1}] {t}")
        body = "\n".join(parts)[:40000]
        user = (
            f"Module: {module_title}\n\nCURRENCY BRIEF:\n{json.dumps(currency_brief, indent=2)[:6000]}\n\n"
            f"MODULE SLIDES:\n{body}\n\nPropose conservative modernization deltas (or none)."
        )
        try:
            resp = self.call(messages=[{"role": "user", "content": user}], max_tokens=8000)
            text = "".join(b.text for b in resp.content if b.type == "text")
            d = extract_json(text)
            deltas = d.get("deltas") if isinstance(d, dict) else None
            out = []
            for x in (deltas or []):
                if not isinstance(x, dict):
                    continue
                out.append({
                    "slide_index": int(x.get("slide_index", 0) or 0),
                    "kind": x.get("kind") if x.get("kind") in ("update", "remove") else "update",
                    "original": str(x.get("original", ""))[:2000],   # match target — do NOT alter
                    "replacement": strip_em_dashes(str(x.get("replacement", ""))[:2000]),
                    "reason": str(x.get("reason", ""))[:1000],
                    "source": str(x.get("source", ""))[:400],  # URL supporting the modernized fact
                    "status": "proposed",  # proposed | approved | rejected
                })
            return out
        except Exception as e:
            print(f"[DeltaProposer] failed: {e!r}", flush=True)
            return []
