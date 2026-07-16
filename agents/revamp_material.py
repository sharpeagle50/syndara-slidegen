"""Revamp material agent — a flexible, autonomous author for one module's hands-on material.

Given a module's slides + whatever practice material the course already had (a Jupyter
notebook, a reading, an existing exercise, or nothing), it decides the BEST modernized
material for today's learners with wide latitude: a graded coding exercise, a run-and-read
notebook demo, a reformatted reading, a suggested better resource/video, a scenario
walkthrough — zero, one, or several. It is deliberately NOT constrained to rigid categories
or a fixed count per module.

Freedom is only in the AUTHORING. Whatever it emits still passes the unchanged downstream
quality gates: 'notebook' exercises are executed (GPU when needed) + AI-reviewed + fixed via
HandsOnAgent._finalize_notebook_solution; text exercises are fact-checked. This agent decides
WHAT to make; the existing pipeline proves it's correct.

Renderable forms (the learner runtime special-cases only 'notebook'; everything else renders
through the generic holistically-graded text view):
  - "notebook"  -> code, gets executed + reviewed
  - any text type ("reading", "tutorial", "scenario_walkthrough", "build_challenge",
    "troubleshooting", "practice", "roleplay", ...) -> markdown, fact-checked
"""
from __future__ import annotations

import json

from .base import BaseAgent, extract_json, STYLE_RULE, strip_em_dashes_deep


REVAMP_MATERIAL_SYSTEM = """You are revamping the hands-on material for ONE module of an existing
course. You get the module's slides and whatever practice material it already had (a Jupyter
notebook, a worksheet or document, a reading, an existing exercise, or nothing). A document may
already be a "do X and submit Y" exercise — preserve that intent while modernizing it.

Decide the BEST modernized material for today's learners ({today}). Use your judgment — you are
NOT filling in a rigid template:
- Keep what still teaches well. Modernize ONLY what is genuinely outdated (deprecated libraries or
  APIs, stale practices, dead tools/links). Don't rewrite good material for its own sake.
- Preserve the original's intent, topic, and difficulty unless those are what's outdated.
- The material can be a graded coding exercise, a run-and-read notebook demo, a build challenge,
  a scenario walkthrough, a short reading (you may reformat it or point to a better current
  resource/video), a roleplay, or NOTHING if the module genuinely needs no hands-on work.
  Zero, one, or several items — whatever actually serves the learner.
- Use web_search to confirm what's current whenever you touch anything technical or factual.

RENDERABLE FORMS — pick the best fit per item:
- Coding material -> type "notebook". Provide "notebook_solution" as a COMPLETE, RUNNABLE .ipynb
  JSON object (it WILL be executed and reviewed downstream, so it must run clean on a fresh machine).
  The two most common notebook archetypes:
    (1) MOST COMMON — a GRADED exercise (a student/solution PAIR): tag each cell the learner fills in
        with metadata.syndara_learner={"hint":"..."}; the student version is auto-derived by stubbing
        those into blanks/TODOs.
    (2) 2ND MOST COMMON — a run-and-read DEMO: tag nothing; the whole notebook ships as-is to run through.
  Think critically — if the material is better as some other kind of notebook, build that; don't force
  it into one of these two.
- Anything else -> a text-based exercise. It is HOLISTICALLY GRADED, so it must have a concrete task
  and an answer_key rubric. The learner sees ONLY "scenario" + "task": put a one-paragraph real-world
  framing in "scenario" and the COMPLETE, self-contained exercise in "task" as markdown — what to do,
  any resource links or a suggested video, and exactly what to submit. Label "type" with the best fit:
  "submission" (a do-and-submit task — the default), "tutorial" (guided walkthrough of a real platform),
  or "reading" (a reading — still add a short applied task to submit so it can be graded).

Return ONLY JSON (no fences):
{
  "exercises": [
    // coding:
    {"type":"notebook","title":"...","scenario":"...","task":"...","expected_outputs":["..."],
     "tools_to_use":["..."],"time_estimate":"...","notebook_solution":{<full .ipynb JSON>},
     "answer_key":{"rubric":["..."],"expected_results":"..."}}
    // or text-based (content lives in scenario + task; holistically graded):
    {"type":"submission","title":"...","scenario":"...","task":"<full markdown: what to do + submit>",
     "expected_outputs":["..."],"answer_key":{"rubric":["..."],"expected_results":"..."},
     "time_estimate":"..."}
  ],
  "notes": "one line on what you changed and why (or 'kept as-is')"
}
Return {"exercises": []} if the module genuinely needs no hands-on material."""


