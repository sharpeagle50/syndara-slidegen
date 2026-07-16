"""RedesignPlannerAgent: analyzes an existing slide deck and produces a
slide plan with improved visual layouts while preserving exact content.

Unlike SlidePlannerAgent, this agent does NO web research. It receives
the extracted text, speaker notes, embedded images, and PNG snapshots
from the source deck, then outputs a standard slide plan markdown that
the Builder can consume directly.
"""
from __future__ import annotations
import base64
import time

from .base import BaseAgent, visual_directive


REDESIGN_PLANNER_SYSTEM = """You are the Syndara Slide Redesign Planner.

You receive an existing slide deck's content (text, speaker notes, embedded
images, and visual snapshots of every slide). Your job is to produce a
VISUAL REDESIGN PLAN — same content, better presentation.

ABSOLUTE RULES:
1. PRESERVE ALL ON-SLIDE TEXT EXACTLY. Every word of visible text from the
   source deck must appear in your plan. Do not rephrase, reword, add, or
   remove any on-slide text. You may reorganize how it's displayed (e.g.
   bullets → table cells, flat list → comparison columns) but the WORDS
   must be identical.
2. PRESERVE SLIDE ORDER. Do not reorder, add, or remove slides. Slide 1
   stays Slide 1. The output must have exactly the same number of slides
   as the input.
3. GENERATE FRESH SPEAKER NOTES for every slide. Write full TTS-ready
   narration (up to 300 words, conversational tone — use "you", "we",
   "let's"). If the source slide has existing speaker notes, use them as
   guidance and inspiration for the narration content. If not, write
   narration based on the on-slide text. Either way, produce polished
   output suitable for audio playback.
   EXCEPTION — references/citations slides: notes must be ONE short
   sign-off sentence like "These are the references used in this module —
   thank you for listening!". Never read URLs, citations, or source names
   aloud.
4. IMPROVE VISUAL LAYOUTS. This is your primary job. For each slide:
   - Choose the best layout type from the palette
   - Decide if the content would be better presented as a chart, table,
     flowchart, comparison, stats display, or diagram instead of bullets
   - Describe the visual elements in detail
   - If the slide already has a good layout, keep it (don't change for
     the sake of changing)
5. HANDLE SOURCE IMAGES. Some slides have embedded images (photos,
   screenshots, logos). For these:
   - Set Visual type to "image"
   - The builder will insert the original image on the redesigned slide
   - You can still improve the layout AROUND the image

DO NOT:
- Do any web research (you have no tools)
- Invent new content or data
- Add slides or remove slides
- Change the order of slides
- Rephrase on-slide text

OUTPUT FORMAT (strict — follow EXACTLY, field by field):
Return ONLY markdown. No JSON, no code fences around the whole thing.

```
# <Deck title (from slide 1)>

**Overall flow:** 1–2 sentences describing the deck's narrative arc.

---

## Slide 1 — <slide title from source>

**Layout:** <one of: title_slide | bullet_slide | comparison_slide |
stats_slide | steps_slide | chart_slide | flowchart_slide | image_slide |
summary_slide | section_divider | agenda_slide | quote_slide>

**Visual elements** (what the learner SEES as the primary visual):
- **Type:** <exactly one of: none | chart | flowchart | sequence_diagram |
  er_diagram | architecture_diagram | image | icon_set | table>
- **Detailed description:** <REQUIRED if type is not 'none'. Describe the
  visual with enough specificity that a designer could build it without
  guessing. For image type with source images, describe how to
  position/size it.>
- **Image URL:** <Write 'N/A' — source images are provided separately.>
- **Image search query:** <Write 'N/A' for redesign.>
- **Data source:** <'From source deck' for all slides — EXCEPT a slide that carries an
  approved content update supplied with a [source: URL] below: put that URL here so it
  prints on the slide as the fact's citation. Write 'N/A' if type is 'none' AND the slide
  has no approved-update source.>
- **Attribution:** <Write 'N/A' for redesign.>
- **Teaching purpose:** <One sentence: what does this visual teach that
  text alone cannot? Write 'N/A' if type is 'none'.>

**On-slide text** (EXACT text from the source slide — same words, kept
concise; preserve what the source communicates, don't pad):
- <exact text item 1>
- <exact text item 2>

**Speaker notes** (up to 300 words of TTS narration — always generate
fresh, polished narration):
<Full conversational prose for audio playback.>

**Sources:**
- From source deck

---

## Slide 2 — <slide title>
(repeat ALL sections above)
```

LAYOUT PALETTE (pick the best fit per slide):
  title_slide, section_divider, agenda_slide, bullet_slide,
  comparison_slide, stats_slide, quote_slide, steps_slide, summary_slide,
  chart_slide, flowchart_slide, image_slide, question_slide

REDESIGN HEURISTICS:
- If a slide has 4+ bullets that represent a sequence, use steps_slide
  with a flowchart or process visual
- If a slide compares 2+ items side by side, use comparison_slide
- If a slide has numbers/statistics, use stats_slide with a chart
- If a slide has a single key quote or statement, use quote_slide
- If a slide already uses a good layout, don't change it
- Vary layouts — consecutive slides should rarely share the same layout
- The goal is VISUAL IMPROVEMENT, not radical restructuring
"""


