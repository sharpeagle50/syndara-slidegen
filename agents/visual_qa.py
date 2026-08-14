"""Visual QA agent — inspects rendered slides for visual defects using Claude vision."""
from __future__ import annotations
import base64
import json
import traceback
from pathlib import Path
from typing import Optional

from .. import keyring


QA_SYSTEM_PROMPT = """\
You are a visual QA inspector for presentation slides. You receive rendered slide images and check for visual defects.

Inspect each slide for these 12 categories of defects:

1. **overlapping_elements** — text on text, text covering diagrams, elements stacked on each other
2. **text_overflow** — text cut off at edges, extending past the visible slide area
3. **insufficient_margins** — elements too close to slide edges (< 0.5 inch visually)
4. **low_contrast** — text hard to read against its background color
5. **placeholder_artifacts** — "Lorem ipsum", "[source]", "[insert X]", raw markdown syntax (**, ##, etc.), template placeholders left in, and raw TeX/LaTeX markup shown as literal text (dollar-sign math like $x$, \frac, or caret/brace superscripts like e^{-x} visible on the slide — math must appear typeset or as Unicode, never as markup)
6. **empty_slide** — slides with barely any content or completely blank. EXCEPTION: a slide whose label says it is an ANIMATION slide is INTENTIONALLY a bare title/poster frame — the motion is overlaid separately, the builder is forbidden to add content to it, and it must NEVER be flagged as empty_slide (or any sparseness complaint)
7. **diagram_legibility** — diagram/chart labels too small, clipped, or unreadable
8. **inconsistent_styling** — slides that don't match the expected color palette or font style
9. **excessive_text** — a slide so text-dense it reads as a document or wall of text, or is visually cramped/hard to scan: full paragraphs where phrases would do, or text packed so tightly legibility suffers. This is a LEGIBILITY / DENSITY judgment, NOT a word count — a clean, well-structured text-forward slide (bullets, columns, or a table that explains a concept) is FINE even if it carries more words than a visual slide; only flag text that is genuinely unstructured, paragraph-like, or cramped. EXCEPTIONS: references / sources / citations / bibliography / further-reading slides are always exempt (full citations legitimately run long); and never flag a slide the plan intended as text-forward merely for carrying explanatory text — flag it only if the text is actually cramped or unreadable.
10. **broken_connectors** — an arrow or connector in a diagram that is wrong PHYSICALLY or in its MEANING. PHYSICAL: a line that doesn't reach its target shape, protrudes past it, overlaps/cuts through other shapes, or stops short — connectors should run cleanly from one shape edge to another. DIRECTION / TARGET: an arrow that points to the WRONG shape, or in the WRONG direction, versus what the diagram's own labels or the "Planned visual" describe — e.g. the intended flow is A → B → C but an arrow runs B → A, links to the wrong node, or the arrowhead sits on the wrong end. Read the diagram's labels (and the "Planned visual" when given) and confirm every connector joins the correct source to the correct target, pointing the correct way.
11. **misaligned_elements** — elements that visually appear intended to be aligned but aren't. Check: titles or centered text that's slightly off-center on the slide; icons or images that don't line up vertically with adjacent text; columns or grid items at inconsistent heights; rows of elements with uneven spacing. Only flag when the misalignment is clearly unintentional — deliberate asymmetric or staggered layouts are fine
12. **inaccurate_visual** — an image, screenshot, diagram, chart, or icon that does NOT match what the slide's own title, caption, or body text says it shows. This is a CONTENT/accuracy check, not a layout check — read the slide's words, then look at its visual and judge whether they agree. Flag when: the slide names or describes a specific interface/screen/product but the image is a logo, wordmark, generic branding, or an unrelated stock photo; a diagram or chart contradicts, misrepresents, or omits the facts stated in the slide text; a screenshot is labeled as one thing but clearly shows another; data in a chart doesn't match the numbers in the text. Do NOT flag a reasonable, relevant illustration just because it isn't a perfect, official, or high-resolution example — only flag a genuine mismatch between what the slide claims and what the visual actually depicts. Each slide's label may include what the PLAN intended it to show ("Planned visual: ..."); when present, verify the rendered visual against that plan — not just the topic but the CONSTRUCTION and ORIENTATION: flag a diagram whose flow runs the wrong way, axes/elements flipped, rotated, or mirrored incorrectly, the wrong items grouped or connected, labels attached to the wrong part, or steps ordered against what the labels say. The visual must be BUILT correctly, not merely be about the right thing. Use the planned title to confirm you're judging the right slide. A slide whose label says it is TEXT-ONLY has no primary visual — NEVER flag it under inaccurate_visual.

You will also be given the expected style palette (colors, fonts) so you can check for consistency.

Respond with ONLY valid JSON (no markdown fences, no explanation) in this format:
{
  "slides": [
    {
      "slide_index": <1-based slide number matching the label>,
      "issues": ["category_name", ...],
      "severity": "critical" | "minor",
      "description": "Human-readable description of what's wrong",
      "suggestion": "Specific fix suggestion"
    }
  ]
}

Only include slides that have defects. If all slides look good, return: {"slides": []}
Be strict but fair — flag real visual problems, not minor aesthetic preferences.

SEVERITY — set "severity" on every flagged slide so we don't spend a full rebuild pass on a
cosmetic nit:
- "critical": the slide looks broken or WRONG to a learner — text cut off / overflowing the slide,
  elements overlapping so content is obscured, unreadable low-contrast text, placeholder or template
  artifacts, an empty slide (but never an ANIMATION poster frame — those are intentionally bare),
  unreadable diagram/chart labels, broken or misrouted connectors, or a
  visual that contradicts the slide's own text (inaccurate_visual).
- "minor": a small aesthetic imperfection that does NOT impede understanding — a slightly off-center
  title, a few words over the text limit, mild palette/style inconsistency, or slightly uneven spacing.
When you are unsure, choose "critical". Calibration examples:
- Title text runs off the right edge → "critical" (text_overflow).
- A subtitle sits ~15px left of center on an otherwise clean slide → "minor" (misaligned_elements).
- A label is 0.4in from the edge but fully readable → do NOT flag it at all.
"""

