# syndara-slidegen

[![CI](https://github.com/sharpeagle50/syndara-slidegen/actions/workflows/ci.yml/badge.svg)](https://github.com/sharpeagle50/syndara-slidegen/actions/workflows/ci.yml)

An AI pipeline that **generates and redesigns PowerPoint decks** — the slide
engine behind [Syndara](https://github.com/sharpeagle50). Give it a topic (or an
existing `.pptx`), and it researches, plans slide-by-slide, builds a real
PowerPoint file, and visually reviews its own output.

Released under the MIT license. Contributions welcome.

## How it works

The pipeline is a set of cooperating agents over a shared layout/render toolkit:

| Stage | Component | What it does |
|-------|-----------|--------------|
| Plan | `SlidePlannerAgent` | Researches a topic + outline → a slide-by-slide content plan |
| Build | `ClaudeCodeSlideBuilder` / `AgenticSlideBuilder` | Turns the plan into a real `.pptx` (PptxGenJS or a layout library) |
| Review | `ReviewerAgent` | Critiques content and structure, requests revisions |
| Visual QA | `VisualQAAgent` | Renders slides to images and inspects them for layout problems |
| Redesign | `RedesignPlannerAgent` | Parses an existing `.pptx` and plans a visual redesign of the same content |
| Diagrams | `DiagramGenAgent` | Generates charts and diagrams to embed |

The `tools/` package provides the shared infrastructure: hand-designed slide
layouts, a Node/PptxGenJS bridge, diagram/chart renderers, `.pptx` parsing, and
icon rendering (react-icons).

## Concept animations (Manim)

The `animation/` package lets the AI produce full-motion animated explainers by
**writing [Manim](https://www.manim.community/) code and iterating on what it
renders** — the same generate → render → look → self-correct loop as the slide
builder, pointed at an animation engine instead of a deck:

| Stage | Component | What it does |
|-------|-----------|--------------|
| Storyboard | `StoryboardAgent` | Plain-English concept → an ordered beat sheet (pedagogy + pacing) |
| Build | `ManimBuilderAgent` | One beat → Manim Scene code → rendered MP4; inspects its own keyframes and fixes runtime errors and layout defects |
| Select | `AnimationSelectorAgent` | Picks which built slides deserve a full-bleed animation when none were marked at plan time |
| Render | `manim_render` | Modal-or-local render dispatch; runs LLM-written code with credential-scrubbed env, injects pre-generated narration so no API key exists inside the render |
| Assemble | `manim_assemble` | Pure ffmpeg: per-beat clips + narration → one MP4 (freeze-frame holds, silence padding) |

Install the extra deps with `pip install -e ".[animation]"` (Manim needs
Python ≥ 3.11 and the Cairo/Pango toolchain). Rendering works with a local
`manim` binary out of the box; `animation/manim_runner.py` is an optional
standalone [Modal](https://modal.com) app you can deploy so the heavy render
toolchain stays off your application image.

## Requirements

**Core (needed for any deck build):**

| Requirement | Used for |
|---|---|
| Python 3.10+ | everything (`pip install -e .` pulls the Python deps) |
| `ANTHROPIC_API_KEY` env var | all agents |
| Node.js 18+ | the PptxGenJS deck renderer and icon rendering (`cd tools && npm install` once) |
| `claude` CLI ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), `npm i -g @anthropic-ai/claude-code`) | the default slide builder (drives PptxGenJS agentically). Without it the CLI falls back to the layout-library builder automatically |

**Per-feature (degrade gracefully when absent — the related visual is skipped
or the stage is bypassed with a warning):**

| Requirement | Used for |
|---|---|
| LibreOffice (`soffice` on PATH) | rendering slides to images: visual QA, redesign's visual analysis, the builder's `render_slide` self-check |
| poppler (`pdftoppm` on PATH) | the PDF→image step of slide rendering (pairs with LibreOffice) |
| `d2` CLI | auto-routed architecture/flow diagrams |
| `mmdc` (`npm i -g @mermaid-js/mermaid-cli`, needs Chromium) | sequence/ER/state diagrams |
| `pip install -e ".[diagrams]"` (graphviz + cairosvg) | legacy graphviz flowchart fallback |

## Install

```bash
pip install -e .                         # the deckgen package + Python deps
cd tools && npm install && cd ..         # PptxGenJS + react-icons (Node renderer)
npm install -g @anthropic-ai/claude-code @mermaid-js/mermaid-cli   # default builder + mermaid
brew install --cask libreoffice          # or your OS package manager
brew install poppler d2                  # slide rendering + d2 diagrams
```

## Usage

### CLI

```bash
# Generate a deck from a topic (researches the web, plans, builds, QAs):
deckgen build "Intro to Vector Databases" --slides 12 --style midnight -o deck.pptx

# Ground it in your own material:
deckgen build "Q3 Sales Onboarding" --context-file notes.md --style professional

# Redesign an existing deck (same content, better design):
deckgen redesign old_deck.pptx --style coral_energy -o new_deck.pptx
```

Useful flags: `--style <preset|JSON palette>` (see `tools/slide_layouts.py` for
the ~20 presets), `--max-words N` (on-slide text budget), `--decorative`
(background accent shapes; default is plain), `--web-images` (let the planner
embed real images from the web), `--qa-passes N`, `--watermark logo.png`
(stamp a logo bottom-right on every slide and centered on the title slide).

### Library

```python
from deckgen.agents import SlidePlannerAgent, ClaudeCodeSlideBuilder

outline = {
    "module_id": "vector-db-m1", "module_position": 1,
    "title": "Intro to Vector Databases", "summary": "",
    "subtopics": [], "outcomes": [], "slides": [],
    "slide_count": 12, "max_words_per_slide": 20, "plain_backgrounds": True,
}
plan = SlidePlannerAgent().plan(outline, style="midnight")

outline["approved_slide_plan"] = plan
pptx_path = ClaudeCodeSlideBuilder().build(outline, "./out", "midnight")
```

`cli.py` is the reference orchestration (plan → build → review → visual QA);
read it to see how the agents compose.

The model can be overridden with the `SYNDARA_MODEL` environment variable
(default: a current Claude model). See `agents/base.py`.

## Review checkpoints (human-in-the-loop)

Each stage is a separate call, so *you* decide when to pause between them — the
engine is stateless and holds no opinion about review, so a checkpoint is just
"run the next stage only after your own function returns." This is exactly how
the hosted [Syndara](https://github.com/sharpeagle50) product layers human
approval on top of the engine; you can build the same thing in a few lines.

```python
from deckgen.agents import SlidePlannerAgent, ClaudeCodeSlideBuilder

# 1. Plan
plan = SlidePlannerAgent().plan(outline, style="midnight")

# 2. Checkpoint — review/edit the plan before a single slide is built.
#    plan["markdown"] is the human-readable plan: show it, let someone edit it,
#    and hand it back. Return it unchanged to approve, or raise to abort.
plan = review_plan(plan)

# 3. Build from the (possibly edited) plan
outline["approved_slide_plan"] = plan
pptx = ClaudeCodeSlideBuilder().build(outline, "./out", "midnight")

# 4. Checkpoint — review the built deck. To request changes, feed structured
#    feedback into another build pass (the same shape ReviewerAgent returns):
feedback = review_deck(pptx)   # e.g. {"slides": [{"slide_index": 2, "status": "revise",
                               #        "suggestion": "..."}], "global_feedback": "..."}
if feedback:
    pptx = ClaudeCodeSlideBuilder().build(outline, "./out", "midnight", feedback)
```

When the feedback flags specific slides, the builder **edits the existing
`.pptx` in place** and leaves every other slide untouched (it only rebuilds from
scratch for sweeping, deck-wide changes) — so a targeted review pass is cheap
and non-destructive. If you'd rather apply edits directly than re-run the
builder, `tools/pptx_tool.py` exposes the low-level primitives
(`update_speaker_notes`, `extract_text_content`, `sanitize_pptx`, slide
rendering) the same review flow is built on.

Persisting these checkpoints (a database, a web UI, resuming after a restart) is
deliberately out of scope for the engine — that's application concern. The
engine just gives you clean seams to pause at.

## Contributing

Contributions are accepted under the MIT license (inbound = outbound): by
submitting a pull request, you agree your contribution is licensed under the
same MIT terms as this project. No separate CLA is required.

## Acknowledgments

The concept-animation engine exists because **Vishal Yalla** suggested letting
the AI use Manim to make animations. That idea became the `animation/`
package. Thank you, Vishal.

## License

[MIT](LICENSE) © Syndara