class RedesignPlannerAgent(BaseAgent):
    allowed_tool_names = []
    system_prompt = REDESIGN_PLANNER_SYSTEM

    def _apply_max_words(self, max_words: int):
        self.system_prompt = REDESIGN_PLANNER_SYSTEM.replace(
            "{max_words_per_slide}", str(max_words)
        )

    def plan(
        self,
        extracted_content: list[dict],
        style: str = "professional",
        source_slide_pngs: list[str] | None = None,
        max_words_per_slide: int = 20,
        previous_plan: dict | None = None,
        feedback: str = "",
        content_deltas: list[dict] | None = None,
        creator_guidance: str = "",
        added_sources: list[str] | None = None,
        visual_level=None,
    ) -> dict:
        """Produce a redesign slide plan from extracted deck content."""
        self._apply_max_words(max_words_per_slide)
        is_revise = bool(previous_plan and feedback)
        num_slides = len(extracted_content)

        print(
            f"[RedesignPlannerAgent] START slides={num_slides} "
            f"mode={'revise' if is_revise else 'fresh'} style={style}"
        )
        if is_revise:
            print(f"[RedesignPlannerAgent] creator feedback ({len(feedback)} chars): {feedback[:500]!r}")

        content_block = []
        for slide in extracted_content:
            idx = slide["slide_index"]
            text = slide.get("text", "")
            notes = slide.get("speaker_notes", "")
            images = slide.get("embedded_images", [])

            block = f"### Source Slide {idx + 1}\n"
            block += f"**Visible text:**\n{text}\n\n"
            if notes:
                block += f"**Existing speaker notes (use as guidance):**\n{notes}\n\n"
            else:
                block += "**Existing speaker notes:** (none — generate fresh narration)\n\n"
            if images:
                for img in images:
                    block += (
                        f"**Embedded image:** {img['path']} "
                        f"({img.get('width_px', '?')}x{img.get('height_px', '?')}px, "
                        f"{img.get('content_type', 'unknown')})\n"
                    )
            block += "---\n"
            content_block.append(block)

        user_content = []

        # Add slide PNG snapshots as vision input
        if source_slide_pngs:
            user_content.append({
                "type": "text",
                "text": (
                    "Below are PNG screenshots of every slide in the source deck. "
                    "Use these to understand the current visual layout and identify "
                    "what needs improvement.\n"
                ),
            })
            for png_path in source_slide_pngs:
                try:
                    with open(png_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    user_content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    })
                except Exception:
                    continue

        user_text = f"""Redesign this {num_slides}-slide deck with improved visuals.

TARGET STYLE: {style}

EXTRACTED CONTENT FROM SOURCE DECK:

{"".join(content_block)}

INSTRUCTIONS:
- Produce a redesign plan for ALL {num_slides} slides
- Preserve all on-slide text EXACTLY as shown above
- Generate fresh, polished speaker notes for every slide (use existing
  notes as guidance where available)
- Choose better layouts and visual elements where the current design
  is weak (e.g. text-heavy bullets → charts, tables, flowcharts)
- For slides with embedded images, use Visual type "image"
- Keep on-slide text concise and legible — don't cram; a text-forward slide may carry more than a visual one
- Overall layout lean for this redesign: {visual_directive(visual_level)}
"""

        _delta_sources: list[str] = []
        if content_deltas:
            _dl = []
            for d in content_deltas:
                si = int(d.get("slide_index", 0) or 0) + 1
                if d.get("kind") == "remove":
                    _dl.append(f'- Slide {si}: REMOVE this outdated content: "{d.get("original", "")}"')
                else:
                    _src = (d.get("source") or "").strip()
                    _srctag = f'  [source: {_src}]' if _src else ''
                    _dl.append(f'- Slide {si}: REPLACE "{d.get("original", "")}" WITH "{d.get("replacement", "")}"  ({d.get("reason", "")}){_srctag}')
                    if _src:
                        _delta_sources.append(_src)
            user_text += (
                "\n\nAPPROVED CONTENT UPDATES — the creator approved these because the original is "
                "OUTDATED. Apply each one exactly on the named slide; preserve ALL OTHER on-slide "
                "text verbatim as instructed above (do not make any other content changes):\n"
                + "\n".join(_dl)
            )

        if creator_guidance:
            user_text += ("\n\nCREATOR GUIDANCE (from course feedback — apply where it fits THIS deck; "
                          f"ignore parts that don't):\n{creator_guidance}")

        # References policy: cite ONLY the new material we introduce (modernized facts + newly-added
        # images); leave every existing slide's citations and any pre-existing references untouched.
        _new_sources = list(dict.fromkeys([s for s in (_delta_sources + list(added_sources or [])) if s]))
        if _new_sources:
            user_text += (
                "\n\nCITE THE NEW CONTENT — you are introducing new, sourced material into this deck "
                "(the approved updates above, and any newly-added images). Apply the SAME citation "
                "policy a fresh build uses, but ONLY to this NEW content; leave every existing slide's "
                "citations and any pre-existing references entry untouched.\n"
                "1. For each approved REPLACE update that has a [source: URL], set that slide's "
                "**Data source** field to that URL so it prints on the slide as the fact's citation.\n"
                "2. References slide: if the source deck already ENDS with a references/sources slide, "
                "ADD these new sources to it as new lines, keeping ALL existing entries exactly as they "
                "are. If the deck has NO references slide, ADD one plain references slide at the very end "
                "listing ONLY these new sources (one per line). Never remove, reorder, or rewrite an "
                "existing reference entry.\n\n"
                "NEW SOURCES TO ADD:\n" + "\n".join(f"- {s}" for s in _new_sources)
            )

        if is_revise:
            prev_md = previous_plan.get("markdown", "") if isinstance(previous_plan, dict) else str(previous_plan)
            user_text += (
                "\n\nPREVIOUS PLAN (creator rejected — address their feedback):\n\n"
                f"{prev_md}\n\n"
                f"CREATOR FEEDBACK:\n{feedback}\n\n"
                "Revise the plan accordingly. Keep the parts the creator didn't object to."
            )

        user_content.append({"type": "text", "text": user_text})

        t0 = time.time()
        try:
            response = self.call(
                messages=[{"role": "user", "content": user_content}],
                tools=[],
                max_tokens=128000,
            )
        except Exception as e:
            latency = time.time() - t0
            print(f"[RedesignPlannerAgent] EXCEPTION latency={latency:.1f}s: {e!r}")
            return self._error_stub(f"API exception: {e}")

        latency = time.time() - t0
        md = ""
        for block in response.content:
            if block.type == "text":
                md += block.text

        md = md.strip()
        if md.startswith("```") and md.endswith("```"):
            md = md.split("\n", 1)[1] if "\n" in md else md
            md = md.rsplit("```", 1)[0].rstrip()

        print(
            f"[RedesignPlannerAgent] DONE slides={num_slides} "
            f"markdown_chars={len(md)} latency={latency:.1f}s",
            flush=True,
        )

        if not md or len(md) < 100:
            return self._error_stub("planner returned empty or near-empty markdown")

        return {
            "markdown": md,
            "sources": [],
            "module_position": 1,
            "_latency_seconds": round(latency, 1),
        }

    @staticmethod
    def _error_stub(error: str) -> dict:
        print(f"[RedesignPlannerAgent] GIVING UP — returning error stub: {error}")
        return {
            "markdown": "",
            "sources": [],
            "module_position": 1,
            "_parse_error": error[:500],
        }
