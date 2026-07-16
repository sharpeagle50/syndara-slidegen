"""Command-line interface for the deckgen slide engine.

    deckgen build "Intro to Vector Databases" --slides 12 --style midnight -o deck.pptx
    deckgen redesign old_deck.pptx --style coral_energy -o new_deck.pptx

Mirrors the same plan → build → review → visual-QA pipeline Syndara runs in
production, as a single local run with no database or human-in-the-loop steps.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[deckgen] {msg}", flush=True)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", text.lower().strip())
    return re.sub(r"[\s-]+", "-", s)[:60].strip("-") or "deck"


def _pick_builder():
    """Default: claude_code (richer layouts). Fallback: layout library."""
    from .agents import AgenticSlideBuilder, ClaudeCodeSlideBuilder

    choice = os.environ.get("SYNDARA_BUILDER", "claude_code").lower()
    if choice in ("layout_library", "agentic"):
        return AgenticSlideBuilder()
    try:
        import claude_agent_sdk  # noqa: F401
        return ClaudeCodeSlideBuilder()
    except ImportError as e:
        _log(f"claude-agent-sdk unavailable ({e}); falling back to layout-library builder")
        return AgenticSlideBuilder()


def _make_outline(title: str, summary: str, slide_count: int, max_words: int,
                  plain_backgrounds: bool) -> dict:
    return {
        "module_id": f"{_slugify(title)}-m1",
        "module_position": 1,
        "title": title,
        "summary": summary,
        "subtopics": [],
        "outcomes": [],
        "slide_count": slide_count,
        "max_words_per_slide": max_words,
        "plain_backgrounds": plain_backgrounds,
        "slides": [],
    }


def _require_plan(plan: dict) -> dict:
    """Fail fast when a planner returned an error stub instead of a plan."""
    if not plan.get("markdown"):
        err = plan.get("_parse_error") or plan.get("error") or "planner returned an empty plan"
        raise SystemExit(f"[deckgen] planning failed: {err}")
    return plan


def _watermark_info(args: argparse.Namespace) -> dict | None:
    if not args.watermark:
        return None
    wm = Path(args.watermark)
    if not wm.exists():
        raise SystemExit(f"[deckgen] error: watermark image not found: {wm}")
    return {"image_path": str(wm), "mode": args.watermark_mode}


def _build_review_qa(outline: dict, work_dir: Path, style: str, qa_passes: int,
                     watermark_info: dict | None = None) -> str:
    """Builder + fact-check + visual-QA loop. Returns path to the final .pptx."""
    from .agents import ReviewerAgent, VisualQAAgent
    from .tools import pptx_tool
    from .tools.slide_layouts import get_palette

    builder = _pick_builder()
    reviewer = ReviewerAgent()
    reviewer_feedback = None
    pptx_path = ""

    OUTER_MAX = 2
    for cycle in range(1, OUTER_MAX + 1):
        _log(f"builder pass {cycle}/{OUTER_MAX}...")
        pptx_path = builder.build(outline, str(work_dir), style, reviewer_feedback, watermark_info)
        if not Path(pptx_path).exists() or not zipfile.is_zipfile(pptx_path):
            raise RuntimeError("builder produced a missing or corrupt .pptx")
        if cycle == OUTER_MAX:
            break
        slide_content = pptx_tool.extract_text_content(pptx_path)
        _plan = outline.get("approved_slide_plan")
        plan_context = ({"markdown": _plan.get("markdown", ""), "sources": _plan.get("sources") or []}
                        if isinstance(_plan, dict) else None)
        verdict = reviewer.review_slides(slide_content, cycle, plan_context=plan_context)
        _log(f"reviewer: {verdict.get('status', 'approved')}")
        if verdict.get("status", "approved") != "approved":
            _flagged = [s.get("slide_index") for s in (verdict.get("slides") or []) if s.get("status") == "revise"]
            _log(f"  reviewer flagged slides {_flagged or '(none — global feedback → full rebuild)'}")
            if verdict.get("global_feedback"):
                _log(f"  reviewer note: {str(verdict['global_feedback'])[:200]}")
        if verdict.get("status", "approved") == "approved":
            break
        reviewer_feedback = verdict

    qa_agent = VisualQAAgent()
    palette = get_palette(style)
    for qa_cycle in range(1, qa_passes + 1):
        try:
            qa = qa_agent.inspect(
                pptx_path, palette,
                max_words_per_slide=outline.get("max_words_per_slide") or 20,
            )
        except Exception as e:
            _log(f"visual QA unavailable ({e}); skipping — install LibreOffice to enable it")
            break
        _log(f"visual QA pass {qa_cycle}: {qa['status']}, {qa['defect_count']} defects")
        for s in (qa.get("slides") or []):
            _issues = ", ".join(s.get("issues") or []) if isinstance(s.get("issues"), list) else (s.get("issues") or "")
            _log(f"  QA slide {s.get('slide_index')}: {(s.get('description') or _issues or '')[:140]}")
        if qa["status"] in ("pass", "error") or qa["defect_count"] == 0 or qa_cycle == qa_passes:
            break
        qa_feedback = {
            "slides": [
                {"slide_index": s["slide_index"], "status": "revise",
                 "issues": s["issues"],
                 "suggestion": s["description"] + " " + s["suggestion"]}
                for s in qa["slides"]
            ],
            "global_feedback": f"Visual QA cycle {qa_cycle}: {qa['defect_count']} visual defects found.",
        }
        new_path = builder.build(outline, str(work_dir), style, qa_feedback, watermark_info)
        if Path(new_path).exists() and zipfile.is_zipfile(new_path):
            pptx_path = new_path

    if watermark_info:
        from .tools.watermark import apply_watermark
        _log(f"applying watermark ({watermark_info['mode']})...")
        apply_watermark(pptx_path, watermark_info["image_path"], watermark_info["mode"], watermark_info.get("scale", 1.0))

    return pptx_path


def cmd_build(args: argparse.Namespace) -> int:
    from .agents import SlidePlannerAgent

    from .tools.text_extract import extract_text_from_path, extract_text_from_dir, UnsupportedFileType

    context = args.context or ""
    if args.context_file:
        try:
            file_text = extract_text_from_path(args.context_file)
        except UnsupportedFileType:
            # An explicit file the user pointed at — treat an unknown extension
            # as plain text, preserving the original read_text() behavior.
            file_text = Path(args.context_file).read_text(errors="replace")
        context = (context + "\n\n" if context else "") + file_text
    if getattr(args, "context_dir", None):
        _log(f"reading context from folder {args.context_dir}...")
        dir_text = extract_text_from_dir(args.context_dir, log=_log)
        if dir_text:
            context = (context + "\n\n" if context else "") + dir_text

    work_dir = Path(tempfile.mkdtemp(prefix="deckgen_build_"))
    outline = _make_outline(args.topic, context, args.slides, args.max_words, not args.decorative)

    _log(f"planning {args.slides} slides on {args.topic!r} (style={args.style})...")
    planner = SlidePlannerAgent()
    slide_plan = _require_plan(planner.plan(outline, args.style, web_images=args.web_images,
                                            max_questions=args.questions))

    if args.web_images and slide_plan.get("image_urls"):
        from .tools.image_fetch import download_plan_images
        img_dir = work_dir / "images"
        _log(f"downloading {len(slide_plan['image_urls'])} web images...")
        image_map = asyncio.run(download_plan_images(slide_plan["image_urls"], str(img_dir)))
        outline["web_images"] = image_map

    outline["approved_slide_plan"] = slide_plan
    (work_dir / "outline.json").write_text(json.dumps(outline, indent=2))

    pptx_path = _build_review_qa(outline, work_dir, args.style, args.qa_passes,
                                 _watermark_info(args))
    out = args.output or f"{_slugify(args.topic)}.pptx"
    shutil.copy2(pptx_path, out)
    _log(f"done → {out}")
    return 0


def cmd_redesign(args: argparse.Namespace) -> int:
    from .agents import RedesignPlannerAgent
    from .tools import pptx_tool

    src = Path(args.input)
    if not src.exists():
        _log(f"error: {src} not found")
        return 1

    work_dir = Path(tempfile.mkdtemp(prefix="deckgen_redesign_"))
    _log(f"extracting content from {src.name}...")
    extracted = pptx_tool.extract_deck_content(str(src), str(work_dir / "source_images"))
    try:
        source_pngs = pptx_tool.extract_slide_pngs(str(src), str(work_dir / "source_pngs"))
    except Exception as e:
        _log(f"slide rendering unavailable ({e}); planning from text only — install LibreOffice for visual analysis")
        source_pngs = []
    _log(f"extracted {len(extracted)} slides")

    title = args.title or src.stem.replace("_", " ").replace("-", " ").title()
    outline = _make_outline(title, "", len(extracted), args.max_words, not args.decorative)

    _log(f"planning redesign (style={args.style})...")
    planner = RedesignPlannerAgent()
    slide_plan = _require_plan(planner.plan(extracted, args.style, source_pngs,
                                            max_words_per_slide=args.max_words))

    # Map embedded source images so the builder can re-place them
    web_images = {}
    for slide in extracted:
        for img in slide.get("embedded_images", []):
            heading = f"Slide {slide['slide_index'] + 1}"
            try:
                from PIL import Image as PILImage
                with PILImage.open(img["path"]) as pil_img:
                    w, h = pil_img.size
            except Exception:
                w, h = img.get("width_px", 800), img.get("height_px", 600)
            web_images[heading] = {"path": img["path"], "width_px": w, "height_px": h,
                                   "aspect": round(w / max(h, 1), 3)}
    if web_images:
        outline["web_images"] = web_images

    outline["approved_slide_plan"] = slide_plan
    (work_dir / "outline.json").write_text(json.dumps(outline, indent=2))

    pptx_path = _build_review_qa(outline, work_dir, args.style, args.qa_passes,
                                 _watermark_info(args))
    out = args.output or f"{src.stem}_redesigned.pptx"
    shutil.copy2(pptx_path, out)
    _log(f"done → {out}")
    return 0


def _add_shared_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--style", default="professional",
                   help="palette preset name, custom palette JSON, or path to a "
                        "palette .json file (default: professional)")
    p.add_argument("--max-words", type=int, default=20,
                   help="max words of on-slide text per slide (default: 20)")
    p.add_argument("--decorative", action="store_true",
                   help="add decorative background shapes (default: plain backgrounds)")
    p.add_argument("--qa-passes", type=int, default=3,
                   help="max visual QA passes (default: 3; requires LibreOffice)")
    p.add_argument("--watermark", help="logo image to stamp on every slide")
    p.add_argument("--watermark-mode", default="title_and_bottom_right",
                   choices=["bottom_right", "title_and_bottom_right"],
                   help="bottom-right corner on all slides, optionally centered "
                        "at the top of the title slide too (default)")
    p.add_argument("-o", "--output", help="output .pptx path")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckgen",
        description="Generate or redesign a PowerPoint deck with an AI agent pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="research a topic and build a deck from scratch")
    b.add_argument("topic", help="deck topic/title")
    b.add_argument("--context", default="", help="extra context to ground the content")
    b.add_argument("--context-file", help="file to extract and append to the context (txt/md/csv/pdf/docx/xlsx)")
    b.add_argument("--context-dir", help="folder whose supported files (txt/md/csv/pdf/docx/xlsx) are extracted and appended to the context")
    b.add_argument("--slides", type=int, default=15, help="target slide count (default: 15)")
    b.add_argument("--questions", type=int, default=2,
                   help="max in-slide comprehension questions (0 = none; default: 2)")
    b.add_argument("--web-images", action="store_true",
                   help="let the planner pick real web images and embed them")
    _add_shared_args(b)
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("redesign", help="rebuild an existing .pptx with improved design")
    r.add_argument("input", help="path to the source .pptx")
    r.add_argument("--title", help="deck title (default: derived from the filename)")
    _add_shared_args(r)
    r.set_defaults(func=cmd_redesign)

    args = parser.parse_args(argv)

    # --style can be a preset name, inline palette JSON, or a path to a
    # palette .json file (the CLI equivalent of the UI's saved palettes).
    if args.style.endswith(".json") and Path(args.style).is_file():
        args.style = Path(args.style).read_text().strip()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        _log("error: ANTHROPIC_API_KEY is not set")
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
