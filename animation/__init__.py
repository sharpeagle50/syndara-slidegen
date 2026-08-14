"""Animated slides: LLM agents that write, render, and self-correct Manim animations for
slide-deck generation — a plan can mark a slide `Type: animation`, and this package turns
that slide into a rendered full-bleed animation clip.

The loop mirrors the slide builder's generate → render → look → fix pattern:

- ManimBuilderAgent — one animation brief → Manim Scene code → rendered MP4, self-correcting
                      on render errors and visual defects (it LOOKS at its own keyframes)
- AnimationSelectorAgent — picks which built slides deserve a full-bleed animation when the
                      planner didn't mark any
- manim_render      — Modal-or-local render dispatch with a credential-scrubbed sandbox and
                      pre-generated-narration injection (no API keys inside the render)
- manim_runner      — standalone Modal app for the heavy Manim/Cairo/Pango toolchain
                      (deploy separately; never imported by the pipeline)

The idea of letting the AI use Manim to make animations was suggested by Vishal Yalla. See the
README's Acknowledgments.
"""
from .manim_builder import ManimBuilderAgent
from .animation_selector import AnimationSelectorAgent

__all__ = ["ManimBuilderAgent", "AnimationSelectorAgent"]
