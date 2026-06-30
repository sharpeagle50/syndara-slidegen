"""Reviewer Agent: critiques content, fact-checks with web search. NO write tools."""
from __future__ import annotations
import json
from .base import BaseAgent, extract_json, text_from_response

REVIEWER_SYSTEM = """You are the Syndara Reviewer + Fact-Checker. You evaluate course
content against the content standard AND aggressively fact-check every factual claim
using web_search. You have NO write tools. You can only read and search. Your
output is a verdict that tells the Builder what to fix.

FACT-CHECK RULES (treat as required, not optional):
- GROUNDING FIRST: If the review request includes an AUTHORITATIVE PLAN (a
  researched, source-cited content plan plus its list of sources), that plan is
  the source of truth. The planner already web-searched and cited those facts.
  Your fact-checking job is to catch where the BUILDER DRIFTED from the plan —
  NOT to re-litigate the planner's research:
    * Any statistic, date, name, tool, or claim on a slide that also appears in
      the plan is already researched and cited. Treat it as VERIFIED. Do NOT
      flag it `unverified`, and NEVER tell the Builder to remove or water down a
      fact that is grounded in the plan. Stripping cited, researched material is
      itself a defect — it is the single worst thing you can do here.
    * Only fact-check a claim that appears on a slide but does NOT trace to the
      plan or its sources — those are Builder additions. Run web_search on
      those; if you cannot confirm within 2 attempts, flag `unverified`.
  When NO plan is provided, fact-check every claim from scratch using the rules
  below.
- For EVERY tool, prompt, command, shortcut, API call, statistic, or date that
  is NOT already grounded in the plan, run web_search to confirm it exists and
  is accurate TODAY (search "[tool name] 2025" / "[tool name] features"). Tools
  change frequently; a command from 2023 may no longer exist.
- If you cannot verify such a NON-PLAN claim within 2 attempts, flag it
  `unverified` and tell the Builder to remove or rephrase it.
- Error on the side of demanding verification for Builder additions — but never
  at the cost of deleting a plan-backed, already-cited fact.

CONTENT STANDARD (from skills/practicality_mandate):
For each slide, evaluate:
1. Does it name at least one specific, real tool? (Rule 1)
2. Does it show a specific technique or step? (Rule 2)
3. Does it include a specific prompt, command, or numbered steps? (Rule 3)
4. Are all tool names verifiable as current and available? (web_search MUST confirm)
5. Are factual claims cited?

Produce a JSON verdict with EXACTLY this structure. The slide_index field is
required on every slide entry — the Builder uses it to target edits, so if
you omit it or get it wrong, the wrong slide gets rewritten.

{
  "status": "approved" | "revise" | "needs_diagram" | "needs_image",
  "overall_compliance_pct": 0.85,
  "auto_review_pass_rate": 0.70,
  "web_verification_rate": 0.90,
  "citation_coverage": 0.75,
  "slides": [
    {
      "slide_index": 0,                     // 0-BASED. Must match input slide_index.
      "score": 3,                           // 0-3 (Rules 1, 2, 3)
      "status": "approved" | "revise" | "needs_diagram",
      "issues": ["missing_tool", "missing_command", "unverified_tool",
                 "missing_citation", "hallucinated_feature", "wrong_tool_name",
                 "stale_info", "too_text_heavy"],
      "suggestion": "Actionable, slide-specific edit the Builder can execute.
                    Tell them exactly what to change: which bullet to rewrite,
                    which tool name to swap, which fact to remove. Avoid
                    vague feedback like 'improve this slide'.",
      "tools_verified": {"ChatGPT": true, "SomeTool": false}
    }
  ],
  "global_feedback": "One-paragraph overall summary — issues that span the
                      deck, not any single slide."
}

RULES FOR SLIDE ENTRIES:
- ONLY include a slides[] entry for a slide if it needs action (status is
  'revise' or 'needs_diagram'). Approved slides should be omitted entirely —
  keeps the verdict small AND tells the Builder to leave them alone.
  (Do still count approved slides in overall_compliance_pct.)
- Every entry MUST have slide_index matching the input. Never guess, never
  invent an index, never skip it.
- Write suggestions as imperative instructions: "Replace 'X' with 'Y'",
  "Remove the claim about Z", "Add a citation to docs.X.com/Y".

Score per slide: 0-3 (one point each for Rule 1, Rule 2, Rule 3).
A slide needs revision if score < 2.
Overall status is 'approved' if ≥60% of slides score ≥2.
Otherwise status is 'revise'.
If a slide would benefit from a diagram (comparison, workflow, stats), set status to 'needs_diagram'.

CRITICAL: Output your JSON verdict as direct text. Do NOT compose or
store it inside code execution — code execution is only for filtering
search results.
"""


