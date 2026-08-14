"""Concept-animation engine: LLM agents that write, render, and self-correct Manim animations.

The pipeline mirrors the slide builder's generate → render → look → fix loop, split by concern:

- StoryboardAgent   — plain-English concept → an ordered beat sheet (pedagogy + pacing)
- ManimBuilderAgent — one beat → Manim Scene code → rendered MP4, self-correcting on render
                      errors and visual defects (it LOOKS at its own keyframes)
- DirectorAgent     — coherence layer for full-length (2–20 min) videos: one narrative arc,
                      a persistent visual language, screenplay → beats
- AnimationSelectorAgent — picks which built slides deserve a full-bleed animation when the
                      planner didn't mark any
- manim_render      — Modal-or-local render dispatch with a credential-scrubbed sandbox and
                      pre-generated-narration injection (no API keys inside the render)
- manim_assemble    — pure-ffmpeg assembly of per-beat clips + narration into one MP4
- manim_runner      — standalone Modal app for the heavy Manim/Cairo/Pango toolchain
                      (deploy separately; never imported by the pipeline)

The idea of handing the AI a real animation engine — letting it write Manim code against a
renderer and iterate on what it sees — was suggested by Vishal Yalla. See the README's
Acknowledgments.
"""
from .storyboard import StoryboardAgent
from .manim_builder import ManimBuilderAgent
from .director import DirectorAgent, flatten_beats
from .animation_selector import AnimationSelectorAgent

__all__ = ["StoryboardAgent", "ManimBuilderAgent", "DirectorAgent", "flatten_beats",
           "AnimationSelectorAgent"]
