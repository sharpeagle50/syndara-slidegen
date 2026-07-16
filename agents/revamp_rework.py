"""Autonomous slide-rework agent for the revamp "Update slides" mode.

Unlike RedesignPlanner (restyle only, strict 1:1 slide mapping), this agent is a free-ish agent:
it SEES the module's slides (text + notes + embedded images + rendered PNGs), web-researches on
demand, and evaluates every slide on BOTH modernity (useful / relevant / accurate as of today) and
presentation quality — then proposes a reworked deck that may reorder, split, merge, add, remove,
and edit slides. Conservative on deleting; freer on editing/reordering/splitting/adding. It reuses
original images wherever they still help and only requests new ones where a slide is genuinely thin.

Output is a REVIEWABLE plan (same markdown format the builder consumes) plus a source-map that ties
each output slide back to its source slide(s) so images/notes/citations can be rethreaded.

Cost is intentionally NOT budget-capped here (only loop-guarded via max_iterations) so the true
free-running cost is observable. Per-step trace (every web search, the proposed operations, token +
search spend) is emitted via report_gen_event; SYNDARA_TRACE_VERBOSE adds per-search detail.
"""
import os

from .base import BaseAgent, extract_json, report_gen_event, MaxTokensError, STYLE_RULE, strip_em_dashes, visual_directive


SOURCEMAP_SENTINEL = "===SOURCEMAP==="

REWORK_SYSTEM = """You are an expert instructional designer REWORKING one module's slide deck — not
just restyling it. Today is {today}. You can SEE the slides (their text, speaker notes, embedded
images, and rendered snapshots) and you have web_search / web_fetch to research whenever you need to.

YOUR JOB: make every slide (1) USEFUL, (2) RELEVANT, and (3) ACCURATE AS OF TODAY, and make the deck
present and flow well. Evaluate each slide on modernity AND presentation, research anything you're
unsure is current, then rework the deck.

YOU MAY:
- EDIT on-slide text to fix outdated facts, deprecated tools/APIs, stale figures, or awkward wording.
- REORDER slides for better flow.
- SPLIT one overloaded slide into two.
- MERGE two thin or redundant slides into one.
- ADD a bridging or summary slide where there's a genuine gap.
- Change layout/visuals (text-heavy bullets -> table / chart / flowchart / comparison / diagram).

LAYOUT (how visual vs text-forward the reworked deck should be): {layout_lean}

RULES (asymmetric caution — much more careful about removing than about adding/editing/reordering):
- NEVER lose content. When you split or merge, REDISTRIBUTE the words — don't drop facts. Only REMOVE
  a slide if it is genuinely redundant or wholly obsolete; when in doubt, KEEP it.
- Be conservative deleting; be freer editing, reordering, splitting, and adding.
- REUSE the original images wherever they still illustrate the point. Each original image has an id
  (listed below) — put "**Image ID:** <id>" on the slide where you want it, and you MAY move an image
  to a different slide than its source if it fits better there. Only request a NEW image where a slide
  is genuinely thin/abstract and a real image clearly helps. {web_images_note} (Every NEW web image is
  automatically captioned with its source directly underneath — you don't add that yourself.)
- MODERNIZE with evidence: for any fact/figure/tool you change or add, web_search to confirm it's
  current, and cite the source URL. Don't invent sources.
- PRESERVE CITATIONS: when you KEEP content from an original slide, carry over any source citation it
  already had (a Data source line, a footnote, a "[source: ...]") onto the reworked slide's Data source.
  NEVER drop a citation for content you keep.
- Preserve the creator's intent and voice. Don't pad, don't editorialize, don't add filler slides.
- Generate fresh, TTS-ready speaker notes (up to 300 words, conversational) for every output slide.
- NEVER acknowledge the rework in the speaker notes. Narrate as a presenter teaching this material for
  the first time — the learner must NOT know the deck was reworked, updated, modernized, or adapted
  from an older course. No meta-commentary: no "this slide has been updated", "previously", "the
  refreshed version", "now includes", "we've modernized this", etc. Just present and explain what's on
  the slide, exactly as any presenter delivering this deck would.

OUTPUT — two parts, in this exact order:

PART 1 — the reworked plan, one block per OUTPUT slide, in this format:

### Slide <n>
- **Type:** <none | chart | flowchart | sequence_diagram | er_diagram | architecture_diagram | image | icon_set | table>
- **Detailed description:** <REQUIRED if type is not 'none'; describe the visual precisely.>
- **Image ID:** <to place an ORIGINAL image, its id from the list below (e.g. src-3); for a NEW web image, write 'NEW:<what to find>'; otherwise 'N/A'. Use 'NEW:' only when new images are allowed.>
- **Data source:** <a source URL for any statistic/claim/figure ON this slide — one you ADDED or modernized, OR one already cited on the original slide whose content you kept. 'N/A' only if the slide has no such sourced claim.>
- **Teaching purpose:** <one sentence, or 'N/A' if type is 'none'.>

**On-slide text**
- <exact text lines for this slide>

**Speaker notes**
<full narration>

---

Close the deck with a references slide that lists BOTH every source already cited anywhere in the
original deck (its own references slide + any on-slide or footnote citations) AND every new source you
used. Do not drop any original reference.

PART 2 — after a line containing exactly {sentinel}, output ONE JSON object (no fences):
{{"map": [{{"out": <1-based output slide #>, "src": [<source slide #s this draws from, 1-based>], "op": "keep|edit|split|merge|new|reorder|remove"}}, ...],
  "changes": "<2-4 sentence human summary of what you changed and why>",
  "sources": ["<url>", ...]}}
Every output slide must appear in "map". Use "src": [] for a genuinely new slide. (Image placement
is declared per-slide via the Image ID field above, NOT here.)"""


