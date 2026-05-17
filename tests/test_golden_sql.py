"""
test_golden_sql.py
------------------
Run a golden Q→SQL dataset through the full orchestrator and assert:
  - The Planner picks the expected path (`sql` / `direct` / `clarify`).
  - For sql-path entries: the generated SQL contains all `sql_must_contain`
    tokens and none of the `sql_must_not_contain` tokens (case-insensitive).
  - For sql-path entries: the executed result's row count is within the
    [`result_min_rows`, `result_max_rows`] bounds (when specified).

Use this as a regression test for prompt changes — exit code is 0 if
all entries pass, non-zero otherwise, so it can plug into CI.

Usage
-----
    python3 tests/test_golden_sql.py
    python3 tests/test_golden_sql.py --only 001,005,019
    python3 tests/test_golden_sql.py --verbose
    python3 tests/test_golden_sql.py --dataset tests/golden_sql_dataset.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Make the project root importable when this file is run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.orchestrator import run as orchestrator_run  # noqa: E402


DEFAULT_DATASET = Path(__file__).resolve().parent / "golden_sql_dataset.jsonl"


def _planner_path(resp) -> str:
    for s in resp.steps:
        if s.agent == "planner" and s.status == "ok":
            d = s.detail or {}
            return str(d.get("path", ""))
    return ""


def _row_count(resp) -> int | None:
    """Parse the sql_executor 'Returned N row(s)' summary."""
    for s in resp.steps:
        if s.agent == "sql_executor" and s.status == "ok":
            m = re.search(r"(\d+)\s+row", s.summary)
            if m:
                return int(m.group(1))
    return None


def check_entry(entry: dict, resp) -> list[str]:
    """Return a list of failure reasons. Empty list = PASS."""
    fails: list[str] = []

    expected_path = entry.get("expected_path", "")
    actual_path = _planner_path(resp)
    if expected_path and actual_path != expected_path:
        fails.append(f"path: expected={expected_path!r}, got={actual_path!r}")

    if expected_path != "sql":
        # For direct / clarify we only check the path; nothing else applies.
        return fails

    sql = (resp.sql or "")
    sql_lower = sql.lower()

    for token in entry.get("sql_must_contain", []):
        if token.lower() not in sql_lower:
            fails.append(f"sql_must_contain: missing token {token!r}")

    for token in entry.get("sql_must_not_contain", []):
        if token.lower() in sql_lower:
            fails.append(f"sql_must_not_contain: unexpected token {token!r}")

    n = _row_count(resp)
    if n is not None:
        min_r = entry.get("result_min_rows")
        max_r = entry.get("result_max_rows")
        if min_r is not None and n < min_r:
            fails.append(f"result_min_rows: expected>={min_r}, got {n}")
        if max_r is not None and n > max_r:
            fails.append(f"result_max_rows: expected<={max_r}, got {n}")

    return fails


def main():
    parser = argparse.ArgumentParser(
        description="Golden Q→SQL regression runner."
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET),
                        help="Path to the JSONL dataset")
    parser.add_argument("--only",
                        help="Comma-separated entry IDs to run (e.g. 001,005)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print PASS rows and actual SQL on FAIL")
    args = parser.parse_args()

    entries: list[dict] = []
    with open(args.dataset, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            entries.append(json.loads(line))

    if args.only:
        only = {s.strip() for s in args.only.split(",")}
        entries = [e for e in entries if e.get("id") in only]

    print(f"\nRunning {len(entries)} golden entries...\n")
    n_pass = 0
    n_fail = 0

    for entry in entries:
        eid = entry.get("id", "?")
        question = entry.get("question", "")
        try:
            resp = orchestrator_run(question, history=[])
        except Exception as e:
            n_fail += 1
            print(f"  [FAIL] {eid}  question={question!r}")
            print(f"         orchestrator error: {e}")
            continue

        fails = check_entry(entry, resp)
        if not fails:
            n_pass += 1
            if args.verbose:
                print(f"  [PASS] {eid}  {question[:70]}")
        else:
            n_fail += 1
            print(f"  [FAIL] {eid}  {question[:70]}")
            for f in fails:
                print(f"         {f}")
            if args.verbose and resp.sql:
                print(f"         actual SQL: {resp.sql[:160]}")

    print(f"\n=== {n_pass} pass / {n_fail} fail / {len(entries)} total ===")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
