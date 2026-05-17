"""
agents/orchestrator.py
----------------------
Rule-based Orchestrator — runs the full chat workflow:

  Planner → (sql path) → Coder ↔ Code Reviewer ↔ sql_executor
          → Output Reviewer (semantic + viz decision)
          → Writer ↔ groundedness_check (hard-block retry loop)
          → (sequential) Viz Coder ↔ Viz Code Reviewer
                       → viz_generator (sandbox) → Viz Reviewer
          → to_excel (whenever row_count > 0)
          → AgentResponse(text, sql, chart_png, viz_code, excel_bytes, steps)
          → trace_logger.log_turn → INSERT into chat_trace (fail-safe)

Retry caps:
  Coder ↔ Code Reviewer            : 5    → exhaustion → Supervisor
  Output Reviewer ↔ Coder           : 2    → exhaustion → Supervisor
  Writer ↔ groundedness (hard block) : 2    → exhaustion → ship GROUNDEDNESS_FAIL_TEXT (no Supervisor)
  Viz Coder ↔ Viz Code Reviewer     : 2    → exhaustion → drop chart, ship text only (no Supervisor)
  Viz Reviewer revise               : 1    → exhaustion → ship last PNG, or drop on verdict='drop'

Helpers:
  _step                  emit a StepEvent into the running trace list
  _attach_excel          build .xlsx download bytes + emit to_excel step
                         (used at all 3 SQL-path return sites — happy path,
                         supervisor ship_partial, supervisor retry_with_strategy)
  _write_grounded_answer Writer ↔ groundedness loop with hard-block exhaustion
                         (used at all 3 SQL-path return sites for the same reason)

Public entry:
  run(user_message, history) — wraps _run_impl and persists the turn to
                               chat_trace via trace_logger.log_turn at the
                               end (try/except so logging can't break the chat)
"""

from __future__ import annotations

from dataclasses import asdict

from agents.schemas import AgentResponse, StepEvent
from agents.planner import plan as planner_plan
from agents.coder import write_sql
from agents.code_reviewer import review as code_review
from agents.output_reviewer import review as output_review
from agents.writer import write_answer
from agents.viz_coder import write_viz_code
from agents.viz_code_reviewer import review as viz_code_review
from agents.viz_reviewer import review as viz_review
from agents.supervisor import decide as supervisor_decide
from tools import (
    sql_parser,
    sql_safety_guard,
    sql_executor,
    viz_code_parser,
    viz_generator,
    to_excel,
    groundedness_check,
    safe_derivations_summary,
    log_turn,
)


MAX_CODE_REVIEW_RETRIES = 5
MAX_OUTPUT_REVIEW_RETRIES = 2
MAX_VIZ_CODE_RETRIES = 2
MAX_VIZ_REVISE = 1
MAX_GROUNDEDNESS_RETRIES = 2     # total Writer attempts when groundedness fails


# Standard failure text used when the groundedness retry budget is exhausted.
# Surfaces the same in the chat bubble and in the chat_trace `answer_text`.
#
# Note: this string does NOT mention specific UI elements (Show SQL,
# Download Excel) because those affordances are conditional — the Excel
# button only appears when `to_excel` succeeds (e.g., openpyxl is
# installed), the SQL expander only when `resp.sql` is set, etc. The
# Streamlit frontend renders whichever ones are present; promising
# specific ones here risks the same "Writer hallucinated a button"
# failure mode that the writer.txt UI-affordance rule guards against.
GROUNDEDNESS_FAIL_TEXT = (
    "I couldn't produce an answer with verifiable numbers after "
    f"{MAX_GROUNDEDNESS_RETRIES} attempts — every draft I wrote cited "
    "values that don't appear in the underlying data. The SQL I ran and "
    "the rows it returned are surfaced under this message when available; "
    "please inspect the data directly to answer this question."
)


def _step(steps, agent, status, summary, **detail):
    steps.append(StepEvent(agent=agent, status=status, summary=summary, detail=detail))


