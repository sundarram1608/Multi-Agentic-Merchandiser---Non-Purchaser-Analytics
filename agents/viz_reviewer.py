"""
agents/viz_reviewer.py
----------------------
Multimodal review of the rendered chart PNG. Uses native Anthropic SDK via
agents.llm.call_llm_vision_json.
"""

from __future__ import annotations

from agents.llm import call_llm_vision_json, load_prompt
from agents.schemas import VizReviewerResult


ALLOWED = {"ok", "revise", "drop"}


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("viz_reviewer")
    return _SYSTEM_PROMPT


def review(image_png: bytes, resolved_question: str) -> VizReviewerResult:
    user_text = (
        f"## Resolved question\n{resolved_question.strip()}\n\n"
        f"You can see the rendered chart attached. Return the JSON verdict."
    )
    raw = call_llm_vision_json(
        system=_system_prompt(),
        user_text=user_text,
        image_png=image_png,
        temperature=0.0,
        max_tokens=300,
    )
    verdict = raw.get("verdict")
    if verdict not in ALLOWED:
        raise ValueError(f"VizReviewer invalid verdict: {verdict!r}")
    return VizReviewerResult(
        verdict=verdict,
        feedback=str(raw.get("feedback", "")).strip(),
    )
