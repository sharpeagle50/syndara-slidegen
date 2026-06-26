"""SlidePlannerAgent: deep-research phase that produces the slide-by-slide
content plan for one module.

This agent does the heavy content lifting — it's allowed (and encouraged) to
web-search in a loop for the specific tools, infrastructures, statistics, and
concrete details the slides need. Its output is a markdown document the
creator reviews before the Builder spends 5-10 min rendering the deck.

The output is plain markdown — no JSON schema gymnastics. The downstream
Builder reads it as authoritative content guidance.
"""
from __future__ import annotations
import re
import time

from .base import BaseAgent, STYLE_RULE, strip_em_dashes


SLIDE_PLANNER_SYSTEM = """You are the Syndara Slide Content Researcher.
Your job is to produce the DETAILED CONTENT PLAN for one module's slide deck.
A human creator will review your markdown document and approve it before
another agent renders the actual PPTX — so this is where the research and
specificity happens, not later.

RESEARCH BEHAVIOR
- Do substantial web research on the topic. Keep searching in a loop until
  you have enough concrete, current, verifiable material to write an
  information-dense plan. 5–30 searches per module is the typical range —
  go deep when the topic demands it, stop when returns diminish. Don't
  stop at one.
- Look for: specific tool names (with correct vendor / latest version /
  pricing if relevant), concrete workflows and infrastructure patterns,
  real statistics, case studies, named examples, common failure modes, and
  the exact terminology practitioners use.
- Fetch source pages when a search snippet looks promising but thin.
- IMAGE RESEARCH: When you decide a slide needs a real image (type =
  image/photo/screenshot), find the actual image URL from your research
  sources. The pages you're already reading for content often contain
  the perfect screenshots, product photos, or diagrams. Use web_fetch
  on promising pages to find direct image URLs. Prefer: official vendor
  docs and product pages, press kits, documentation screenshots,
  educational resources, government/public domain images. Avoid images
  with visible watermarks (stock photo previews). Every image MUST be
  cited with attribution in the plan.

SOURCE QUALITY AND FACT-CHECKING (do this as you go, not at the end)
- Prefer primary/reputable sources: official vendor docs, peer-reviewed
  research, government/regulatory bodies, well-known industry publications
  (e.g. Reuters, Bloomberg, Wired, The Verge, Ars Technica, TechCrunch,
  IEEE, HBR, McKinsey, Gartner, Forrester). Use these when available.
- EXCEPTION — primary-source claims don't need cross-verification: if you
  are describing how vendor X's product works and citing vendor X's own
  docs, that's authoritative. Same for an org's own announcement about
  itself. Don't waste searches cross-verifying first-party claims.
- OTHERWISE — for any non-obvious factual claim sourced from a blog,
  marketing page, Medium post, Reddit thread, unknown publication, or
  any low-signal source: cross-verify. Either
    (a) find at least one INDEPENDENT reputable source that says the same
        thing, or
    (b) investigate the original source's authorship/credibility enough
        to decide it's trustworthy (author expertise, publication track
        record, citations it carries).
  If you can't cross-verify and the source is weak, either DROP the claim
  or mark it inline as "[unverified: only seen on {url}]" so the creator
  knows.
- CURRENCY CHECK — the current date is provided in the user message. Use
  it to calibrate. Strongly prefer sources from the last 3 months for
  anything involving AI tools, pricing, features, or fast-moving industry
  facts. For each concrete product/feature/stat you cite, do a quick
  follow-up search to confirm nothing has superseded it (e.g. "cursor
  pricing [current month year]", "claude code features latest"). If you
  find a newer authoritative source, use that one instead and discard
  the older one. Timeless material (definitions, concepts, established
  workflows) is exempt but should still be from a credible source.
- TOOL COVERAGE IS ABOUT EFFICACY AND ADOPTION, NOT AGE. Established
  industry standards (even years old) get full coverage — verify they're
  still current with recent sources. New tools with real traction and
  proven results also get coverage. Hype-only tools (blog buzz, no real
  adoption) get a brief mention at most. When a newer tool is challenging
  an established one — real practitioners switching, adoption growing —
  cover both and explain what's driving the shift.
- Cite every non-obvious fact. Inline citations like `[source: vendor.com/page]`
  or `[source: Gartner 2025 report]` so the creator can sanity-check.
  When you've cross-verified a shaky source, cite BOTH the original and
  the confirming source: `[source: blog.com/post; confirmed: reuters.com/article]`.

OUTPUT FORMAT (strict — follow this EXACTLY, field by field)
Return ONLY markdown. No JSON, no code fences around the whole thing, no
preamble. Every slide MUST have ALL sections below, in this order, with
these exact headings. Do not skip or reorder any section.

CRITICAL: Your complete slide plan MUST be output as direct text in your
response. Do NOT compose, store, or validate the plan inside code
execution. Code execution is only for filtering search results. Your
final text output must contain the full markdown plan — if it only
contains a summary or narration, the downstream system cannot parse it.

```
# <Module title>

**Overall flow:** 1–2 sentences on the narrative arc of the module.

---

## Slide 1 — <slide title>

**Layout:** <one of: title_slide | bullet_slide | comparison_slide |
stats_slide | steps_slide | chart_slide | flowchart_slide | image_slide |
summary_slide | section_divider | agenda_slide | quote_slide>

**Visual elements** (what the learner SEES as the primary visual):
- **Type:** <exactly one of: none | chart | flowchart | sequence_diagram |
  er_diagram | architecture_diagram | image | photo | icon_set |
  screenshot | table>
- **Detailed description:** <REQUIRED if type is not 'none'. Describe the
  visual with enough specificity that a designer could build it without
  guessing. Include: all node/label text, arrow directions, data values,
  axis labels, color coding, column headers, or image subject matter.
  Example: "Bar chart: X-axis = 'Copilot', 'Cursor', 'Claude Code';
  Y-axis = monthly cost in USD; values = $19, $20, $0 (free tier).
  Highlight Claude Code bar in green.">
- **Image URL:** <REQUIRED if type is image/photo/screenshot. Direct URL
  to the image file you found during research. Must be an actual image
  URL (ending in .png, .jpg, .jpeg, .gif, .webp, or from a page you
  fetched and confirmed contains the image). Write 'N/A' for other types.>
- **Image search query:** <Fallback search query if the URL breaks. Write
  a descriptive query like "Claude Code terminal interface screenshot".
  Write 'N/A' for non-image types.>
- **Data source:** <URL or citation for any real data shown. Write 'N/A'
  if the visual is conceptual or type is 'none'.>
- **Attribution:** <REQUIRED if type is image/photo/screenshot. Credit
  the source: "Anthropic — official documentation" or "NASA/JPL —
  public domain" or "React docs — Meta Open Source". Write 'N/A' for
  other types.>
- **Teaching purpose:** <One sentence: what does this visual teach that
  text alone cannot? Write 'N/A' if type is 'none'.>

**On-slide text** (COMPLETE list of every word visible on the slide —
MAXIMUM {max_words_per_slide} words total across ALL items combined):
- <exact text item 1 — e.g. "3 AI coding assistants compared">
- <exact text item 2 — e.g. "$0/mo free tier">
- <exact text item 3 — e.g. "Context-aware completions">
(List every text element: title, subtitle, bullet, label, callout, stat.
If a visual has labels, those count toward the {max_words_per_slide}-word limit. 2–4 items
typical. NO item longer than 6 words. NO full sentences.)

**Speaker notes** (up to 300 words of narration read aloud by TTS —
no minimum, write what feels appropriate for the slide. A quick visual
slide might only need 10–20 words; a dense concept slide might need 250):
<Full conversational prose. This IS the teaching content. Write as spoken
language — use "you", "we", "let's". Include: concrete explanations,
real examples, step-by-step tool walkthroughs, exact commands/prompts,
transitions to the next slide, why this matters, common mistakes.
Cite every non-obvious fact inline: `[source: url-or-name]`.>

**Sources:**
- <url 1>
- <url 2>

---

## Slide 2 — <slide title>
(repeat ALL sections above in the same order)
```

CONTENT PHILOSOPHY (critical — the next agent reads this)
- The SLIDE shows MAXIMUM 20 WORDS of text. That's it. Key phrases, big
  numbers, short labels — NOT sentences. If a slide has more than 3 short
  bullets or any bullet longer than 6 words, you're doing it wrong. Push
  ALL depth into speaker notes. The slide is a visual anchor, not a document.
- NEVER use emoji characters (🔒 🔍 💡 ⚡ etc.) anywhere in on-slide text,
  speaker notes, or bullet points. They look unprofessional in presentation
  slides. Use plain words only.
- The SPEAKER NOTES are the CORE TEACHING CONTENT. This is where the real
  course lives. Write them as full, in-depth narration — the kind a skilled
  instructor would deliver out loud while pointing at the slide. Include:
    * Concrete explanations of every concept on the slide
    * Real-world examples, anecdotes, and case studies
    * Step-by-step walkthroughs of how to use specific tools
    * Exact commands, prompts, or workflows the learner would follow
    * Transitions that connect this slide to the previous and next one
    * Context the learner needs to understand WHY something matters
    * Common mistakes and how to avoid them
  Speaker notes: up to 300 words per slide, no minimum — match the length
  to the slide's complexity. A title slide or quick visual might only need
  10–20 words; a concept-heavy slide might need 250. They will be read
  aloud by a TTS narrator — write conversationally, not as an essay.
  Use "you" and "we". Pause for emphasis with short sentences.
- VISUALS ARE THE SLIDE. Every content slide should have a visual as its
  primary element — the text is secondary. Favor visuals wherever they add
  real information: flowcharts for processes, charts for comparisons / stats,
  diagrams for architectures, images for illustrative concepts. A flowchart
  of a workflow beats 4 bullets describing the same workflow.
- Pick the RIGHT visual type for the content — these map 1:1 to the tools
  the Builder will use:
    * chart → bar/line/pie/scatter for quantitative comparisons, stats, trends
    * flowchart → step-by-step workflows, decision trees, if/then logic
    * sequence_diagram → interactions between actors/systems over time
    * er_diagram → entities and relationships (databases, schemas)
    * architecture_diagram → system/software components and how they connect
    * image / photo / screenshot → when a REAL image from the web is the
      best teaching tool. Use this ONLY when no generated diagram can
      substitute: actual tool UI screenshots, photos of physical objects
      (anatomy, hardware, lab equipment), satellite imagery, complex
      real-world visualizations, or premade infographics/graphs you found
      during research that are more accurate than anything you could
      describe for the builder to generate. Do NOT overuse — most slides
      should use generated visuals (charts, flowcharts, diagrams). Reserve
      real images for cases where authenticity, visual complexity, or
      recognition matters. When you use this type, you MUST provide an
      Image URL from your research and an Attribution.
    * icon_set → 3–5 conceptual icons with labels (e.g. tool-category tiles)
    * table → small structured comparison (only if chart/diagram doesn't fit)
- Aim for 70%+ of content slides to carry a real visual (not just the
  title/summary slides). Text-only bullet slides should be rare exceptions.
- Do NOT invent data. If a chart is warranted, describe exactly what data it
  should plot and where the data comes from (cite the source). If you can't
  find real data, pick a different visual type or different layout.
- Name real tools by name with correct vendor. Never say "an AI tool" —
  say "Cursor" or "GitHub Copilot" or "Claude Code."
- Quick demos are welcome (e.g. "run this snippet and see the output," "visit this tool's demo page") — but don't include full hands-on exercises or multi-step tasks that require the learner to produce work; a dedicated exercise follows the slides.
- EXERCISES ARE COMPLETELY SEPARATE FROM SLIDES. A dedicated interactive exercise
  is generated by a different system after the slides are built. NEVER include an
  "exercise slide", "practice activity slide", or "hands-on slide" in your plan.
  Your job is ONLY the teaching slides. The exercise system handles all practice
  and assessment independently.
- SLIDES TEACH, EXERCISES PRACTICE — Your slides should teach concepts,
  techniques, and real tool workflows — including how to set up and use specific
  AI tools (ChatGPT Voice Mode, Claude, etc.) that learners will use independently
  after the course. But don't turn slides into full practice sessions (e.g. "now
  open ChatGPT and do 3 rounds of paraphrasing"). Teach the tool, show the
  workflow, give example prompts — the separate exercise handles practice.

LAYOUT PALETTE (pick the best fit per slide)
  title_slide, section_divider, agenda_slide, bullet_slide,
  comparison_slide, stats_slide, quote_slide, steps_slide, summary_slide,
  chart_slide, flowchart_slide, image_slide, question_slide

HEURISTICS
- Open with title_slide. Close with summary_slide. Don't pad.
- If you include a references/citations slide at the end, its speaker notes
  must be ONE short sign-off sentence like "These are the references used in
  this module — thank you for listening!". NEVER read URLs, citations, or
  source names aloud in the narration.
- Vary layouts — don't let every middle slide be bullet_slide.
- The prompt gives a slide-count RANGE. Use however many slides within that
  range best fit the material — but never go below the minimum or above the
  maximum. More focused slides are better than fewer overloaded ones.
- Put comprehension questions only where they genuinely check understanding,
  at most twice per module. Mark them with **Layout:** `question_slide`.
  For question slides, use this structure:
    - **On-slide text**: The question + 3-4 answer options (labeled A, B, C, D)
    - **Speaker notes**: The correct answer + brief explanation of WHY it's correct
  The builder will automatically split each question_slide into TWO physical
  slides: one showing only the question + options (no answer highlighted),
  and one showing the same content with the correct answer revealed. This
  creates a click-to-reveal animation in the downloadable PPTX.
"""