def _attach_excel(result: dict, steps: list[StepEvent]) -> bytes | None:
    """Build the .xlsx download bytes for a successful SQL result and
    emit a `to_excel` step event.

    Returns None when there are no rows, when the SQL didn't succeed,
    or when the `to_excel` tool itself failed (e.g. openpyxl missing).
    Used by both the happy path (`_finalize_sql_answer`) and the
    Supervisor recovery branches (`ship_partial`,
    `retry_with_strategy`) so every grounded SQL answer in the system
    carries the same data-download affordance.

    The helper emits one of three step events:
      - `to_excel · ok`   — bytes produced successfully
      - `to_excel · fail` — tool returned `b""`; reads
                            `excel_tools.LAST_TO_EXCEL_ERROR` to
                            include the actual exception in the
                            step summary so the user can see what
                            went wrong in the trace expander
      - (no event)        — no rows to export, nothing to log
    """
    if not result or not result.get("ok") or result.get("row_count", 0) <= 0:
        return None
    xl = to_excel(result["rows"], result["columns"])
    if not xl:
        # Surface the failure in the trace — until this was wired in,
        # `to_excel` would silently swallow exceptions (e.g. missing
        # openpyxl) and the user would see the Writer promising an
        # Excel button that never appeared, with no diagnostic.
        from tools.excel_tools import LAST_TO_EXCEL_ERROR
        reason = LAST_TO_EXCEL_ERROR or "to_excel returned empty bytes"
        _step(steps, "to_excel", "fail",
              f"Excel build failed — {reason}")
        return None
    _step(steps, "to_excel", "ok", f"Excel workbook ready ({len(xl)} bytes)")
    return xl


def _write_grounded_answer(
    resolved_question: str,
    rows: list[dict],
    columns: list[str],
    steps: list[StepEvent],
    *,
    caveat: str | None = None,
) -> tuple[str, bool]:
    """Compose a SQL-path answer with **hard-block** groundedness checking.

    Runs the Writer ↔ groundedness loop up to MAX_GROUNDEDNESS_RETRIES
    times. Every attempt emits a `writer` step event and a `groundedness`
    step event so the full retry history is visible in the trace.

    Returns (text, grounded):
      - `text` is the final Writer output. When grounded=True, this is
        the verified answer. When grounded=False, the budget was
        exhausted — the caller is expected to replace this with the
        explicit failure message (see GROUNDEDNESS_FAIL_TEXT).
      - `grounded` is True iff a Writer attempt passed the check.

    Used by `_finalize_sql_answer` (happy path) and the two Writer-
    invoking branches of `_supervisor_fallback` (ship_partial,
    retry_with_strategy). Consolidating here means every SQL-path
    Writer output passes through the same hard-block contract.
    """
    previous_text: str | None = None
    groundedness_feedback: str | None = None
    text = ""

    # Pre-compute the safe derivations once per turn and pass them into
    # every Writer attempt. Computing this in the orchestrator (not the
    # Writer) means the same input always produces the same list,
    # eliminating one source of non-determinism in the Writer's output.
    safe_values = safe_derivations_summary(rows, columns)

    for attempt in range(1, MAX_GROUNDEDNESS_RETRIES + 1):
        _step(
            steps, "writer", "start",
            f"Composing answer (attempt {attempt}/{MAX_GROUNDEDNESS_RETRIES})...",
        )
        try:
            text = write_answer(
                resolved_question,
                rows=rows,
                columns=columns,
                path="sql",
                caveat=caveat,
                previous_text=previous_text,
                groundedness_feedback=groundedness_feedback,
                safe_values=safe_values,
            )
        except Exception as e:
            _step(steps, "writer", "fail", f"Writer error: {e}")
            return "", False
        _step(steps, "writer", "ok", "Answer drafted")

        try:
            g = groundedness_check(text, rows, columns)
        except Exception as e:
            _step(steps, "groundedness", "fail", f"Groundedness check error: {e}")
            return text, False

        if g.grounded:
            _step(
                steps, "groundedness", "ok",
                f"All {g.total_numbers} number(s) in answer matched against rows",
            )
            return text, True

        # Failed this attempt — surface in trace.
        preview = ", ".join(g.ungrounded[:3])
        more = f" (+{len(g.ungrounded) - 3} more)" if len(g.ungrounded) > 3 else ""

        if attempt < MAX_GROUNDEDNESS_RETRIES:
            _step(
                steps, "groundedness", "retry",
                f"{len(g.ungrounded)} number(s) not in rows: {preview}{more} — retrying Writer",
                ungrounded=g.ungrounded,
            )
            groundedness_feedback = (
                f"Your previous answer cited these values that do NOT appear "
                f"in the result rows or any safe derivation of them: "
                f"{', '.join(g.ungrounded)}. "
                "Safe derivations the groundedness check accepts: "
                "(a) individual cell values from the rows above, "
                "(b) the grand total of any numeric column, "
                "(c) per-group subtotals — the sum of a numeric column for "
                "all rows that share a categorical value (e.g. 'Necklace "
                "shows 41 requests' when 41 is the sum of all Necklace rows "
                "in the result), "
                "(d) per-group row counts (e.g. '4 distinct attributes for "
                "Necklace'), "
                "(e) top-N partial sums for N in {2..5} on a sorted numeric "
                "column (e.g. 'the top 3 attributes account for 36 "
                "requests'), "
                "(f) percentages relative to a column total. "
                "Anything else (averages, differences, ratios, arbitrary "
                "subset sums) will be flagged. Rewrite the answer using "
                "ONLY safe derivations. If you cannot make a useful answer "
                "under this constraint, say so concisely in 1-2 sentences."
            )
            previous_text = text
        else:
            _step(
                steps, "groundedness", "fail",
                f"HARD BLOCK: groundedness retry budget exhausted after "
                f"{MAX_GROUNDEDNESS_RETRIES} attempts. {len(g.ungrounded)} "
                f"number(s) still unverified: {preview}{more}",
                ungrounded=g.ungrounded,
            )
            return text, False

    # Defensive — loop always returns above.
    return text, False


