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

## Contributing

Contributions are accepted under the MIT license (inbound = outbound): by
submitting a pull request, you agree your contribution is licensed under the
same MIT terms as this project. No separate CLA is required.

## License

[MIT](LICENSE) © Syndara