BATCH_SIZE = 6


class VisualQAAgent:
    """Uses Claude Sonnet vision to inspect rendered slides for visual defects."""

    model = "claude-sonnet-5"

    def __init__(self):
        self.client = keyring.sync_anthropic()

    def inspect(self, pptx_path: str, style_palette: Optional[dict] = None,
                only_indices: Optional[list[int]] = None,
                revision_feedback: str = "",
                max_words_per_slide: int = 20,
                slide_intents: Optional[dict] = None,
                prior_defects: Optional[dict] = None,
                expected_total: Optional[int] = None) -> dict:
        """Inspect slides of a PPTX for visual defects.

        Args:
            only_indices: If provided, only inspect these 0-based slide indices.
            revision_feedback: If set, only check whether this revision was addressed.
            prior_defects: {0-based index: what the previous QA pass flagged + the fix the
                builder was instructed to make} — shown to the model per slide so a re-pass
                verifies the FIX rather than judging the slide cold.
            expected_total: deck length when only_indices was computed. If the rendered deck
                no longer has that many slides, the indices are stale (builder added/removed
                slides) and the restriction is DROPPED — full inspection is the safe fallback.

        Returns:
            {
                "status": "pass" | "fail" | "error",
                "defect_count": int,
                "slide_total": int,  # rendered deck length (for the caller's next-pass scoping)
                "slides": [
                    {
                        "slide_index": int,  # 0-based
                        "issues": [str],     # issue category names
                        "description": str,  # human-readable description
                        "suggestion": str,   # specific fix suggestion
                    }
                ]
            }
        """
        try:
            return self._inspect_inner(pptx_path, style_palette, only_indices, revision_feedback,
                                       max_words_per_slide, slide_intents, prior_defects, expected_total)
        except Exception as e:
            traceback.print_exc()
            print(f"[VisualQA] ERROR: inspection failed — {e}", flush=True)
            return {"status": "error", "defect_count": 0, "slides": [], "error": str(e)}

    def _inspect_inner(self, pptx_path: str, style_palette: Optional[dict] = None,
                        only_indices: Optional[list[int]] = None,
                        revision_feedback: str = "",
                        max_words_per_slide: int = 20,
                        slide_intents: Optional[dict] = None,
                        prior_defects: Optional[dict] = None,
                        expected_total: Optional[int] = None) -> dict:
        """Core inspection logic, may raise."""
        from ..tools.qa_render import render_all_slides
        from .base import _retry_api_call, extract_json

        jpegs = render_all_slides(pptx_path)
        if not jpegs:
            return {"status": "pass", "defect_count": 0, "slides": [], "slide_total": 0}

        # Clean up the temp render directory after we're done reading files
        _qa_render_dir = str(Path(jpegs[0]).parent) if jpegs else None

        # Filter to only requested indices if specified
        indexed_jpegs = list(enumerate(jpegs))
        if only_indices is not None and expected_total is not None and len(jpegs) != expected_total:
            print(f"[VisualQA] deck length changed ({expected_total} → {len(jpegs)}) — "
                  f"stale only_indices dropped, inspecting the full deck", flush=True)
            only_indices = None
        if only_indices is not None:
            target = set(only_indices)
            indexed_jpegs = [(i, j) for i, j in indexed_jpegs if i in target]
            if not indexed_jpegs:
                if _qa_render_dir:
                    import shutil
                    shutil.rmtree(_qa_render_dir, ignore_errors=True)
                return {"status": "pass", "defect_count": 0, "slides": [], "slide_total": len(jpegs)}

        # Batch slides into groups of BATCH_SIZE
        batches: list[list[tuple[int, str]]] = []
        current_batch: list[tuple[int, str]] = []
        for i, jpeg_path in indexed_jpegs:
            current_batch.append((i, jpeg_path))
            if len(current_batch) >= BATCH_SIZE:
                batches.append(current_batch)
                current_batch = []
        if current_batch:
            batches.append(current_batch)

        all_defects: list[dict] = []

        palette_str = ""
        if style_palette:
            palette_str = f"\n\nExpected style palette:\n{json.dumps(style_palette, indent=2)}"

        def _inspect_batch(batch):
            batch_defects: list[dict] = []
            content_blocks: list[dict] = []
            for slide_num, jpeg_path in batch:
                # Label for the slide, plus what the plan intended it to show (if known)
                # so the accuracy check can compare the rendered visual against the plan.
                _label = f"Slide {slide_num + 1}:"
                if slide_intents and slide_num in slide_intents:
                    _label += f" [{slide_intents[slide_num]}]"
                # Re-pass context: what the previous QA pass flagged here and the fix the
                # builder was instructed to make — so this pass verifies the fix landed
                # (and didn't break anything else) instead of judging the slide cold.
                if prior_defects and slide_num in prior_defects:
                    _label += (f" [FLAGGED LAST QA PASS — the builder was instructed:"
                               f" {prior_defects[slide_num]} — verify this fix was applied and"
                               f" check the slide for any NEW problems the edit introduced.]")
                content_blocks.append({
                    "type": "text",
                    "text": _label,
                })
                # Base64-encode the JPEG
                jpeg_bytes = Path(jpeg_path).read_bytes()
                b64_data = base64.standard_b64encode(jpeg_bytes).decode()
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64_data,
                    },
                })

            # Add palette info at the end
            if palette_str:
                content_blocks.append({
                    "type": "text",
                    "text": palette_str,
                })

            raw_text = ""
            try:
                system_prompt = QA_SYSTEM_PROMPT.replace("{max_words}", str(max_words_per_slide))
                if revision_feedback:
                    system_prompt = (
                        "You are a visual QA inspector for presentation slides. "
                        "A creator requested a specific revision. Check ONLY whether "
                        "the revised slide(s) visually reflect the requested change. "
                        "Do NOT flag unrelated issues or suggest additional improvements.\n\n"
                        f"CREATOR'S REVISION REQUEST:\n\"{revision_feedback}\"\n\n"
                        "Respond with ONLY valid JSON (no markdown fences) in this format:\n"
                        '{\n  "slides": [\n    {\n'
                        '      "slide_index": <1-based slide number>,\n'
                        '      "issues": ["revision_not_applied"],\n'
                        '      "description": "What specifically was not addressed",\n'
                        '      "suggestion": "How to fix it"\n'
                        "    }\n  ]\n}\n\n"
                        'If the revision was correctly applied, return: {"slides": []}'
                    )
                raw = _retry_api_call(
                    lambda: self.client.messages.with_raw_response.create(
                        model=self.model,
                        # Sonnet 5 defaults adaptive thinking ON; this class parses
                        # content[0].text directly (not via BaseAgent), so keep it off.
                        thinking={"type": "disabled"},
                        max_tokens=8192,
                        system=system_prompt,
                        messages=[{
                            "role": "user",
                            "content": content_blocks,
                        }],
                    ),
                    label="VisualQA",
                    model=self.model,
                )
                response = raw.parse()
                raw_text = response.content[0].text.strip()
                # Robust parse: handles fenced ```json blocks and surrounding
                # prose (same helper the reviewer uses), not just bare JSON.
                batch_result = extract_json(raw_text)
                batch_defects.extend(self._defects_from(batch_result))
            except (json.JSONDecodeError, ValueError, KeyError, IndexError) as parse_err:
                slide_nums = [s + 1 for s, _ in batch]
                # The model answered in prose instead of JSON. Dropping the batch
                # here silently ships real defects — a deck can pass every QA pass
                # with known overlaps/broken connectors sitting in the unparsed
                # text. Ask the model to reformat its own analysis into the JSON
                # schema and reparse; only if THAT also fails do we flag the slides
                # for re-inspection rather than treating them as clean.
                recovered = False
                try:
                    from .base import report_gen_event
                    report_gen_event(
                        "retry", f"VisualQA JSON reformat — model returned prose for slides {slide_nums}",
                        {"agent": "VisualQA", "reason": str(parse_err)[:200]})
                except Exception:
                    pass
                if raw_text:
                    try:
                        reformatted = self._reformat_to_json(raw_text)
                        batch_defects.extend(self._defects_from(extract_json(reformatted)))
                        recovered = True
                        print(f"[VisualQA] recovered slides {slide_nums} via JSON reformat", flush=True)
                    except Exception as reformat_err:
                        print(f"[VisualQA] reformat retry failed for slides {slide_nums}: {reformat_err}", flush=True)
                if not recovered:
                    print(
                        f"[VisualQA] JSON parse failed for slides {slide_nums}: {parse_err}. "
                        f"Raw response: {raw_text[:300] if raw_text else '(unavailable)'}",
                        flush=True,
                    )
                    for s, _ in batch:
                        batch_defects.append({
                            "slide_index": s,
                            "issues": ["qa_unparsed"],
                            # "minor", explicitly: without a severity these would default to
                            # critical and force a full builder rebuild for slides nobody
                            # actually diagnosed. An unverified slide is not a known defect.
                            "severity": "minor",
                            "description": "QA response could not be parsed; this slide was not verified.",
                            "suggestion": "Re-inspect for overlapping text, text overflow, and broken or misrouted connectors.",
                        })
            return batch_defects

        # Inspect batches concurrently — each is an independent vision call. Threads are
        # fine (blocking network I/O releases the GIL); ThreadPoolExecutor.map preserves
        # batch order, so the aggregated defects match the old sequential result exactly.
        if len(batches) <= 1:
            for _b in batches:
                all_defects.extend(_inspect_batch(_b))
        else:
            from concurrent.futures import ThreadPoolExecutor
            # 8 (was 4): Anthropic limits sit at ~100% headroom in every build log; on a
            # 25-batch deck this halves the wall-clock of a full QA pass.
            with ThreadPoolExecutor(max_workers=min(8, len(batches))) as _ex:
                for _bd in _ex.map(_inspect_batch, batches):
                    all_defects.extend(_bd)

        if _qa_render_dir:
            import shutil
            shutil.rmtree(_qa_render_dir, ignore_errors=True)

        defect_count = len(all_defects)
        critical_count = sum(1 for d in all_defects if d.get("severity", "critical") == "critical")
        return {
            "status": "fail" if defect_count > 0 else "pass",
            "defect_count": defect_count,
            "critical_count": critical_count,
            "slide_total": len(jpegs),
            "slides": all_defects,
        }

    @staticmethod
    def _defects_from(batch_result: dict) -> list[dict]:
        """Map a parsed QA batch result into internal defect dicts (converting
        the model's 1-based slide_index to 0-based)."""
        out: list[dict] = []
        for slide_defect in batch_result.get("slides", []):
            _sev = slide_defect.get("severity")
            out.append({
                "slide_index": slide_defect.get("slide_index", 1) - 1,
                "issues": slide_defect.get("issues", []),
                # Default missing/invalid severity to "critical" (fail-safe: an unlabeled defect
                # still triggers a rebuild rather than silently shipping).
                "severity": _sev if _sev in ("critical", "minor") else "critical",
                "description": slide_defect.get("description", ""),
                "suggestion": slide_defect.get("suggestion", ""),
            })
        return out

    def _reformat_to_json(self, prose: str) -> str:
        """The QA model sometimes returns its findings as prose instead of JSON,
        which would otherwise drop the whole batch's defects. Ask it to convert
        that analysis into the strict schema so the defects aren't lost."""
        from .base import _retry_api_call
        instruction = (
            "Convert the following slide QA analysis into ONLY valid JSON — no "
            "markdown fences, no prose — in this exact format:\n"
            '{"slides": [{"slide_index": <1-based slide number>, '
            '"issues": ["category_name"], "severity": "critical" | "minor", '
            '"description": "what is wrong", "suggestion": "specific fix"}]}\n'
            'Set "severity" from the analysis: "critical" if the slide looks broken or '
            'wrong to a learner (text cut off/overflowing, overlap, unreadable), "minor" '
            'for small polish issues. When the analysis is unclear, use "critical".\n'
            'Include ONLY slides that have a real defect. If none do, return '
            '{"slides": []}.\n\nANALYSIS:\n' + prose
        )
        raw = _retry_api_call(
            lambda: self.client.messages.with_raw_response.create(
                model=self.model,
                # Sonnet 5 defaults adaptive thinking ON; parses content[0].text directly.
                thinking={"type": "disabled"},
                max_tokens=8192,
                messages=[{"role": "user", "content": instruction}],
            ),
            label="VisualQA-reformat",
            model=self.model,
        )
        return raw.parse().content[0].text.strip()