# =============================================================================
# ## Public entry
# =============================================================================

def run(user_message: str, history: list[dict] | None = None) -> AgentResponse:
    """Public entry. Wraps `_run_impl` with persistent trace logging so
    every chat turn lands in the `chat_trace` MySQL table. Logging
    failures are swallowed inside `log_turn` — they never break the
    response."""
    resp = _run_impl(user_message, history)
    try:
        log_turn(user_message=user_message, resp=resp)
    except Exception:
        # Defense in depth: log_turn already catches everything, but
        # belt-and-braces here in case the import itself raises.
        pass
    return resp


def _run_impl(user_message: str, history: list[dict] | None = None) -> AgentResponse:
    history = history or []
    steps: list[StepEvent] = []

    # ---- Planner ----
    _step(steps, "planner", "start", "Categorizing & resolving context...")
    try:
        plan = planner_plan(user_message, history=history)
    except Exception as e:
        _step(steps, "planner", "fail", f"Planner failed: {e}")
        return AgentResponse(
            text="I had trouble understanding your question. Could you rephrase it?",
            steps=steps,
        )
    _step(steps, "planner", "ok", f"path={plan.path}",
          path=plan.path, plan=plan.plan, resolved=plan.resolved_question)

    # ---- Direct / Clarify ----
    if plan.path == "clarify":
        return AgentResponse(text=plan.resolved_question or plan.plan, steps=steps)

    if plan.path == "direct":
        _step(steps, "writer", "start", "Composing direct answer...")
        text = write_answer(plan.resolved_question, path="direct", plan=plan.plan)
        _step(steps, "writer", "ok", "Answer ready")
        return AgentResponse(text=text, steps=steps)

    # ---- SQL path ----
    return _run_sql_path(user_message, plan.resolved_question, steps)


# =============================================================================
# ## SQL path with two nested review loops
# =============================================================================

