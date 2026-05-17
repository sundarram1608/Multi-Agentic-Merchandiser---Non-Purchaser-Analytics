"""
agents/viz_code_reviewer.py
---------------------------
Static review of matplotlib code BEFORE it runs in the sandbox.
"""

from __future__ import annotations

from agents.llm import call_llm_json, load_prompt, MODEL_HAIKU
from agents.schemas import VizCodeReviewerResult


ALLOWED = {"ok", "retry"}


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("viz_code_reviewer")
    return _SYSTEM_PROMPT


def review(viz_code: str, resolved_question: str) -> VizCodeReviewerResult:
    user_block = (
        f"## Resolved question\n{resolved_question.strip()}\n\n"
        f"## Viz code\n{viz_code}\n\n"
        f"Return the JSON object."
    )
    raw = call_llm_json(
        system=_system_prompt(),
        user=user_block,
        model=MODEL_HAIKU,
        temperature=0.0,
        max_tokens=250,
    )
    verdict = raw.get("verdict")
    if verdict not in ALLOWED:
        raise ValueError(f"VizCodeReviewer invalid verdict: {verdict!r}")
    return VizCodeReviewerResult(
        verdict=verdict,
        feedback=str(raw.get("feedback", "")).strip(),
    )
