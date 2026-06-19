"""Visual QA agent — inspects rendered slides for visual defects using Claude vision."""
from __future__ import annotations
import base64
import json
import traceback
from pathlib import Path
from typing import Optional


QA_SYSTEM_PROMPT = """\
You are a visual QA inspector for presentation slides. You receive rendered slide images and check for visual defects.

Inspect each slide for these 12 categories of defects:

1. **overlapping_elements** — text on text, text covering diagrams, elements stacked on each other
2. **text_overflow** — text cut off at edges, extending past the visible slide area
3. **insufficient_margins** — elements too close to slide edges (< 0.5 inch visually)
4. **low_contrast** — text hard to read against its background color
5. **layout_repetition** — consecutive slides with identical visual layout pattern (e.g., three bullet slides in a row). EXCEPTION: a multiple-choice "Quick check"/comprehension-question slide immediately followed by the SAME slide with one option highlighted (accent fill and/or a ✓) is an INTENTIONAL question→answer-reveal pair, not a defect — never flag it as repetition or a duplicate.
6. **placeholder_artifacts** — "Lorem ipsum", "[source]", "[insert X]", raw markdown syntax (**, ##, etc.), template placeholders left in
7. **empty_slide** — slides with barely any content or completely blank
8. **diagram_legibility** — diagram/chart labels too small, clipped, or unreadable
9. **inconsistent_styling** — slides that don't match the expected color palette or font style
10. **excessive_text** — slides with too many words visible (more than ~{max_words} words of on-slide text)
11. **broken_connectors** — connecting lines in diagrams that don't actually reach their target shapes, protrude past them, overlap/cut through other shapes, or stop short. Lines should start at one shape edge and end at another — flag any that look disconnected, misrouted, or visually broken
12. **misaligned_elements** — elements that visually appear intended to be aligned but aren't. Check: titles or centered text that's slightly off-center on the slide; icons or images that don't line up vertically with adjacent text; columns or grid items at inconsistent heights; rows of elements with uneven spacing. Only flag when the misalignment is clearly unintentional — deliberate asymmetric or staggered layouts are fine

You will also be given the expected style palette (colors, fonts) so you can check for consistency.

Respond with ONLY valid JSON (no markdown fences, no explanation) in this format:
{
  "slides": [
    {
      "slide_index": <1-based slide number matching the label>,
      "issues": ["category_name", ...],
      "description": "Human-readable description of what's wrong",
      "suggestion": "Specific fix suggestion"
    }
  ]
}

Only include slides that have defects. If all slides look good, return: {"slides": []}
Be strict but fair — flag real visual problems, not minor aesthetic preferences.
"""

BATCH_SIZE = 6


class VisualQAAgent:
    """Uses Claude Sonnet vision to inspect rendered slides for visual defects."""

    model = "claude-sonnet-4-6"

    def __init__(self):
        from anthropic import Anthropic
        self.client = Anthropic()

    def inspect(self, pptx_path: str, style_palette: Optional[dict] = None,
                only_indices: Optional[list[int]] = None,
                revision_feedback: str = "",
                max_words_per_slide: int = 20) -> dict:
        """Inspect slides of a PPTX for visual defects.

        Args:
            only_indices: If provided, only inspect these 0-based slide indices.
            revision_feedback: If set, only check whether this revision was addressed.

        Returns:
            {
                "status": "pass" | "fail" | "error",
                "defect_count": int,
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
            return self._inspect_inner(pptx_path, style_palette, only_indices, revision_feedback, max_words_per_slide)
        except Exception as e:
            traceback.print_exc()
            print(f"[VisualQA] ERROR: inspection failed — {e}", flush=True)
            return {"status": "error", "defect_count": 0, "slides": [], "error": str(e)}

    def _inspect_inner(self, pptx_path: str, style_palette: Optional[dict] = None,
                        only_indices: Optional[list[int]] = None,
                        revision_feedback: str = "",
                        max_words_per_slide: int = 20) -> dict:
        """Core inspection logic, may raise."""
        from ..tools.qa_render import render_all_slides
        from .base import _retry_api_call, extract_json

        jpegs = render_all_slides(pptx_path)
        if not jpegs:
            return {"status": "pass", "defect_count": 0, "slides": []}

        # Clean up the temp render directory after we're done reading files
        _qa_render_dir = str(Path(jpegs[0]).parent) if jpegs else None

        # Filter to only requested indices if specified
        indexed_jpegs = list(enumerate(jpegs))
        if only_indices is not None:
            target = set(only_indices)
            indexed_jpegs = [(i, j) for i, j in indexed_jpegs if i in target]
            if not indexed_jpegs:
                if _qa_render_dir:
                    import shutil
                    shutil.rmtree(_qa_render_dir, ignore_errors=True)
                return {"status": "pass", "defect_count": 0, "slides": []}

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

        for batch in batches:
            content_blocks: list[dict] = []
            for slide_num, jpeg_path in batch:
                # Label for the slide
                content_blocks.append({
                    "type": "text",
                    "text": f"Slide {slide_num + 1}:",
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
                all_defects.extend(self._defects_from(batch_result))
            except (json.JSONDecodeError, ValueError, KeyError, IndexError) as parse_err:
                slide_nums = [s + 1 for s, _ in batch]
                # The model answered in prose instead of JSON. Dropping the batch
                # here silently ships real defects — a deck can pass every QA pass
                # with known overlaps/broken connectors sitting in the unparsed
                # text. Ask the model to reformat its own analysis into the JSON
                # schema and reparse; only if THAT also fails do we flag the slides
                # for re-inspection rather than treating them as clean.
                recovered = False
                if raw_text:
                    try:
                        reformatted = self._reformat_to_json(raw_text)
                        all_defects.extend(self._defects_from(extract_json(reformatted)))
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
                        all_defects.append({
                            "slide_index": s,
                            "issues": ["qa_unparsed"],
                            "description": "QA response could not be parsed; this slide was not verified.",
                            "suggestion": "Re-inspect for overlapping text, text overflow, and broken or misrouted connectors.",
                        })

        if _qa_render_dir:
            import shutil
            shutil.rmtree(_qa_render_dir, ignore_errors=True)

        defect_count = len(all_defects)
        return {
            "status": "fail" if defect_count > 0 else "pass",
            "defect_count": defect_count,
            "slides": all_defects,
        }

    @staticmethod
    def _defects_from(batch_result: dict) -> list[dict]:
        """Map a parsed QA batch result into internal defect dicts (converting
        the model's 1-based slide_index to 0-based)."""
        out: list[dict] = []
        for slide_defect in batch_result.get("slides", []):
            out.append({
                "slide_index": slide_defect.get("slide_index", 1) - 1,
                "issues": slide_defect.get("issues", []),
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
            '"issues": ["category_name"], "description": "what is wrong", '
            '"suggestion": "specific fix"}]}\n'
            'Include ONLY slides that have a real defect. If none do, return '
            '{"slides": []}.\n\nANALYSIS:\n' + prose
        )
        raw = _retry_api_call(
            lambda: self.client.messages.with_raw_response.create(
                model=self.model,
                max_tokens=8192,
                messages=[{"role": "user", "content": instruction}],
            ),
            label="VisualQA-reformat",
            model=self.model,
        )
        return raw.parse().content[0].text.strip()