def _run_sql_path(
    original_user_message: str,
    resolved_question: str,
    steps: list[StepEvent],
) -> AgentResponse:

    outer_feedback: str | None = None     # from Output Reviewer
    outer_previous_sql: str | None = None
    last_good_sql = ""
    last_good_result: dict = {}

    for outer_attempt in range(1, MAX_OUTPUT_REVIEW_RETRIES + 1):

        # ---- Inner loop: Coder ↔ Code Reviewer ↔ sql_executor (max 5 retries) ----
        sql_text, result = _inner_sql_loop(
            resolved_question, steps,
            seed_feedback=outer_feedback, seed_previous_sql=outer_previous_sql,
        )
        if sql_text is None or result is None or not result.get("ok"):
            # Inner loop failed — escalate to Supervisor
            return _supervisor_fallback(original_user_message, steps,
                                        last_sql=outer_previous_sql,
                                        last_result=last_good_result,
                                        resolved=resolved_question)

        last_good_sql = sql_text
        last_good_result = result

        # ---- Output Reviewer (semantic fit + viz decision) ----
        _step(steps, "output_reviewer", "start", "Judging result against the question...")
        try:
            o_review = output_review(
                resolved_question=resolved_question,
                sql=sql_text,
                rows=result["rows"],
                columns=result["columns"],
                original_user_message=original_user_message,
            )
        except Exception as e:
            _step(steps, "output_reviewer", "fail", f"Output Reviewer failed: {e}")
            # Tolerate: assume ok, no viz
            o_review = None

        if o_review is None or o_review.verdict == "ok":
            if o_review:
                _step(steps, "output_reviewer", "ok",
                      f"semantic=ok · viz_applies={o_review.viz_applies}",
                      viz_applies=o_review.viz_applies)
            viz_applies = bool(o_review and o_review.viz_applies)
            return _finalize_sql_answer(
                resolved_question, sql_text, result, viz_applies, steps,
            )

        # verdict == "retry" — bounce back to Coder with semantic feedback
        _step(steps, "output_reviewer", "retry", o_review.feedback)
        outer_feedback = o_review.feedback
        outer_previous_sql = sql_text

    # Output review loop exhausted → Supervisor
    return _supervisor_fallback(original_user_message, steps,
                                last_sql=last_good_sql,
                                last_result=last_good_result,
                                resolved=resolved_question)


# -----------------------------------------------------------------------------
# ## Inner loop: Coder ↔ Code Reviewer ↔ sql_executor
# -----------------------------------------------------------------------------
def _inner_sql_loop(
    resolved_question: str,
    steps: list[StepEvent],
    *,
    seed_feedback: str | None,
    seed_previous_sql: str | None,
) -> tuple[str | None, dict | None]:

    feedback = seed_feedback
    previous_sql = seed_previous_sql
    sql_text = ""

    for attempt in range(1, MAX_CODE_REVIEW_RETRIES + 1):
        _step(steps, "coder", "start",
              f"Writing SQL (attempt {attempt}/{MAX_CODE_REVIEW_RETRIES})...")
        try:
            coder_raw = write_sql(resolved_question, feedback=feedback, previous_sql=previous_sql)
        except Exception as e:
            _step(steps, "coder", "fail", f"Coder error: {e}")
            return None, None

        parsed = sql_parser(coder_raw)
        if not parsed.ok:
            _step(steps, "coder", "retry", f"sql_parser: {parsed.reason}")
            feedback = f"Your output didn't contain valid <sql_query> tags ({parsed.reason}). Wrap the SQL in <sql_query>...</sql_query>."
            previous_sql = coder_raw[:300]
            continue

        sql_text = parsed.sql
        # Emit the Coder's reasoning into the step trace so the user can see
        # what the Coder thought about before writing SQL (visible in the
        # "How I got this answer" expander and persisted in chat_trace).
        _step(steps, "coder", "ok", "SQL produced",
              sql=sql_text, reasoning=parsed.reasoning)

        guard = sql_safety_guard(sql_text)
        if not guard.ok:
            _step(steps, "safety_guard", "retry", guard.reason)
            feedback = f"Safety guard rejected the SQL: {guard.reason}. SELECT-only."
            previous_sql = sql_text
            continue
        _step(steps, "safety_guard", "ok", "SELECT-only — passed")

        try:
            # Pass the reasoning so the Code Reviewer can verify the SQL
            # actually implements what the Coder claimed it would.
            review = code_review(
                resolved_question, sql_text,
                execution_error=None,
                reasoning=parsed.reasoning,
            )
        except Exception as e:
            _step(steps, "code_reviewer", "fail", f"Reviewer error: {e}")
            review = None

        if review and review.verdict == "retry":
            _step(steps, "code_reviewer", "retry", review.feedback)
            feedback = review.feedback
            previous_sql = sql_text
            continue
        if review:
            _step(steps, "code_reviewer", "ok", "SQL technically sound")

        _step(steps, "sql_executor", "start", "Running query against MySQL...")
        result = sql_executor(sql_text)

        if not result["ok"]:
            err = result["error"]
            _step(steps, "sql_executor", "fail", err)
            try:
                err_review = code_review(
                    resolved_question, sql_text,
                    execution_error=err,
                    reasoning=parsed.reasoning,
                )
            except Exception as e:
                err_review = None
                _step(steps, "code_reviewer", "fail", f"Reviewer error on err: {e}")
            feedback = err_review.feedback if (err_review and err_review.verdict == "retry") \
                       else f"Execution failed: {err}. Try a different approach."
            previous_sql = sql_text
            continue

        _step(steps, "sql_executor", "ok", f"Returned {result['row_count']} row(s)")
        return sql_text, result

    return None, None