def slide_range_for(target: int) -> tuple[int, int]:
    """Map a target slide count to its deck-length band.

    Decks come in three creator-facing lengths; the planner has discretion
    WITHIN the band but must never leave it:
      short  1–20 · medium 20–40 · long 40–60
    """
    if target <= 20:
        return (1, 20)
    if target <= 40:
        return (20, 40)
    return (40, 60)


class SlidePlannerAgent(BaseAgent):
    # Research loop needs web tools — that's the whole point of this stage.
    allowed_tool_names = ["web_search", "web_fetch"]
    system_prompt = SLIDE_PLANNER_SYSTEM + STYLE_RULE

    def _apply_max_words(self, max_words: int):
        self.system_prompt = SLIDE_PLANNER_SYSTEM.replace(
            "{max_words_per_slide}", str(max_words)
        ) + STYLE_RULE

    def plan(
        self,
        outline: dict,
        style: str = "syndara",
        previous_plan: dict | None = None,
        feedback: str = "",
        assessment_questions: list[dict] | None = None,
        web_images: bool = False,
        max_questions: int = 2,
        available_tools: list[str] | None = None,
    ) -> dict:
        """Return a slide-by-slide content plan (markdown) for the given outline.

        max_questions caps in-slide comprehension question_slides (0 = none).
        """
        self._apply_max_words(outline.get("max_words_per_slide") or 20)
        import json as _j
        is_revise = bool(previous_plan and feedback)
        mod_pos = outline.get("module_position") or outline.get("module_id") or "?"
        outline_slide_count = len(outline.get("slides", []))
        print(
            f"[SlidePlannerAgent] START module={mod_pos} "
            f"mode={'revise' if is_revise else 'fresh'} "
            f"outline_slides={outline_slide_count} style={style}"
        )
        if is_revise:
            print(f"[SlidePlannerAgent] creator feedback ({len(feedback)} chars): {feedback[:500]!r}")

        max_words_per_slide = outline.get("max_words_per_slide") or 20
        explicit_target = outline.get("slide_count")
        target_slides = explicit_target or max(outline_slide_count, 40)
        slide_lo, slide_hi = slide_range_for(target_slides)
        max_questions = max(0, int(max_questions))
        if max_questions <= 0:
            questions_directive = (
                "IN-SLIDE QUESTIONS: Do NOT include any comprehension/question "
                "slides. Never use **Layout:** `question_slide`."
            )
        else:
            questions_directive = (
                f"IN-SLIDE QUESTIONS: Include AT MOST {max_questions} comprehension "
                f"question slide(s) — only where they genuinely check understanding, "
                f"never as filler. Fewer is fine. Mark each with **Layout:** "
                f"`question_slide`."
            )
        tools_clean = [t.strip() for t in (available_tools or []) if t and t.strip()]
        if tools_clean:
            tools_directive = (
                f"LEARNER TOOL ACCESS: Your learners have access to these paid tools: "
                f"{', '.join(tools_clean)}. When slides demonstrate tools or workflows, "
                f"prefer these and assume learners can use them. Do not instruct learners to "
                f"buy or sign up for tools outside this list."
            )
        else:
            tools_directive = ""
        from datetime import date
        today = date.today().strftime("%B %d, %Y")
        user_msg = f"""Today's date is {today}. Use this to calibrate your research —
search for the most current state of this field.

Research and produce the detailed content plan for this module.

MODULE INFO:
- Title: {outline.get('title', '?')}
- Summary: {outline.get('summary', '')}
- Position: {mod_pos}
- Module ID: {outline.get('module_id', '?')}

STYLE: {style}

You are the SOLE researcher and designer for this module. There is no
prior outline — you start from the title and summary above. Research the
topic thoroughly, then design the slide-by-slide plan based on what you
find.

DECK LENGTH: plan between {slide_lo} and {slide_hi} slides (inclusive).
You have full discretion WITHIN that range — use however many slides best
fit the material — but NEVER go outside it. Open with a title_slide and
close with a summary_slide.

{questions_directive}

{tools_directive}

CRITICAL — FOLLOW THE OUTPUT FORMAT EXACTLY:
Every slide MUST have ALL of these sections in this exact order:
1. **Layout** — one layout type from the palette
2. **Visual elements** — Type, Detailed description, Data source, Teaching purpose
3. **On-slide text** — bullet list of EVERY visible word (max {max_words_per_slide} words total)
4. **Speaker notes** — up to 300 words of TTS narration (the real teaching content)
5. **Sources** — URLs used for this slide

ON-SLIDE TEXT: List the EXACT text that appears on the slide. Max {max_words_per_slide} words
total. No item longer than 6 words. No full sentences. If you can replace
text with a chart, flowchart, or diagram, do it.

SPEAKER NOTES: Up to 300 words each, no minimum — match length to the
slide. A quick visual might only need 10–20 words of narration; a dense
concept slide might need 250. Write conversationally ("you", "we",
"let's"), include concrete examples, step-by-step walkthroughs,
tool-specific commands, and smooth transitions. The speaker notes ARE
the course.

VISUAL ELEMENTS: Every content slide should have a real visual. Describe
it with enough detail that a designer could build it without guessing —
include all labels, data values, node text, arrow directions, colors.
"""
        if web_images:
            user_msg += """
WEB IMAGES ENABLED: You may use real images from the web for slides where
a generated diagram cannot substitute. When you set Visual type to
image/photo/screenshot, you MUST:
1. Find the actual image URL from pages you research (official docs,
   product pages, press materials, educational resources)
2. Provide the direct URL in **Image URL:**
3. Cite the source in **Attribution:**
4. Provide a fallback search query in **Image search query:**

Use this SELECTIVELY — most slides should still use generated visuals
(charts, flowcharts, diagrams). Reserve real images for: tool UI
screenshots, photos of physical objects, complex real-world visuals,
or premade graphs/infographics that are more accurate than anything
the builder could generate. Avoid watermarked stock photo previews.
"""
        else:
            user_msg += """
NOTE: Web image insertion is not enabled for this course. Use only
generated visuals (charts, flowcharts, diagrams, icon sets). If you
specify type image/photo/screenshot, describe what the visual should
depict and the builder will create a suitable alternative diagram.
"""
        if assessment_questions:
            domains = set()
            question_texts = []
            for q in assessment_questions:
                if q.get("domain"):
                    domains.add(q["domain"])
                question_texts.append(q.get("text", ""))
            domain_list = ", ".join(sorted(domains)) if domains else "general competency"
            user_msg += (
                f"\nINTERNAL KPIs (DO NOT mention these to learners — they silently "
                f"guide your content depth decisions):\n"
                f"The creator has pre/post assessment questions across: {domain_list}. "
                f"Ensure this module's slides and speaker notes naturally build "
                f"competence in these areas where relevant. Never reference the "
                f"assessment, survey, or KPIs in slide content or speaker notes.\n"
            )

        if is_revise:
            prev_md = previous_plan.get("markdown", "") if isinstance(previous_plan, dict) else str(previous_plan)
            user_msg += (
                "\n\nPREVIOUS PLAN (creator rejected — address their feedback):\n\n"
                f"{prev_md}\n\n"
                f"CREATOR FEEDBACK:\n{feedback}\n\n"
                "Revise the plan accordingly. Keep the parts the creator didn't object to.\n\n"
                "Only do new web research if the feedback asks for something that requires "
                "new information (e.g., replacing a tool, adding a topic, verifying a claim). "
                "For structural changes (reordering slides, rewording notes, changing layouts), "
                "just make the edit directly without searching."
            )

        tools = [
            self.web_search_tool(max_uses=30),
            self.web_fetch_tool(max_uses=15),
        ]

        t0 = time.time()
        try:
            # run_tool_loop returns (final_text, updated_messages_list).
            # The iteration count we want is roughly half the len of that list
            # (one user tool_result turn per assistant tool_use turn) but we
            # don't actually need it for correctness — just a rough stat.
            final_text, msgs = self.run_tool_loop(
                messages=[{"role": "user", "content": user_msg}],
                tools=tools,
                tool_handlers={},
                max_tokens=128000,
                trace_label=f"SlidePlanner.module{mod_pos}",
            )
        except Exception as e:
            latency = time.time() - t0
            print(f"[SlidePlannerAgent] tool_loop EXCEPTION module={mod_pos} latency={latency:.1f}s: {e!r}")
            return self._error_stub(mod_pos, f"tool_loop exception: {e}", "")

        latency = time.time() - t0
        iterations = max(1, (len(msgs) - 1) // 2) if isinstance(msgs, list) else 1
        md = (final_text or "").strip()
        # Strip a stray outer fenced block if the model wraps the whole thing
        if md.startswith("```") and md.endswith("```"):
            md = md.split("\n", 1)[1] if "\n" in md else md
            md = md.rsplit("```", 1)[0].rstrip()

        print(
            f"[SlidePlannerAgent] DONE module={mod_pos} "
            f"iterations={iterations} markdown_chars={len(md)} "
            f"latency={latency:.1f}s",
            flush=True,
        )
        print(f"[SlidePlannerAgent] markdown first 500 chars: {md[:500]!r}", flush=True)

        if not md or len(md) < 100:
            return self._error_stub(mod_pos, "planner returned empty or near-empty markdown", md)

        # Enforce the deck-length band: one corrective retry if the plan
        # landed outside it, then accept whatever we have (warn, don't fail).
        planned = len(re.findall(r"^##\s+Slide\s+\d+", md, re.MULTILINE))
        if planned and not (slide_lo <= planned <= slide_hi) and isinstance(msgs, list):
            print(
                f"[SlidePlannerAgent] plan has {planned} slides — outside the "
                f"{slide_lo}–{slide_hi} band, requesting correction",
                flush=True,
            )
            fix_msg = (
                f"Your plan has {planned} slides, but it must have between "
                f"{slide_lo} and {slide_hi} slides (inclusive). "
                f"{'Trim or merge slides' if planned > slide_hi else 'Expand the material with additional slides'} "
                f"to fit the range, then output the COMPLETE corrected plan in the same format. "
                f"Do not do any new web research."
            )
            try:
                fixed_text, _ = self.run_tool_loop(
                    messages=msgs + [{"role": "user", "content": fix_msg}],
                    tools=tools,
                    tool_handlers={},
                    max_tokens=128000,
                    trace_label=f"SlidePlanner.module{mod_pos}.bandfix",
                )
                fixed_md = (fixed_text or "").strip()
                if fixed_md.startswith("```") and fixed_md.endswith("```"):
                    fixed_md = fixed_md.split("\n", 1)[1] if "\n" in fixed_md else fixed_md
                    fixed_md = fixed_md.rsplit("```", 1)[0].rstrip()
                fixed_count = len(re.findall(r"^##\s+Slide\s+\d+", fixed_md, re.MULTILINE))
                if fixed_count and len(fixed_md) >= 100:
                    md = fixed_md
                    planned = fixed_count
            except Exception as e:
                print(f"[SlidePlannerAgent] band correction failed ({e}) — keeping original plan", flush=True)
            if not (slide_lo <= planned <= slide_hi):
                print(
                    f"[SlidePlannerAgent] ⚠ plan still has {planned} slides "
                    f"(band {slide_lo}–{slide_hi}) — proceeding anyway",
                    flush=True,
                )

        # Enforce the in-slide question cap: one corrective retry if the plan
        # exceeds max_questions (matters most for max_questions=0 → none).
        def _count_questions(text: str) -> int:
            # Anchored to the Layout marker (won't match prose), but tolerant of
            # the colon, an em/en-dash separator, backticks, and spacing.
            return len(re.findall(r"\*\*Layout:?\*\*\s*[—–-]?\s*`?\s*question_slide", text, re.IGNORECASE))
        q_count = _count_questions(md)
        if q_count > max_questions and isinstance(msgs, list):
            print(
                f"[SlidePlannerAgent] plan has {q_count} question slide(s) — over "
                f"the cap of {max_questions}, requesting correction",
                flush=True,
            )
            limit_txt = ("Remove ALL question_slide slides — this deck must have none."
                         if max_questions == 0 else
                         f"Keep at most {max_questions} question_slide slide(s); convert or remove the rest.")
            qfix = (
                f"Your plan has {q_count} question_slide slides, which exceeds the limit. "
                f"{limit_txt} Output the COMPLETE corrected plan in the same format. "
                f"Do not do any new web research."
            )
            try:
                qtext, _ = self.run_tool_loop(
                    messages=msgs + [{"role": "user", "content": qfix}],
                    tools=tools,
                    tool_handlers={},
                    max_tokens=128000,
                    trace_label=f"SlidePlanner.module{mod_pos}.qfix",
                )
                qmd = (qtext or "").strip()
                if qmd.startswith("```") and qmd.endswith("```"):
                    qmd = qmd.split("\n", 1)[1] if "\n" in qmd else qmd
                    qmd = qmd.rsplit("```", 1)[0].rstrip()
                if len(qmd) >= 100 and _count_questions(qmd) <= q_count:
                    md = qmd
            except Exception as e:
                print(f"[SlidePlannerAgent] question-cap correction failed ({e}) — keeping plan", flush=True)
            final_q = _count_questions(md)
            if final_q > max_questions:
                print(
                    f"[SlidePlannerAgent] ⚠ plan still has {final_q} question slide(s) "
                    f"(cap {max_questions}) — proceeding anyway",
                    flush=True,
                )

        sources = self._extract_sources(md)
        image_urls = SlidePlannerAgent._extract_image_urls(md) if web_images else []
        if image_urls:
            print(f"[SlidePlannerAgent] found {len(image_urls)} web image references", flush=True)
        return {
            "markdown": strip_em_dashes(md),
            "sources": sources,
            "image_urls": image_urls,
            "module_position": mod_pos,
            "_iterations": iterations,
            "_latency_seconds": round(latency, 1),
        }

    @staticmethod
    def _extract_image_urls(md: str) -> list[dict]:
        import re
        entries = []
        headings = re.findall(r'^(## Slide \d+[^\n]*)', md, flags=re.MULTILINE)
        sections = re.split(r'^## Slide \d+', md, flags=re.MULTILINE)[1:]
        for heading, section in zip(headings, sections):
            type_match = re.search(r'\*\*Type:\*\*\s*(image|photo|screenshot)', section, re.IGNORECASE)
            if not type_match:
                continue
            url_match = re.search(r'\*\*Image URL:\*\*\s*(https?://\S+)', section)
            url = url_match.group(1).rstrip(".,);") if url_match else ""
            if url.lower() == "n/a":
                url = ""
            query_match = re.search(r'\*\*Image search query:\*\*\s*"?([^"\n]+)"?', section)
            query = query_match.group(1).strip() if query_match else ""
            if query.lower() == "n/a":
                query = ""
            attr_match = re.search(r'\*\*Attribution:\*\*\s*([^\n]+)', section)
            attribution = attr_match.group(1).strip() if attr_match else ""
            if attribution.lower() == "n/a":
                attribution = ""
            if url or query:
                entries.append({
                    "slide_heading": heading.strip(),
                    "image_url": url,
                    "search_query": query,
                    "attribution": attribution,
                })
        return entries

    @staticmethod
    def extract_slide_intents(md: str) -> dict:
        """Parse the plan markdown into {0-based RENDERED slide index: intent string}
        describing what each slide was PLANNED to show, so Visual QA can verify the rendered
        visual against the plan. Text-only slides (Type 'none') are flagged so QA skips the
        visual-accuracy check on them.

        Keyed by RENDERED slide order, NOT plan order: the builder splits each question_slide
        (**Layout:** question_slide) into TWO physical slides — the question, then the same
        content with the answer revealed — so a quiz slide's intent is emitted twice to keep
        indices aligned with the rendered deck Visual QA actually inspects. (Without this,
        every slide after the first quiz would be compared against the wrong slide's intent.)
        Best-effort — returns {} if the plan can't be parsed."""
        import re
        if not md:
            return {}
        try:
            headings = re.findall(r'^##\s+Slide\s+\d+\s*[—–-]?\s*([^\n]*)', md, flags=re.MULTILINE)
            sections = re.split(r'^##\s+Slide\s+\d+', md, flags=re.MULTILINE)[1:]
            intents: dict = {}
            render_idx = 0
            for i, section in enumerate(sections):
                title = (headings[i] if i < len(headings) else "").strip("—– \t")
                tmatch = re.search(r'\*\*Type:\*\*\s*([A-Za-z_]+)', section)
                vtype = (tmatch.group(1).strip().lower() if tmatch else "none")
                if not vtype or vtype == "none":
                    intent = (f'Planned slide title: "{title}". This is a TEXT-ONLY slide '
                              "with no primary visual — do NOT apply the visual-accuracy "
                              "check to it.")
                else:
                    dmatch = re.search(
                        r'\*\*Detailed description:\*\*\s*(.+?)(?:\n\s*-\s*\*\*|\n\n|\Z)', section, re.S
                    )
                    visual = re.sub(r'\s+', ' ', dmatch.group(1)).strip() if dmatch else ""
                    if visual:
                        intent = f'Planned slide title: "{title}". Planned visual ({vtype}): {visual}'
                    else:
                        intent = f'Planned slide title: "{title}". Planned visual type: {vtype}.'
                intents[render_idx] = intent
                render_idx += 1
                # A question_slide renders as two physical slides (question + answer reveal);
                # both show the same planned subject, so duplicate the intent to stay aligned.
                if re.search(r'\*\*Layout:\*\*\s*`?\s*question_slide', section, re.I):
                    intents[render_idx] = intent
                    render_idx += 1
            return intents
        except Exception:
            return {}

    @staticmethod
    def _extract_sources(md: str) -> list[str]:
        import re
        urls = re.findall(r"https?://\S+?(?=[)\s\],]|$)", md)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            u = u.rstrip(".,);")
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out[:40]

    @staticmethod
    def _error_stub(mod_pos, error: str, excerpt: str) -> dict:
        print(f"[SlidePlannerAgent] GIVING UP module={mod_pos} — returning error stub: {error}")
        return {
            "markdown": "",
            "sources": [],
            "module_position": mod_pos,
            "_parse_error": error[:500],
            "_raw_response_excerpt": (excerpt or "")[:2000],
        }
