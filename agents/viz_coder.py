"""
agents/viz_coder.py
-------------------
Writes a short matplotlib snippet wrapped in <viz_code>...</viz_code>.
Orchestrator extracts via viz_code_parser and runs in viz_generator sandbox.
"""

from __future__ import annotations

import json

from agents.llm import call_llm, load_prompt, MODEL_HAIKU


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("viz_coder")
    return _SYSTEM_PROMPT


def write_viz_code(
    resolved_question: str,
    rows: list[dict],
    columns: list[str],
    *,
    feedback: str | None = None,
    previous_code: str | None = None,
) -> str:
    preview = rows[:30]

    parts = [
        f"## Resolved question\n{resolved_question.strip()}",
        f"## Columns\n{', '.join(columns)}",
        f"## Rows (first 30)\n{json.dumps(preview, default=str, indent=2)}",
    ]
    if rows and len(rows) > 30:
        parts.append(f"(total rows: {len(rows)})")

    if feedback and previous_code:
        parts.append(
            "## Previous attempt (rejected)\n"
            f"Code:\n{previous_code}\n\n"
            f"Reviewer feedback:\n{feedback}\n\n"
            "Write NEW code that addresses the feedback."
        )

    parts.append("Return the code wrapped in <viz_code>...</viz_code>.")
    user_block = "\n\n".join(parts)

    return call_llm(
        system=_system_prompt(),
        user=user_block,
        model=MODEL_HAIKU,
        temperature=0.0,
        max_tokens=900,
    )