# -----------------------------------------------------------------------------
# ## Finalize: Writer (always) + Viz pipeline (if applicable)
# -----------------------------------------------------------------------------
def _finalize_sql_answer(
    resolved_question: str,
    sql_text: str,
    result: dict,
    viz_applies: bool,
    steps: list[StepEvent],
) -> AgentResponse:

    # --- Writer ↔ groundedness (hard-block, MAX_GROUNDEDNESS_RETRIES) ---
    # The Writer loop refuses to ship text whose numbers don't reconcile
    # against the rows. On budget exhaustion we ship an explicit failure
    # message rather than the unverified text.
    text, grounded = _write_grounded_answer(
        resolved_question, result["rows"], result["columns"], steps,
    )
    if not grounded:
        text = GROUNDEDNESS_FAIL_TEXT

    chart_png: bytes | None = None
    final_viz_code: str | None = None

    if viz_applies and result["row_count"] > 0:
        chart_png, final_viz_code = _run_viz_pipeline(
            resolved_question, result, steps,
        )

    # Always attach an Excel download when we have rows.
    excel_bytes = _attach_excel(result, steps)

    return AgentResponse(
        text=text, sql=sql_text, chart_png=chart_png,
        viz_code=final_viz_code, excel_bytes=excel_bytes, steps=steps,
    )


# =============================================================================
# ## Viz sub-pipeline
# =============================================================================

def _run_viz_pipeline(
    resolved_question: str,
    result: dict,
    steps: list[StepEvent],
) -> tuple[bytes | None, str | None]:
    """
    Runs Viz Coder ↔ Viz Code Reviewer (max 2) → sandbox exec → Viz Reviewer
    (max 1 revise). Returns (png_bytes, viz_code) or (None, None) on failure.
    """

    rows = result["rows"]
    columns = result["columns"]

    viz_feedback: str | None = None
    viz_previous_code: str | None = None
    viz_code: str = ""
    png_bytes: bytes | None = None

    # --- Outer: Viz Reviewer revise loop ---
    for revise_round in range(MAX_VIZ_REVISE + 1):

        # --- Inner: Viz Coder ↔ Viz Code Reviewer (static review) ---
        for vc_attempt in range(1, MAX_VIZ_CODE_RETRIES + 1):
            _step(steps, "viz_coder", "start",
                  f"Writing matplotlib code (attempt {vc_attempt}/{MAX_VIZ_CODE_RETRIES})...")
            try:
                raw = write_viz_code(
                    resolved_question, rows, columns,
                    feedback=viz_feedback, previous_code=viz_previous_code,
                )
            except Exception as e:
                _step(steps, "viz_coder", "fail", f"Viz Coder failed: {e}")
                return None, None

            parsed = viz_code_parser(raw)
            if not parsed.ok:
                _step(steps, "viz_coder", "retry", parsed.reason)
                viz_feedback = (f"Output missing <viz_code> tags ({parsed.reason}). "
                                "Wrap the code in <viz_code>...</viz_code>.")
                viz_previous_code = raw[:300]
                continue

            viz_code = parsed.sql  # ParseResult reuses 'sql' for any code
            _step(steps, "viz_coder", "ok", "Viz code produced")

            # Static review of the code
            try:
                vcr = viz_code_review(viz_code, resolved_question)
            except Exception as e:
                _step(steps, "viz_code_reviewer", "fail", f"Viz Code Reviewer failed: {e}")
                vcr = None

            if vcr and vcr.verdict == "retry":
                _step(steps, "viz_code_reviewer", "retry", vcr.feedback)
                viz_feedback = vcr.feedback
                viz_previous_code = viz_code
                continue
            if vcr:
                _step(steps, "viz_code_reviewer", "ok", "Viz code passes static review")

            break  # exit inner loop with viz_code in hand
        else:
            # static review loop exhausted
            _step(steps, "viz_code_reviewer", "fail", "Static review retries exhausted")
            return None, None

        # --- Run the code in sandbox ---
        _step(steps, "viz_generator", "start", "Sandbox-executing viz code...")
        gen = viz_generator(viz_code, rows, columns)
        if not gen["ok"]:
            _step(steps, "viz_generator", "fail", gen["error"])
            viz_feedback = f"Execution failed: {gen['error']}. Fix and try again."
            viz_previous_code = viz_code
            continue  # try the revise loop again with sandbox-error feedback
        png_bytes = gen["png"]
        _step(steps, "viz_generator", "ok", f"PNG produced ({len(png_bytes)} bytes)")

        # --- Multimodal Viz Reviewer ---
        _step(steps, "viz_reviewer", "start", "Multimodal review of rendered chart...")
        try:
            vr = viz_review(png_bytes, resolved_question)
        except Exception as e:
            _step(steps, "viz_reviewer", "fail", f"Viz Reviewer failed: {e}")
            vr = None

        if vr is None or vr.verdict == "ok":
            if vr:
                _step(steps, "viz_reviewer", "ok", "Chart approved")
            return png_bytes, viz_code

        if vr.verdict == "drop":
            _step(steps, "viz_reviewer", "fail", f"drop: {vr.feedback}")
            return None, None

        # verdict == "revise"
        _step(steps, "viz_reviewer", "retry", f"revise: {vr.feedback}")
        viz_feedback = vr.feedback
        viz_previous_code = viz_code
        # loop back to outer (one revise round)

    # Revise loop exhausted; ship the last PNG anyway if we have one
    return png_bytes, viz_code if png_bytes else None


