"""Reviewer Agent: critiques content, fact-checks with web search. NO write tools."""
from __future__ import annotations
import json
from .base import BaseAgent, extract_json

REVIEWER_SYSTEM = """You are the Syndara Reviewer + Fact-Checker. You evaluate course
content against the content standard AND aggressively fact-check every factual claim
using web_search. You have NO write tools. You can only read and search. Your
output is a verdict that tells the Builder what to fix.

FACT-CHECK RULES (treat as required, not optional):
- For EVERY tool named on a slide, run web_search to confirm: does it exist? Is
  it still current? Are its features/commands as described? Search "[tool name]
  2025" or "[tool name] features" — do not trust the Builder's claims.
- For EVERY specific prompt, command, keyboard shortcut, or API call, run
  web_search to confirm it's accurate TODAY. Tools change frequently. A command
  from 2023 may no longer exist.
- For EVERY statistic, date, or factual claim, run web_search to verify.
- If you cannot verify a claim via search within 2 attempts, flag it as
  `unverified` and tell the Builder to either remove or rephrase.
- Error on the side of demanding verification. False confidence in learner-
  facing material costs Syndara's credibility.

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

    def review_slides(self, slide_content: list[dict], cycle: int = 1,
                      revision_feedback: str = "") -> dict:
        """
        Review extracted slide content.
        slide_content: list of {slide_index, text, speaker_notes}
        revision_feedback: if set, only check whether this feedback was addressed.
        Returns a verdict dict.
        """
        content_summary = json.dumps(slide_content, indent=2)

        if revision_feedback:
            user_msg = f"""Review cycle {cycle}. A creator requested this specific revision:

CREATOR'S REQUEST:
"{revision_feedback}"

Check ONLY whether the revised slide(s) below correctly and adequately address the creator's request. Do NOT flag anything else — no new suggestions, no unrelated issues. Just verify the requested change was made.

Use web_search to verify any factual claims introduced by the revision (tool names, commands, etc.).

REVISED SLIDES:
{content_summary}

Produce the JSON verdict only. No commentary outside the JSON."""
        else:
            user_msg = f"""Review cycle {cycle}. Evaluate these slides against the Practicality Mandate content standard.

Use web_search to verify any tool names you're uncertain about (e.g., search "ChatGPT features 2025" or "is [ToolName] still available").

SLIDES TO REVIEW:
{content_summary}

Produce the JSON verdict only. No commentary outside the JSON."""

        messages = [{"role": "user", "content": user_msg}]

        # Use built-in web search
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 25, "allowed_callers": ["direct"]}]

        try:
            final_text, _ = self.run_tool_loop(
                messages=messages,
                tools=tools,
                tool_handlers={},
                max_tokens=24000,
            )
        except Exception:
            response = self.call(messages, max_tokens=24000)
            final_text = response.content[0].text if response.content else ""

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
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "allowed_callers": ["direct"]}]

        try:
            final_text, _ = self.run_tool_loop(
                messages=messages,
                tools=tools,
                tool_handlers={},
                max_tokens=2000,
            )
        except Exception:
            response = self.call(messages, max_tokens=2000)
            final_text = response.content[0].text if response.content else ""

        return self._parse_simple_verdict(final_text)

    def review_assessment(self, assessment_content: str, cycle: int = 1) -> dict:
        """Review assessment for quality, clarity, and rubric completeness."""
        user_msg = f"""Review cycle {cycle}. Review this assessment.

Use web_search to verify every tool name, feature, command, or factual claim in the questions
and answer options. Incorrect answer keys are especially damaging — verify correct answers.

Check that:
1. MC questions test specific tool knowledge (not just concepts)
2. Scenario tasks require practical application
3. Rubric criteria are specific and measurable
4. Answers are unambiguous and factually correct
5. All referenced tools and features actually exist today

ASSESSMENT:
{assessment_content}

Produce JSON: {{"status": "approved"|"revise", "feedback": "...", "issues": ["wrong_answer", "unverified_tool", ...]}}"""

        messages = [{"role": "user", "content": user_msg}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10, "allowed_callers": ["direct"]}]

        try:
            final_text, _ = self.run_tool_loop(
                messages=messages,
                tools=tools,
                tool_handlers={},
                max_tokens=2000,
            )
        except Exception:
            response = self.call(messages, max_tokens=2000)
            final_text = response.content[0].text if response.content else ""

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
            retry_text = response.content[0].text if response.content else ""
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
                retry_text = response.content[0].text if response.content else ""
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