REWORK_NOTEBOOK_SYSTEM = """You are reworking a hands-on Jupyter SOLUTION notebook so it COHERES with
the module's UPDATED lesson. Today is {today}. You get (1) what the module's slides now teach, and
(2) the current solution notebook. Produce a reworked COMPLETE, RUNNABLE solution notebook that:

- APPLIES and BUILDS ON what the lesson now teaches — use the same concepts, tools, and approach the
  slides present, in the order they build up. A learner who just finished the slides should recognize
  this notebook as the natural next step.
- May go a little BEYOND the slides (a natural extension that deepens the same skill), but stays on
  the lesson's topic and difficulty level.
- UPDATES the notebook's approach/flow where the lesson changed HOW something is done — not just
  deprecated-API swaps: if the slides now teach a newer method/pattern, the notebook should use it.
- ADDS a visualization/plot (matplotlib etc.) where it genuinely aids understanding.
- MODERNIZES outdated code — web_search to confirm current APIs; use current idioms.
- PRESERVES the exercise's core: the graded TASK (what the learner must do / build / submit) and its
  difficulty. Do NOT gratuitously rewrite cells that are already correct and on-lesson.

NOTEBOOK ARCHETYPE — recognize which one this is and KEEP its form:
- MOST COMMON — a GRADED exercise (a student/solution PAIR): a SOLUTION notebook where the cells the
  learner fills in are tagged with metadata.syndara_learner={"hint":"..."} (the student version is
  auto-derived by stubbing those cells into blanks/TODOs). PRESERVE that tagging — keep the tags on the
  cells that ARE the exercise; if the reworked task moves what the learner does, move/add the tags
  accordingly so the student↔solution pair still derives correctly. NEVER strip the tags off a graded
  notebook (that would silently turn it into a demo).
- 2ND MOST COMMON — a run-and-read DEMO: no learner tags; the whole notebook ships as-is and the learner
  just runs it end to end. Keep it tag-free.
- Some notebooks are neither — think critically and preserve whatever makes it work; don't force a mold.

Return ONLY JSON: {"notebook_solution": {<full .ipynb JSON with cells + metadata>}, "notes": "one
line on what you changed"}. If the notebook already coheres and needs nothing, return it unchanged."""


REWORK_EXERCISE_SYSTEM = """You are reworking a TEXT hands-on exercise (a written task the learner
does and submits — e.g. a build challenge, scenario, analysis, troubleshooting, or reading+response)
so it COHERES with the module's UPDATED lesson. Today is {today}. You get what the slides now teach
and the current exercise. Produce a reworked exercise that:

- APPLIES and BUILDS ON what the lesson now teaches — the scenario/task should exercise the concepts,
  tools, and approach the slides present; a learner who just finished the slides should see it as the
  natural application. It may go a little beyond (a natural extension) at the same level.
- UPDATES the task where the lesson changed the approach — don't keep asking the learner to apply a
  method the slides no longer teach.
- MODERNIZES any outdated facts, tools, or references (web_search to confirm what's current).
- PRESERVES the exercise's core: exactly what the learner must DO and SUBMIT, the grading intent, and
  the difficulty. Keep the SAME exercise type. Don't gratuitously rewrite a task that already fits.

Return ONLY JSON: the full exercise object with the SAME shape and "type" it came in as (keep type,
title, scenario, task, expected_outputs, tools_to_use, time_estimate, and the answer_key/rubric).
If it already coheres, return it unchanged."""