# =============================================================================
# ## Supervisor fallback
# =============================================================================

def _supervisor_fallback(
    user_message: str,
    steps: list[StepEvent],
    *,
    last_sql: str | None,
    last_result: dict,
    resolved: str,
) -> AgentResponse:
    _step(steps, "supervisor", "start", "Retry budget exhausted; escalating")
    try:
        decision = supervisor_decide(
            user_message,
            trace=[asdict(s) for s in steps],
        )
    except Exception as e:
        _step(steps, "supervisor", "fail", f"Supervisor error: {e}")
        return AgentResponse(
            text="I couldn't fully answer this question. Try rephrasing or narrowing scope.",
            steps=steps,
        )
    _step(steps, "supervisor", "ok", f"action={decision.action}",
          action=decision.action)

    if decision.action == "abort_gracefully":
        return AgentResponse(text=decision.message, sql=last_sql, steps=steps)
    if decision.action == "ask_user":
        return AgentResponse(text=decision.message, steps=steps)
    if decision.action == "ship_partial" and last_result.get("ok"):
        # Recovery Writer also passes through hard-block grounding.
        text, grounded = _write_grounded_answer(
            resolved, last_result["rows"], last_result["columns"], steps,
            caveat=decision.message,
        )
        if not grounded:
            text = GROUNDEDNESS_FAIL_TEXT
        # Recovery branches still get the Excel download — without this
        # the user sees an answer with no way to inspect the underlying
        # data, which contradicted the Writer's prose in early bug
        # reports.
        return AgentResponse(
            text=text, sql=last_sql,
            excel_bytes=_attach_excel(last_result, steps),
            steps=steps,
        )
    if decision.action == "retry_with_strategy":
        try:
            coder_raw = write_sql(
                resolved,
                feedback=f"SUPERVISOR STRATEGY: {decision.strategy_hint}",
                previous_sql=last_sql,
            )
            parsed = sql_parser(coder_raw)
            if parsed.ok and sql_safety_guard(parsed.sql).ok:
                result = sql_executor(parsed.sql)
                if result["ok"]:
                    text, grounded = _write_grounded_answer(
                        resolved, result["rows"], result["columns"], steps,
                    )
                    if not grounded:
                        text = GROUNDEDNESS_FAIL_TEXT
                    return AgentResponse(
                        text=text, sql=parsed.sql,
                        excel_bytes=_attach_excel(result, steps),
                        steps=steps,
                    )
        except Exception as e:
            _step(steps, "coder", "fail", f"Supervisor retry failed: {e}")

    return AgentResponse(
        text=decision.message or "I couldn't fully answer this question.",
        steps=steps,
    )
