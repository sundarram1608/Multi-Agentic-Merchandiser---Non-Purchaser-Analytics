"""
agents/output_reviewer.py
-------------------------
Runs after sql_executor succeeds. Two jobs:
  1. Decide if the rows semantically answer the user's question.
  2. Decide if a visualization would help (triggers the viz sub-pipeline).
"""

from __future__ import annotations

import json

from agents.llm import call_llm_json, load_prompt, MODEL_HAIKU
from agents.schemas import OutputReviewerResult


ALLOWED_VERDICTS = {"ok", "retry"}


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("output_reviewer")
    return _SYSTEM_PROMPT


def review(
    resolved_question: str,
    sql: str,
    rows: list[dict],
    columns: list[str],
    original_user_message: str | None = None,
) -> OutputReviewerResult:
    preview = rows[:30]

    parts = [f"## Resolved question\n{resolved_question.strip()}"]
    if original_user_message and original_user_message.strip() != resolved_question.strip():
        parts.append(f"## Original user message\n{original_user_message.strip()}")
    parts.append(f"## SQL\n{sql.strip()}")
    parts.append(
        f"## Result preview (first 30 rows; total = {len(rows)})\n"
        f"Columns: {', '.join(columns)}\n"
        f"Rows: {json.dumps(preview, default=str, indent=2)}"
    )
    parts.append("Return the JSON object.")
    user_block = "\n\n".join(parts)

    raw = call_llm_json(
        system=_system_prompt(),
        user=user_block,
        model=MODEL_HAIKU,
        temperature=0.0,
        max_tokens=300,
    )

    verdict = raw.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError(f"OutputReviewer returned invalid verdict: {verdict!r}")

    return OutputReviewerResult(
        verdict=verdict,
        feedback=str(raw.get("feedback", "")).strip(),
        viz_applies=bool(raw.get("viz_applies", False)),
    )
