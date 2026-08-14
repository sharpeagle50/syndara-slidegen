"""Narration expressiveness tagging for ElevenLabs v3 (the "Expressive" narration tier).

Plain speaker notes underuse ElevenLabs v3 — its distinctive feature is inline audio tags
(``[warmly]``, ``[pause]``, ``[emphasizes]`` …) that direct delivery. This module inserts those
tags in one cheap LLM pass, gated to v3 ONLY (on v2 or OpenAI TTS the brackets would be read
aloud literally). Delivery is RESTRAINED and teaching-appropriate by default; it escalates to a
livelier, entertaining, or humorous style ONLY when the creator's own topic/context asks for it.
On any failure the original narration is returned unchanged, so audio always generates.
"""
from .base import BaseAgent, extract_json, text_from_response

_TAG_SYSTEM = """You annotate course narration with ElevenLabs v3 audio tags so a text-to-speech
model delivers it more naturally. You are given one deck's slide narration and return the SAME
narration with sparse inline tags inserted. NEVER change the words — only insert bracketed tags.

Tags are inline square-bracket delivery cues v3 understands, e.g. [warmly], [thoughtfully], [curious],
[reassuring], [gently], [excited], [laughs softly]. They direct delivery and are NOT spoken aloud.

For EMPHASIS and PAUSES, do NOT use tags — v3 handles these better through the text itself:
CAPITALIZE the one or two words that carry the emphasis, and use an ellipsis (…) for a beat or pause.
Never write [emphasizes] or [pause].

DEFAULT = RESTRAINED. This is professional adult education — sound like an excellent human
instructor: warm, clear, measured. Use only subtle cues ([warmly], [thoughtfully], [curious],
[reassuring], [gently]) and use them SPARINGLY: most sentences get NO tag.
Over-tagging sounds robotic and theatrical. Do NOT use comedic or dramatic tags ([laughs out loud],
[shouting], [gasps], [sarcastic]) by default.

ESCALATE ONLY IF THE CREATOR ASKED FOR IT. Read the course topic and context. If the creator
explicitly wants a livelier, entertaining, humorous, energetic, or influencer-style delivery
(e.g. "very expressive", "make it fun", "entertaining", "with jokes/humor", "like an influencer"),
match that: richer emotional range, enthusiasm, and the occasional [laughs]/[excited] where it fits.
The creator's stated intent is the CEILING — never exceed the energy they asked for, and if they say
nothing about tone, stay restrained.

Return ONLY a JSON object {"narration": [...]} with EXACTLY as many strings as you were given, in the
same order, each the original slide's text with tags inserted (words unchanged)."""


class NarrationTagAgent(BaseAgent):
    """Cheap single-call annotator — the judgement is light and the output short, so run it on Haiku."""
    allowed_tool_names: list[str] = []
    system_prompt = _TAG_SYSTEM
    model = "claude-haiku-4-5"


def tag_narration_v3(notes: list[str], topic: str = "", context: str = "") -> list[str]:
    """Return ``notes`` with v3 audio tags inserted (restrained unless the context asks for more).

    Aligned 1:1 to the input; on ANY problem the input is returned unchanged so audio still
    generates. Batched in small chunks to stay well within a small model's output budget and so a
    single bad chunk can't drop the whole deck to plain narration.
    """
    import json as _json
    if not notes:
        return notes
    topic = (topic or "").strip()[:300]
    ctx = (context or "").strip()[:4000]
    agent = NarrationTagAgent()
    out: list[str] = []
    CHUNK = 12
    for start in range(0, len(notes), CHUNK):
        chunk = notes[start:start + CHUNK]
        prompt = (
            f"COURSE TOPIC: {topic or '(untitled)'}\n"
            f"CREATOR CONTEXT (use ONLY to judge how expressive the creator wants it):\n"
            f"{ctx or '(none given — stay restrained)'}\n\n"
            f"Annotate each of these {len(chunk)} narration strings. Return ONLY "
            f'{{"narration": [...]}} with EXACTLY {len(chunk)} strings, same order, words unchanged:\n\n'
            + _json.dumps({"narration": chunk}, ensure_ascii=False)
        )
        try:
            msg = agent.call(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                disable_thinking=True,
            )
            data = extract_json(text_from_response(msg)) or {}
            tagged = data.get("narration")
            if (isinstance(tagged, list) and len(tagged) == len(chunk)
                    and all(isinstance(x, str) and x.strip() for x in tagged)):
                out.extend(tagged)
            else:
                out.extend(chunk)   # shape mismatch → this chunk stays plain
        except Exception as e:
            print(f"[NarrationTags] chunk tagging failed ({type(e).__name__}: {e}); plain narration", flush=True)
            out.extend(chunk)
    return out if len(out) == len(notes) else notes
