"""AnimationSelector: pick which of a built module's slides should become full-bleed Manim
animations, when they weren't marked at plan time.

The main course/deck pipeline marks animations in the SlidePlanner (Type: animation). Paths that
don't run the planner — notably Revamp — have no such marking, so this agent looks at the already-
built slides (title + speaker notes) and chooses up to `budget` where MOTION teaches better than a
static slide. Returns the same shape the planner emits ({rendered_index, title, description}), so the
animation_runner treats both identically. Extends BaseAgent → token usage auto-meters to the sink.
"""
from __future__ import annotations

import json
import re

from ..agents.base import BaseAgent


class AnimationSelectorAgent(BaseAgent):
    # Pure ranking call (pick which slides become Manim animations, not design them), so it doesn't
    # need Opus-tier reasoning — Sonnet 5 is plenty and cheaper. The actual animation DESIGN
    # (ManimBuilderAgent) stays on the default model.
    model = "claude-sonnet-5"
    allowed_tool_names: list[str] = []
    system_prompt = """You choose which slides in a deck should become short animated explainers
(rendered as full-screen Manim animations shown in place of the static slide, narrated by the
slide's existing notes).

You are given the deck's slides (index, title, and speaker notes). Choose the slides where MOTION
teaches best — something building up, moving, transforming, or unfolding step by step (a process
flowing, a graph being traced, vectors adding, an algorithm stepping, a geometric construction).
Work from the MOST motion-worthy slides down. Using the full requested count is good when the
material supports it — an all-animated, montage-style module is a valid choice, so when the number
is large, be inclusive. But don't FORCE motion onto a title slide, a plain bullet list, a summary,
or a slide a static chart already conveys; if fewer slides truly warrant it, pick fewer.

Return ONLY a JSON array (no prose, no fences) of at most the requested number of objects:
  {"index": <the slide's index, integer>,
   "description": "<director's note describing the MOTION to animate — what appears, moves, or is
                   traced over time. Geometry, graphs, processes, motion only; no equations.>"}
If no slide truly benefits, return []."""

    def select(self, slides: list[dict], budget: int) -> list[dict]:
        """slides: ordered [{"title", "speaker_notes"}] index-aligned to the built deck. Returns up to
        `budget` {rendered_index, title, description} entries. Best-effort — [] on any failure."""
        budget = max(0, int(budget or 0))
        if budget <= 0 or not slides:
            return []
        lines = []
        for i, s in enumerate(slides):
            title = (s.get("title") or s.get("text") or "").strip().split("\n")[0][:120]
            notes = re.sub(r"\s+", " ", (s.get("speaker_notes") or "").strip())[:400]
            lines.append(f"[{i}] {title}\n    notes: {notes}")
        user = (f"Deck slides:\n\n" + "\n".join(lines) +
                f"\n\nChoose AT MOST {budget} slide(s) to animate. Return the JSON array.")
        try:
            resp = self.call(messages=[{"role": "user", "content": user}],
                             max_tokens=2048, disable_thinking=True)
            text = "".join(b.text for b in resp.content if b.type == "text")
        except Exception:
            return []
        picked = _parse(text)
        out: list[dict] = []
        seen: set[int] = set()
        for p in picked:
            if not isinstance(p, dict):
                continue
            try:
                idx = int(p.get("index"))
            except (TypeError, ValueError):
                continue
            desc = str(p.get("description") or "").strip()
            if idx < 0 or idx >= len(slides) or idx in seen or not desc:
                continue
            seen.add(idx)
            title = (slides[idx].get("title") or slides[idx].get("text") or "").strip().split("\n")[0][:120]
            out.append({"rendered_index": idx, "title": title, "description": desc})
            if len(out) >= budget:
                break
        return out


def _parse(text: str) -> list:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, list) else []
            except json.JSONDecodeError:
                return []
    return []