class ReviewerAgent(BaseAgent):
    # Structurally enforced: NO write tools
    allowed_tool_names = ["web_search"]
    system_prompt = REVIEWER_SYSTEM
    # Sonnet, not the Opus default: the reviewer is a plan-grounded fact-check (a checker,
    # not learner-facing output), and Sonnet 5 is strong at web-search verification. The
    # model-aware web_search_tool() picks web_search_20260209 for Sonnet 5, so the swap is
    # safe. (Opus-tier reasoning isn't needed to catch drift from an already-cited plan.)
    model = "claude-sonnet-5"
    # Keep Sonnet 5's adaptive thinking ON here — fact-checking against web search
    # genuinely benefits from reasoning. run_tool_loop already filters content by
    # block type, so the leading thinking block is handled; the .call() fallback
    # below extracts text the same way.
    adaptive_thinking = True

    def review_slides(self, slide_content: list[dict], cycle: int = 1,
                      revision_feedback: str = "", plan_context: dict | None = None) -> dict:
        """
        Review extracted slide content.
        slide_content: list of {slide_index, text, speaker_notes}
        revision_feedback: if set, only check whether this feedback was addressed.
        plan_context: {markdown, sources} — the planner's already-researched,
            source-cited plan. When provided, the reviewer verifies on-slide
            facts AGAINST it instead of re-deriving them blind: facts grounded
            in the plan are treated as cited (never stripped), and only Builder
            additions not supported by the plan get fact-checked.
        Returns a verdict dict.
        """
        content_summary = json.dumps(slide_content, indent=2)

        if plan_context and plan_context.get("markdown"):
            _sources = plan_context.get("sources") or []
            _src_lines = "\n".join(f"- {s}" for s in _sources[:60]) or "(none listed)"
            plan_block = f"""
AUTHORITATIVE PLAN — already researched and source-cited by the planner. This is
the source of truth for facts. A fact on a slide that traces to this plan is
already verified and cited: do NOT flag it `unverified` and do NOT tell the
Builder to remove it. Use web_search ONLY for claims that appear on a slide but
are NOT supported by this plan or its sources (Builder additions).

PLAN CONTENT:
{plan_context['markdown']}

SOURCES the planner used (deck-wide):
{_src_lines}
"""
        else:
            plan_block = ('\nUse web_search to verify any tool names or facts '
                          'you are uncertain about (e.g., search "ChatGPT '
                          'features 2025" or "is [ToolName] still available").\n')

        if revision_feedback:
            user_msg = f"""Review cycle {cycle}. A creator requested this specific revision:

CREATOR'S REQUEST:
"{revision_feedback}"

Check ONLY whether the revised slide(s) below correctly and adequately address the creator's request. Do NOT flag anything else — no new suggestions, no unrelated issues. Just verify the requested change was made.

Use web_search to verify any factual claims introduced by the revision (tool names, commands, etc.). Facts that the revision draws from the authoritative plan below are already cited — do not flag or strip those.
{plan_block}
REVISED SLIDES:
{content_summary}

Produce the JSON verdict only. No commentary outside the JSON."""
        else:
            user_msg = f"""Review cycle {cycle}. Evaluate these slides against the Practicality Mandate content standard.
{plan_block}
SLIDES TO REVIEW:
{content_summary}

Produce the JSON verdict only. No commentary outside the JSON."""

        messages = [{"role": "user", "content": user_msg}]

        # Use built-in web search (version auto-selected for the model)
        tools = [self.web_search_tool(max_uses=25)]

        try:
            final_text, _ = self.run_tool_loop(
                messages=messages,
                tools=tools,
                tool_handlers={},
                max_tokens=24000,
            )
        except Exception:
            response = self.call(messages, max_tokens=24000)
            final_text = text_from_response(response)

        return self._parse_verdict(final_text)

    def review_exercises(self, exercises_content: str, cycle: int = 1) -> dict:
        """Review exercise content for practicality and completeness."""
        user_msg = f"""Review cycle {cycle}. Review these exercises against the Practicality Mandate.

Use web_search to verify every tool name, command, prompt, or API call referenced in the exercises.
If a tool doesn't exist or a command is outdated, flag it.

Exercises come in different types. Apply the correct criteria for each:

**submission** — Step-by-step task with file/screenshot deliverable:
- Names specific, real tools the learner will use
- Steps are concrete and actionable — no vague "explore the tool"
- Answer key has a clear rubric
- All commands, shortcuts, and URLs are current

**roleplay** — Live timed conversation with an AI persona:
- Persona (name, role, personality, context) is realistic and detailed
- Opening message sets up the conversation naturally
- Evaluation criteria are specific and measurable
- Scenario creates genuine conversational pressure

**build_challenge** — Multi-platform integration project:
- Deliverables are concrete and verifiable
- Platforms/tools referenced actually exist and integrate as described
- Challenge requires real cross-platform work, not just one tool
- Answer key covers each deliverable

**troubleshooting** — Diagnose and fix a broken artifact:
- Starter artifact has realistic, non-obvious issues
- Known issues are factually accurate (real error patterns, not invented)
- Diagnostic rubric tests reasoning, not just the fix

**scenario_walkthrough** — Timed branching decisions:
- Each decision has a realistic context and time pressure
- Options are plausible (no obviously wrong throwaway choices)
- Follow-up branches create meaningful consequences
- Evaluation protocol covers the full decision tree

**notebook** — Jupyter notebook coding exercise:
- Starter code is syntactically correct and runnable
- Instructions in markdown cells are clear and specific
- Required libraries are imported in setup cells
- Data generation code works without external files
- Expected outputs are verifiable

**tutorial** — Follow AI-written step-by-step instructions on a real platform:
- Steps reference real, current UI elements (menus, buttons, settings) — verify via web_search
- Platform offers a free tier or free trial (verify the signup URL)
- Steps are granular enough to follow without guessing (exact menu names, not vague directions)
- End goal is concrete and achievable within the steps provided
- Required screenshots are specific enough to verify real completion
- At least one screenshot requires the learner's account identity to be visible

EXERCISES:
{exercises_content}

Produce JSON: {{"status": "approved"|"revise", "feedback": "...", "issues": ["unverified_tool", "stale_command", ...]}}"""

        messages = [{"role": "user", "content": user_msg}]
        tools = [self.web_search_tool(max_uses=10)]

        try:
            final_text, _ = self.run_tool_loop(
                messages=messages,
                tools=tools,
                tool_handlers={},
                max_tokens=2000,
            )
        except Exception:
            response = self.call(messages, max_tokens=2000)
            final_text = text_from_response(response)

        return self._parse_simple_verdict(final_text)

    def _parse_verdict(self, text: str) -> dict:
        try:
            return extract_json(text)
        except (ValueError, json.JSONDecodeError):
            pass
        # Retry once: ask the model to reformat its own review as valid JSON.
        print("[Reviewer] verdict didn't parse — asking model to reformat it...")
        try:
            response = self.call(
                [{"role": "user", "content": (
                    "Convert the following slide-deck review into valid JSON with EXACTLY these keys: "
                    '{"status": "approved" or "revise", "overall_compliance_pct": number 0-1, '
                    '"slides": [{"slide_index": int, "status": "revise", "issues": ["..."], "suggestion": "..."}], '
                    '"global_feedback": "..."}. Output ONLY the JSON.\n\nREVIEW:\n' + text[:20000]
                )}],
                max_tokens=16000,
            )
            retry_text = text_from_response(response)
            return extract_json(retry_text)
        except Exception:
            pass
        # Still unparseable → APPROVE (proceed with the built deck). Defaulting to
        # "revise" here is dangerous: with no flagged slides it forces a full
        # rebuild that discards the deck's visuals. A malformed review must never
        # trigger that — visual QA still runs afterward to catch layout issues.
        print(f"[Reviewer] WARNING: verdict unparseable after retry — APPROVING (not forcing a rebuild). First 200 chars: {text[:200]!r}")
        return {
            "status": "approved",
            "overall_compliance_pct": 1.0,
            "slides": [],
            "global_feedback": "Reviewer response was malformed; proceeding with the current deck.",
        }

    def _parse_simple_verdict(self, text: str) -> dict:
        try:
            return extract_json(text)
        except (ValueError, json.JSONDecodeError):
            print(f"[Reviewer] WARNING: could not parse simple verdict. First 200 chars: {text[:200]!r}")

        if text.strip():
            try:
                response = self.call(
                    [{"role": "user", "content": (
                        "Rewrite the following review verdict as valid JSON with exactly these keys: "
                        '{"status": "approved"|"revise", "feedback": "...", "issues": ["..."]}. '
                        "Output only the JSON, nothing else.\n\nVERDICT:\n" + text[:4000]
                    )}],
                    max_tokens=1000,
                )
                retry_text = text_from_response(response)
                verdict = extract_json(retry_text)
                if verdict.get("status") in ("approved", "revise"):
                    return verdict
            except Exception as e:
                print(f"[Reviewer] WARNING: simple verdict reformat retry failed: {e!r}")

        print("[Reviewer] WARNING: simple verdict unparseable — failing closed to revise")
        return {
            "status": "revise",
            "feedback": "The automated reviewer could not produce a verdict. Re-check the content "
                        "for unverified tools, stale commands, and incorrect answers before approving.",
            "issues": ["malformed_review"],
        }
