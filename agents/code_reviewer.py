"""
agents/code_reviewer.py
-----------------------
Judges SQL technical correctness AND — when the Coder's `<reasoning>`
block is provided — verifies that the SQL actually implements the
reasoning. Catches contradictions like "reasoning says no LIMIT but
SQL has LIMIT N" or "reasoning lists 3 dimensions but GROUP BY has 2".

Called by the Orchestrator twice per loop iteration:
  - Static review:    SQL right after the Coder produces it.
  - Post-exec review: with `execution_error` filled in when sql_executor
                      returned a DB error.
Both passes use the same system prompt.

Model: Sonnet 4.6 (paired with the Sonnet-Coder so the reviewer is at
the same competence tier as what it reviews — a weaker reviewer
can't catch a stronger coder's mistakes). Prompt is re-read from disk
on every call (no module-level cache); see `agents/coder.py` for the
rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.llm import call_llm_json, load_prompt, MODEL_SONNET
from tools.sql_tools import get_schema_for_prompt


def _system_prompt() -> str:
    """Re-read on every call (see `agents/coder.py` for the rationale —
    prompt-engineering iteration loop beats a few-hundred-microsecond
    cache when prompt edits otherwise need a full Streamlit restart to
    take effect)."""
    raw = load_prompt("code_reviewer")
    return raw.replace("{SCHEMA}", get_schema_for_prompt())


ALLOWED_VERDICTS = {"ok", "retry"}


@dataclass
class CodeReviewerResult:
    verdict: str       # "ok" or "retry"
    feedback: str      # empty when verdict == "ok"


def review(
    user_question: str,
    sql: str,
    execution_error: str | None = None,
    reasoning: str | None = None,
) -> CodeReviewerResult:
    parts = [
        f"## User question\n{user_question.strip()}",
    ]
    if reasoning:
        parts.append(
            "## Coder's reasoning\n"
            f"{reasoning.strip()}\n\n"
            "Verify the SQL below actually implements this reasoning. If the "
            "reasoning says 'no LIMIT' but the SQL has LIMIT, flag it. If the "
            "reasoning lists N dimensions but the SQL only groups by fewer, "
            "flag it. Quote the specific contradiction in feedback."
        )
    parts.append(f"## SQL\n{sql.strip()}")
    if execution_error:
        parts.append(f"## Execution error\n{execution_error.strip()}")

    user_block = "\n\n".join(parts) + "\n\nReturn the JSON object."

    raw = call_llm_json(
        system=_system_prompt(),
        user=user_block,
        # Sonnet 4.6 — paired with the Sonnet-Coder. A reviewer weaker
        # than the coder it reviews can't catch the coder's mistakes;
        # both share this tier so they catch each other's subtleties.
        model=MODEL_SONNET,
        temperature=0.0,
        max_tokens=400,
    )

    verdict = raw.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError(f"Code Reviewer returned invalid verdict: {verdict!r}")

    return CodeReviewerResult(
        verdict=verdict,
        feedback=str(raw.get("feedback", "")).strip(),
    )
