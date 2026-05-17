"""
tests/verify_evals.py
---------------------
Smoke-test that the eval / persistence integrations are alive.

Runs up to four checks (the last one is skipped if API/DB creds are
missing so the script remains useful on a laptop with no API key):

  [1] tools.groundedness imports + behaves on toy inputs
  [2] tools.trace_logger imports + can CREATE TABLE IF NOT EXISTS
  [3] tools.trace_logger.log_turn can insert + read back a synthetic row
  [4] (only with ANTHROPIC_API_KEY + MYSQL_PASSWORD) run a real chat
      turn through orchestrator.run, then confirm a 'groundedness' step
      event was emitted AND a new row appeared in chat_trace

Exit code 0 if everything that *can* run passes; non-zero on any failure.

Usage
-----
    python3 tests/verify_evals.py
    python3 tests/verify_evals.py --skip-live   # never call the LLM
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Small synthetic AgentResponse-shaped object so we can exercise log_turn
# without running the full pipeline.
# ---------------------------------------------------------------------------
@dataclass
class _FakeStep:
    agent: str
    status: str
    summary: str
    detail: dict = field(default_factory=dict)


@dataclass
class _FakeResp:
    text: str = "Synthetic verification answer — please ignore."
    sql: str | None = "SELECT 1 AS verification_marker"
    chart_png: bytes | None = None
    viz_code: str | None = None
    excel_bytes: bytes | None = None
    steps: list = field(default_factory=list)


def _print_header(title: str):
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def _print_check(label: str, ok: bool, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}]  {label}"
    if extra:
        line += f"  —  {extra}"
    print(line)


# ---------------------------------------------------------------------------
# [1] groundedness
# ---------------------------------------------------------------------------
def check_groundedness() -> bool:
    _print_header("[1] tools.groundedness — deterministic numeric check")
    try:
        from tools.groundedness import groundedness_check
    except Exception as e:
        _print_check("import tools.groundedness", False, str(e))
        return False
    _print_check("import tools.groundedness", True)

    rows = [{"store": "X1", "n": 47}, {"store": "X2", "n": 150}]
    cols = ["store", "n"]

    # 47 appears; 24% ≈ 47/197 = 23.86%; both should match.
    r1 = groundedness_check("X1 has 47 feedbacks, about 24% of total.", rows, cols)
    ok1 = r1.grounded and r1.total_numbers >= 2
    _print_check(
        "grounded answer (47, 24%)", ok1,
        f"grounded={r1.grounded}, total={r1.total_numbers}, ungrounded={r1.ungrounded}",
    )

    # 1500 is fabricated — should be flagged.
    r2 = groundedness_check("X1 has 1500 feedbacks.", rows, cols)
    ok2 = (not r2.grounded) and ("1500" in r2.ungrounded)
    _print_check(
        "hallucinated 1500 caught", ok2,
        f"grounded={r2.grounded}, ungrounded={r2.ungrounded}",
    )

    # Empty rows + no numbers in text — should be vacuously grounded.
    r3 = groundedness_check("I couldn't answer your question.", [], [])
    ok3 = r3.grounded and r3.total_numbers == 0
    _print_check(
        "empty rows + no numbers", ok3,
        f"grounded={r3.grounded}, total={r3.total_numbers}",
    )

    # Group-sum acceptance — Necklace = 20+9+7+5 = 41 should be grounded
    # because the check accepts per-group subtotals for categorical columns.
    necklace_rows = [
        {"product": "Necklace",    "attr": "under 25k",    "n": 20},
        {"product": "Necklace",    "attr": "under 50k",    "n": 9},
        {"product": "Necklace",    "attr": "under 10k",    "n": 7},
        {"product": "Necklace",    "attr": "below 1 lakh", "n": 5},
        {"product": "Bangles",     "attr": "antique",      "n": 7},
        {"product": "Finger Rings","attr": "solitaire",    "n": 3},
        {"product": "Finger Rings","attr": "under 8g",     "n": 3},
    ]
    necklace_cols = ["product", "attr", "n"]
    r4 = groundedness_check(
        "Necklace shows the strongest demand with 41 requests across price bands.",
        necklace_rows, necklace_cols,
    )
    ok4 = r4.grounded
    _print_check(
        "group-sum (Necklace=41) accepted", ok4,
        f"grounded={r4.grounded}, ungrounded={r4.ungrounded}",
    )

    # Top-N partial sum acceptance — top 3 of [20,9,7,5,7,3,3] sorted desc
    # = 20+9+7 = 36 should be grounded.
    r5 = groundedness_check(
        "The top 3 attribute values account for 36 of the requests.",
        necklace_rows, necklace_cols,
    )
    ok5 = r5.grounded
    _print_check(
        "top-3 partial sum (36) accepted", ok5,
        f"grounded={r5.grounded}, ungrounded={r5.ungrounded}",
    )

    # Per-group row count — Necklace has 4 distinct attribute rows.
    r6 = groundedness_check(
        "Necklace appears across 4 distinct attribute values.",
        necklace_rows, necklace_cols,
    )
    ok6 = r6.grounded
    _print_check(
        "group row count (Necklace=4) accepted", ok6,
        f"grounded={r6.grounded}, ungrounded={r6.ungrounded}",
    )

    # Arbitrary-subset sum that ISN'T a group / top-N — should still be
    # rejected so the check stays meaningful. 20 + 3 = 23 spans two
    # different products and isn't a top-N prefix; this should fail.
    r7 = groundedness_check(
        "A fabricated mixed total of 23 across two products.",
        necklace_rows, necklace_cols,
    )
    ok7 = (not r7.grounded) and ("23" in r7.ungrounded)
    _print_check(
        "non-group arbitrary subset sum (23) STILL rejected", ok7,
        f"grounded={r7.grounded}, ungrounded={r7.ungrounded}",
    )

    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7


# ---------------------------------------------------------------------------
# [2] trace_logger.ensure_table
# ---------------------------------------------------------------------------
def check_ensure_table() -> bool:
    _print_header("[2] tools.trace_logger — chat_trace table creation")
    if not os.environ.get("MYSQL_PASSWORD"):
        _print_check("MYSQL_PASSWORD set", False, "skipped, set MYSQL_PASSWORD to enable")
        return False
    try:
        from tools.trace_logger import ensure_table
    except Exception as e:
        _print_check("import tools.trace_logger", False, str(e))
        return False
    _print_check("import tools.trace_logger", True)

    ok = ensure_table()
    _print_check("ensure_table()", ok, "CREATE TABLE IF NOT EXISTS chat_trace")
    return ok


# ---------------------------------------------------------------------------
# [3] trace_logger.log_turn — synthetic insert + read-back
# ---------------------------------------------------------------------------
def check_log_turn() -> bool:
    _print_header("[3] tools.trace_logger — log_turn insert / read-back")
    if not os.environ.get("MYSQL_PASSWORD"):
        _print_check("MYSQL_PASSWORD set", False, "skipped")
        return False
    try:
        from tools.trace_logger import log_turn, _mysql_config  # type: ignore
        import mysql.connector
    except Exception as e:
        _print_check("import deps", False, str(e))
        return False

    marker = f"__verify_evals_synthetic_{int(time.time())}__"
    fake_steps = [
        _FakeStep("planner",      "ok",   "path=sql",         {"path": "sql", "resolved": marker}),
        _FakeStep("coder",        "ok",   "SQL produced"),
        _FakeStep("groundedness", "ok",   "All 0 numbers matched"),
    ]
    fake_resp = _FakeResp(steps=fake_steps)

    log_turn(user_message=marker, resp=fake_resp)
    _print_check("log_turn() did not raise", True)

    # Read it back
    try:
        conn = mysql.connector.connect(**_mysql_config())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT trace_id, user_message, path, supervisor_invoked, "
            "groundedness_warned, step_count, answer_text "
            "FROM chat_trace WHERE user_message = %s "
            "ORDER BY trace_id DESC LIMIT 1",
            (marker,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        _print_check("read-back query", False, str(e))
        return False

    if not row:
        _print_check("synthetic row found", False,
                     "INSERT silently failed — check stderr for [trace_logger] messages")
        return False
    _print_check("synthetic row found", True,
                 f"trace_id={row['trace_id']}, path={row['path']!r}, "
                 f"step_count={row['step_count']}")
    return True


# ---------------------------------------------------------------------------
# [4] End-to-end: real chat turn through the orchestrator
# ---------------------------------------------------------------------------
def check_live_turn() -> bool:
    _print_header("[4] orchestrator.run — live SQL-path turn (LLM + DB)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _print_check("ANTHROPIC_API_KEY set", False, "skipped")
        return False
    if not os.environ.get("MYSQL_PASSWORD"):
        _print_check("MYSQL_PASSWORD set", False, "skipped")
        return False

    try:
        from agents.orchestrator import run as orchestrator_run
        from tools.trace_logger import _mysql_config  # type: ignore
        import mysql.connector
    except Exception as e:
        _print_check("import orchestrator", False, str(e))
        return False

    marker = f"verify_evals smoke test {int(time.time())} — how many total feedbacks?"
    print(f"  Sending: {marker}")
    try:
        resp = orchestrator_run(marker, history=[])
    except Exception as e:
        _print_check("orchestrator.run", False, str(e))
        return False
    _print_check("orchestrator.run returned", True, f"answer={(resp.text or '')[:60]!r}")

    # Step trace should include a 'groundedness' step on the SQL path.
    grounded_step = next(
        (s for s in resp.steps if getattr(s, "agent", "") == "groundedness"),
        None,
    )
    if grounded_step is None:
        # If the planner routed to direct/clarify there'd be no groundedness
        # step, but we explicitly asked a numeric question so this is a fail.
        _print_check("groundedness step emitted", False,
                     "no 'groundedness' step in trace — check _finalize_sql_answer")
        return False
    _print_check(
        "groundedness step emitted", True,
        f"status={grounded_step.status}, summary={grounded_step.summary[:80]!r}",
    )

    # Confirm the trace landed in chat_trace.
    time.sleep(0.5)  # let the INSERT settle
    try:
        conn = mysql.connector.connect(**_mysql_config())
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT trace_id, path, step_count, groundedness_warned, "
            "supervisor_invoked FROM chat_trace "
            "WHERE user_message = %s ORDER BY trace_id DESC LIMIT 1",
            (marker,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        _print_check("chat_trace read-back", False, str(e))
        return False

    if not row:
        _print_check("trace row persisted", False, "no row found — check stderr")
        return False
    _print_check(
        "trace row persisted", True,
        f"trace_id={row['trace_id']}, path={row['path']!r}, "
        f"steps={row['step_count']}, grounded_warned={row['groundedness_warned']}, "
        f"supervisor={row['supervisor_invoked']}",
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-live", action="store_true",
                        help="Skip the live chat turn (don't call the LLM)")
    args = parser.parse_args()

    results = {
        "groundedness":     check_groundedness(),
        "ensure_table":     check_ensure_table(),
        "log_turn":         check_log_turn(),
    }
    if not args.skip_live:
        results["live_turn"] = check_live_turn()

    _print_header("Summary")
    for name, ok in results.items():
        _print_check(name, ok)
    n_pass = sum(1 for v in results.values() if v)
    n_total = len(results)
    print(f"\n  {n_pass} / {n_total} checks passed")
    sys.exit(0 if n_pass == n_total else 1)


if __name__ == "__main__":
    main()