class SlideReworkAgent(BaseAgent):
    allowed_tool_names = ["web_search", "web_fetch"]
    system_prompt = REWORK_SYSTEM

    def _extract_searches(self, messages: list) -> list[str]:
        """Pull every web_search query out of the tool-loop transcript (best-effort)."""
        out: list[str] = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                try:
                    if getattr(b, "type", "") in ("server_tool_use", "tool_use") and getattr(b, "name", "") == "web_search":
                        q = (getattr(b, "input", None) or {}).get("query")
                        if q:
                            out.append(str(q))
                except Exception:
                    continue
        return out

    def rework(self, module_title: str, extracted_content: list[dict], style: str,
               source_slide_pngs: list[str] | None, today: str, web_images_enabled: bool = False,
               creator_guidance: str = "", max_rounds: int = 40, visual_level=None,
               max_questions: int = 0) -> dict:
        """Rework one module's deck. Returns {markdown, source_map, changes, sources, stats}.
        Image placement is declared per-slide in the plan via 'Image ID' (src-N to reuse an original,
        'NEW:<query>' for a new web image) — resolved at build time. Never raises."""
        import base64

        debug = bool(os.environ.get("SYNDARA_TRACE_VERBOSE") or os.environ.get("SYNDARA_REVAMP_DEBUG"))
        n_in = len(extracted_content)
        wi_note = ("You MAY request a NEW web image (the add-on is on) via Image ID 'NEW:<what to find>'."
                   if web_images_enabled else
                   "Do NOT request new web images (the add-on is off) — reuse originals or design a diagram.")
        max_questions = max(0, int(max_questions or 0))
        if max_questions <= 0:
            questions_directive = (
                "IN-SLIDE QUESTIONS: Do NOT add comprehension/question slides to this deck.")
        else:
            questions_directive = (
                f"IN-SLIDE QUESTIONS: You MAY add up to {max_questions} comprehension question slide(s) to "
                f"this deck. The source deck may have few or none — wherever a checkpoint genuinely helps a "
                f"learner test understanding, ADD one (lean toward {max_questions} if the deck is light on "
                f"them), but never add filler just to reach the number. To emit one, output a slide with a "
                f"line '**Layout:** question_slide', put the question plus four options (A-D) in On-slide "
                f"text, and put the correct answer plus a one-line explanation in the speaker notes; the "
                f"builder renders each as a question + answer-reveal pair. Count each as a NEW slide in the "
                f"source-map (op: \"new\", src: []).")
        self.system_prompt = ((REWORK_SYSTEM
                              .replace("{today}", today)
                              .replace("{web_images_note}", wi_note)
                              .replace("{layout_lean}", visual_directive(visual_level))
                              .replace("{sentinel}", SOURCEMAP_SENTINEL)) + STYLE_RULE
                              + "\n\n" + questions_directive)

        # Build the multimodal prompt: per-slide text/notes/images, plus rendered PNGs for vision.
        blocks = []
        manifest = []
        for s in extracted_content:
            idx = int(s.get("slide_index", 0)) + 1
            t = (s.get("text") or "").strip()
            notes = (s.get("speaker_notes") or "").strip()
            imgs = s.get("embedded_images") or []
            b = f"### Source Slide {idx}\n**Text:**\n{t}\n"
            if notes:
                b += f"**Existing notes:**\n{notes[:1500]}\n"
            if imgs:
                _iid = f"src-{idx}"
                b += f"**Original image [id: {_iid}]** — reuse via 'Image ID: {_iid}' where it still helps.\n"
                manifest.append(_iid)
            blocks.append(b)
        guide = f"\n\nCREATOR GUIDANCE (apply where it fits):\n{creator_guidance}" if creator_guidance else ""
        _man = ("\n\nAVAILABLE ORIGINAL IMAGES (place any via its Image ID, moving it to whatever slide "
                "fits best): " + ", ".join(manifest)) if manifest else "\n\n(The source deck has no embedded images.)"
        user_content: list = [{
            "type": "text",
            "text": (f"MODULE: {module_title or '(untitled)'}\nTARGET STYLE: {style}\n"
                     f"SOURCE DECK — {n_in} slides:\n\n" + "\n".join(blocks)[:150000] + _man + guide
                     + "\n\nEvaluate every slide for modernity and presentation, research whatever you "
                       "need, and produce the reworked plan."),
        }]
        for png in (source_slide_pngs or []):
            try:
                with open(png, "rb") as f:
                    user_content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": base64.b64encode(f.read()).decode()}})
            except Exception:
                continue

        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 40, "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 20, "allowed_callers": ["direct"]},
        ]
        report_gen_event("rework", f"reworking {n_in}-slide deck", {"slides_in": n_in, "web_images": web_images_enabled})
        try:
            final_text, messages = self.run_tool_loop(
                messages=[{"role": "user", "content": user_content}], tools=tools,
                tool_handlers={}, max_tokens=128000, trace_label="SlideRework", max_iterations=max_rounds)
        except MaxTokensError as e:
            print(f"[SlideRework] TRUNCATED — deck too large for one rework pass: {e!r}", flush=True)
            report_gen_event("rework", "plan exceeded the token budget (deck too large) — this module "
                             "will restyle instead of rework", {"error": True, "truncated": True})
            return {"markdown": "", "source_map": [], "changes": "", "sources": [],
                    "stats": {"failed": True, "truncated": True}}
        except Exception as e:
            print(f"[SlideRework] failed: {e!r}", flush=True)
            report_gen_event("rework", f"failed: {str(e)[:200]}", {"error": True})
            return {"markdown": "", "source_map": [], "changes": "", "sources": [],
                    "stats": {"failed": True}}

        # Split the plan markdown from the trailing source-map JSON.
        md, _, sm_raw = final_text.partition(SOURCEMAP_SENTINEL)
        markdown = md.strip()
        source_map, changes, sources = [], "", []
        if sm_raw.strip():
            try:
                d = extract_json(sm_raw)
                if isinstance(d, dict):
                    source_map = d.get("map") or []
                    changes = str(d.get("changes", ""))[:2000]
                    sources = [str(u) for u in (d.get("sources") or []) if u][:60]
            except Exception as e:
                print(f"[SlideRework] source-map parse failed: {e!r}", flush=True)

        n_out = markdown.count("### Slide")
        searches = self._extract_searches(messages)
        stats = {"slides_in": n_in, "slides_out": n_out, "web_searches": len(searches),
                 "ops": [x.get("op") for x in source_map if isinstance(x, dict)]}
        # Trace: what changed + how much it researched. Debug adds the actual queries.
        detail = {"slides_in": n_in, "slides_out": n_out, "web_searches": len(searches), "summary": changes}
        if debug:
            detail["queries"] = searches
            detail["map"] = source_map
        report_gen_event("rework", f"{n_in}->{n_out} slides, {len(searches)} searches", detail)
        return {"markdown": strip_em_dashes(markdown), "source_map": source_map,
                "changes": strip_em_dashes(changes), "sources": sources, "stats": stats}
