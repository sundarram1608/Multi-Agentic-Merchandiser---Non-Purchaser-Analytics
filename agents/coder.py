"""
agents/coder.py
---------------
Turns a natural-language question into a SELECT SQL statement.

The Coder's raw output is plain text containing TWO blocks:
  1. <reasoning>...</reasoning> — chain-of-thought analysis with four
     named lines (Dimensions, Filters, Aggregation, Completeness). Forces
     the model to commit to a structural reading before writing SQL.
  2. <sql_query>...</sql_query> — the SELECT statement itself.

Both are extracted by `tools.sql_tools.sql_parser`, which returns a
`ParseResult` with `.sql` and `.reasoning` populated. The Orchestrator
logs the reasoning into the step trace (visible in the "How I got this
answer" expander) and passes it to the Code Reviewer so the reviewer
can verify the SQL actually implements the stated reasoning (catches
contradictions like "reasoning says no LIMIT but SQL has LIMIT").

Model: Sonnet 4.6 (the SQL-quality bottleneck — Haiku struggled with
MySQL window-function syntax and with the schema's non_purchase_type vs
attribute_value disambiguation). The prompt is re-read from disk on
every call (no module-level cache) so prompt edits in
`agents/prompts/coder.txt` take effect on the next chat turn without a
full Streamlit restart.
"""

from __future__ import annotations

from agents.llm import call_llm, load_prompt, MODEL_SONNET
from tools.sql_tools import get_schema_for_prompt


def _system_prompt() -> str:
    """Re-read the prompt on every call.

    The previous implementation cached the substituted prompt in a
    module-level `_SYSTEM_PROMPT` variable on first invocation. That
    cache persists across Streamlit hot-reloads (Streamlit reloads
    .py files but does NOT re-import modules), which meant prompt
    edits to `agents/prompts/coder.txt` silently had no effect until
    the entire Streamlit process was killed and re-launched.

    For a system where prompt-engineering is the primary iteration
    loop, "edit and refresh" beats the few-hundred-microsecond
    saving of caching. We re-read from disk every call.
    """
    raw = load_prompt("coder")
    return raw.replace("{SCHEMA}", get_schema_for_prompt())


def write_sql(
    resolved_question: str,
    *,
    feedback: str | None = None,
    previous_sql: str | None = None,
) -> str:
    """
    Inputs
    ------
    resolved_question : the context-resolved question from the Planner
    feedback          : (optional) reviewer feedback from a prior failed attempt
    previous_sql      : (optional) the SQL string the reviewer rejected

    Returns
    -------
    The Coder's raw text output, still containing <sql_query>...</sql_query>.
    The Orchestrator runs sql_parser() to extract the clean SQL.
    """

    parts = [f"## User question\n{resolved_question.strip()}"]

    if feedback and previous_sql:
        parts.append(
            "## Previous attempt (was rejected)\n"
            f"SQL:\n{previous_sql}\n\n"
            f"Reviewer feedback:\n{feedback}\n\n"
            "Write a NEW SQL that addresses this feedback. Do not repeat the prior attempt."
        )

    parts.append("Return the SQL wrapped in <sql_query>...</sql_query>.")

    user_block = "\n\n".join(parts)
    return call_llm(
        system=_system_prompt(),
        user=user_block,
        # Sonnet 4.6 — Coder is the SQL-quality bottleneck. Haiku was
        # consistently failing on MySQL window-function syntax and on the
        # non_purchase_type vs attribute_value disambiguation. Sonnet handles
        # both cases reliably under the same scaffolding.
        model=MODEL_SONNET,
        temperature=0.0,
        # Bumped from 600 — the new <reasoning> block adds ~200 tokens
        # before the SQL itself starts.
        max_tokens=900,
    )