class RevampMaterialAgent(BaseAgent):
    allowed_tool_names = ["web_search", "web_fetch"]
    system_prompt = REVAMP_MATERIAL_SYSTEM

    def author(
        self,
        module_title: str,
        deck_text: str,
        original_material: dict | str | list | None,
        today: str,
        course_title: str = "",
        reviewer_feedback: str = "",
        must_have_exercise: bool = False,
        course_guidance: str = "",
        prior_context: str = "",
    ) -> dict:
        """Author 0..N modernized exercises for a module. `original_material` is the uploaded
        notebook (dict), a reading/exercise (str), or None. Returns
        {"exercises": [<normalized exercise dicts>], "notes": str}. Each exercise is a proposal;
        the orchestrator runs it through the matching quality gate (execute+review / fact-check)."""
        # Substitute {today} into THIS instance's prompt (agents are one-shot per module, so
        # mutating the instance attr is safe) — avoids double-appending via extra_system.
        self.system_prompt = REVAMP_MATERIAL_SYSTEM.replace("{today}", today) + STYLE_RULE

        if isinstance(original_material, list):
            # Several uploaded files for this module — show them all; author the best exercise(s)
            # using them together (e.g. a notebook + a dataset, or multiple worksheets).
            _parts = []
            for _k, _mat in enumerate(original_material):
                if isinstance(_mat, dict):
                    _parts.append(f"[Material {_k + 1} — a notebook / structured exercise]\n" + json.dumps(_mat)[:40000])
                elif isinstance(_mat, str) and _mat.strip():
                    _parts.append(f"[Material {_k + 1} — a worksheet / reading / document]\n" + _mat[:20000])
            orig_block = (("ORIGINAL MATERIALS (this module's practice files — modernize them into the "
                           "best exercise(s), using ALL of them together; preserve any 'do X, submit Y' "
                           "intent):\n\n" + "\n\n".join(_parts)[:60000]) if _parts
                          else "ORIGINAL MATERIAL: (none — the module had no hands-on material).")
        elif isinstance(original_material, dict):
            # A notebook (or structured exercise) — hand over the JSON, trimmed for the prompt.
            orig_block = ("ORIGINAL MATERIAL (a notebook / structured exercise — modernize its code "
                          "and framing as needed):\n" + json.dumps(original_material)[:60000])
        elif isinstance(original_material, str) and original_material.strip():
            orig_block = ("ORIGINAL MATERIAL (a worksheet / reading / document — modernize it into "
                          "the best exercise, preserving any 'do X, submit Y' intent):\n" + original_material[:30000])
        else:
            orig_block = "ORIGINAL MATERIAL: (none — the module had no hands-on material)."

        user = (
            f"Course: {course_title or '(untitled)'}\nModule: {module_title or '(untitled)'}\n\n"
            f"MODULE SLIDES (for context — do not turn these into the exercise):\n{deck_text[:20000]}\n\n"
            f"{prior_context}"
            f"{orig_block}\n\nAuthor the best modernized hands-on material for this module."
        )
        if must_have_exercise:
            user += ("\n\nThis module ALREADY HAD hands-on material, so you MUST return AT LEAST ONE "
                     "exercise. Keep the same number and type in almost all cases — modernize what's "
                     "outdated. Only change the number or type if the original is SO outdated it is "
                     "structurally incompatible with how this is taught today; even then, never leave "
                     "the module with zero exercises.")
        else:
            user += ("\n\nThis module had NO hands-on material. Add an exercise ONLY if the slides "
                     "genuinely warrant hands-on practice; otherwise return an empty list.")
        if course_guidance:
            user += ("\n\nCREATOR GUIDANCE (from course feedback about this module's material — apply "
                     f"where it fits the exercise(s) you author; ignore parts that don't):\n{course_guidance}")
        if reviewer_feedback:
            user += ("\n\nA REVIEWER FACT-CHECKED your previous version and flagged issues — fix these "
                     f"EXACTLY (verify with web_search), keeping everything else:\n{reviewer_feedback[:4000]}")
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 8, "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4, "allowed_callers": ["direct"]},
        ]
        try:
            final_text, _ = self.run_tool_loop(
                messages=[{"role": "user", "content": user}], tools=tools,
                tool_handlers={}, max_tokens=32000, trace_label="RevampMaterial",
            )
            d = extract_json(final_text)
            if isinstance(d, dict) and isinstance(d.get("exercises"), list):
                return {"exercises": [strip_em_dashes_deep(self._normalize(e)) for e in d["exercises"] if isinstance(e, dict)],
                        "notes": str(d.get("notes", ""))[:500]}
        except Exception as e:
            print(f"[RevampMaterial] failed: {e!r}", flush=True)
        return {"exercises": [], "notes": ""}

    def rework_notebook(self, notebook: dict, lesson_text: str, today: str,
                        module_title: str = "", guidance: str = "", prior_context: str = "") -> dict | None:
        """Rework a solution notebook so it applies/builds on the module's reworked lesson (update
        flows to match how it's now taught, add a visual where it helps, modernize) while preserving
        the graded task. Returns the reworked .ipynb dict, or None to keep the original. Web-enabled;
        the caller still executes + reviews + fixes the result."""
        self.system_prompt = REWORK_NOTEBOOK_SYSTEM.replace("{today}", today) + STYLE_RULE
        _g = f"\n\nCREATOR GUIDANCE:\n{guidance}" if guidance else ""
        user = (f"MODULE: {module_title or '(untitled)'}\n\n"
                f"WHAT THE SLIDES NOW TEACH (the reworked lesson):\n{(lesson_text or '')[:24000]}\n\n"
                f"{prior_context}"
                f"CURRENT SOLUTION NOTEBOOK (.ipynb JSON):\n{json.dumps(notebook)[:60000]}{_g}\n\n"
                "Rework the notebook so it coheres with the lesson.")
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 8, "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4, "allowed_callers": ["direct"]},
        ]
        try:
            final_text, _ = self.run_tool_loop(
                messages=[{"role": "user", "content": user}], tools=tools,
                tool_handlers={}, max_tokens=32000, trace_label="NotebookRework")
            d = extract_json(final_text)
            nb = d.get("notebook_solution") if isinstance(d, dict) else None
            if isinstance(nb, dict) and isinstance(nb.get("cells"), list) and nb["cells"]:
                return strip_em_dashes_deep(nb)
        except Exception as e:
            print(f"[NotebookRework] failed: {e!r}", flush=True)
        return None

    def rework_exercise(self, exercise: dict, lesson_text: str, today: str,
                        module_title: str = "", guidance: str = "", prior_context: str = "") -> dict | None:
        """Rework a TEXT exercise so it applies/builds on the module's reworked lesson while preserving
        the graded task, type, and difficulty. Returns the reworked exercise dict, or None to keep the
        original. Web-enabled; the caller still runs the fact-check gate."""
        self.system_prompt = REWORK_EXERCISE_SYSTEM.replace("{today}", today) + STYLE_RULE
        _g = f"\n\nCREATOR GUIDANCE:\n{guidance}" if guidance else ""
        _pub = {k: v for k, v in exercise.items() if not str(k).startswith("_")}
        user = (f"MODULE: {module_title or '(untitled)'}\n\n"
                f"WHAT THE SLIDES NOW TEACH (the reworked lesson):\n{(lesson_text or '')[:24000]}\n\n"
                f"{prior_context}"
                f"CURRENT EXERCISE (JSON):\n{json.dumps(_pub)[:20000]}{_g}\n\n"
                "Rework the exercise so it coheres with the lesson.")
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 6, "allowed_callers": ["direct"]},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3, "allowed_callers": ["direct"]},
        ]
        try:
            final_text, _ = self.run_tool_loop(
                messages=[{"role": "user", "content": user}], tools=tools,
                tool_handlers={}, max_tokens=16000, trace_label="ExerciseRework")
            d = extract_json(final_text)
            if isinstance(d, dict) and d.get("type") and d.get("type") != "notebook" and (d.get("task") or d.get("scenario")):
                return strip_em_dashes_deep(d)
        except Exception as e:
            print(f"[ExerciseRework] failed: {e!r}", flush=True)
        return None

    @staticmethod
    def _normalize(e: dict) -> dict:
        """Coerce an authored exercise into the shape the pipeline expects, without forcing a
        category. Notebook items keep their notebook_solution for the execution gate; text items
        keep instructions_markdown for the fact-check gate + generic render."""
        etype = str(e.get("type") or "reading").strip() or "reading"
        out: dict = {
            "type": etype,
            "title": str(e.get("title", ""))[:300],
            "scenario": str(e.get("scenario", ""))[:8000],
            "task": str(e.get("task", ""))[:20000],   # text exercises render from scenario + task
            "expected_outputs": [str(x)[:500] for x in (e.get("expected_outputs") or [])][:20],
            "tools_to_use": [str(x)[:120] for x in (e.get("tools_to_use") or [])][:20],
            "time_estimate": str(e.get("time_estimate", ""))[:80],
            "answer_key": e.get("answer_key") if isinstance(e.get("answer_key"), dict) else {},
        }
        if etype == "notebook":
            sol = e.get("notebook_solution")
            out["notebook_solution"] = sol if isinstance(sol, dict) else None
        return out
