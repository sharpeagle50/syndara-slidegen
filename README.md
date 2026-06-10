# syndara-slidegen

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

- **Python 3.10+**
- **Node.js 18+** — the PptxGenJS renderer and icon rendering run in a Node
  subprocess. Install its deps once: `cd tools && npm install`.
- **LibreOffice** — used to rasterize slides for rendering and visual QA. The
  `soffice` binary must be on your `PATH` (visual QA degrades gracefully if it
  is absent, but slide-image rendering requires it).
- **An Anthropic API key** — set `ANTHROPIC_API_KEY` in your environment.

## Install

```bash
pip install -e .            # installs the `deckgen` package + Python deps
cd tools && npm install     # PptxGenJS + react-icons (Node renderer)
# install LibreOffice via your OS package manager (brew install --cask libreoffice, etc.)
```

## Usage

```python
from deckgen.agents import SlidePlannerAgent, ClaudeCodeSlideBuilder

# 1. Plan a deck from a topic/outline
planner = SlidePlannerAgent()
plan = planner.plan(
    outline={"title": "Intro to Vector Databases", "summary": "...", "slide_count": 12},
    style="midnight",
)

# 2. Build the .pptx from the plan
builder = ClaudeCodeSlideBuilder()
# ... drive the builder with the plan to produce slides.pptx
```

The model can be overridden with the `SYNDARA_MODEL` environment variable
(default: a current Claude model). See `agents/base.py`.

> **Note:** a turnkey CLI (`deckgen build "topic"` / `deckgen redesign in.pptx`)
> is in progress. For now the agents are used as a library, as above.

## Contributing

Contributions are accepted under the MIT license (inbound = outbound): by
submitting a pull request, you agree your contribution is licensed under the
same MIT terms as this project. No separate CLA is required.

## License

[MIT](LICENSE) © Syndara
