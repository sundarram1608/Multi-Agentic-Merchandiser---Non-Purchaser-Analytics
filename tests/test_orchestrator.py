"""
test_orchestrator.py
--------------------
End-to-end smoke test for the full chat workflow. Runs the Orchestrator
against real MySQL and prints the step trace (every Writer attempt, every
groundedness verdict, every retry, every tool call) plus the final SQL
and answer text.

Requires `ANTHROPIC_API_KEY` + `MYSQL_PASSWORD` in env / `.env`. For a
non-LLM verification of the eval / persistence integrations, use
`tests/verify_evals.py` instead. For pattern-based Q→SQL regression
testing, use `tests/test_golden_sql.py`.

Usage
-----
    python3 tests/test_orchestrator.py
    python3 tests/test_orchestrator.py "what is the top reason in X1?"
"""

import sys
from pathlib import Path

# Make the project root importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.orchestrator import run  # noqa: E402


DEFAULT_QUERIES = [
    "What is the top reason for non-purchase across all stores?",
    "Which designs are customers asking for in bangles at X1?",
    "Compare size issues between X1 and X4.",
    "Hi",                                # direct path
    "What's interesting?",               # clarify path
]


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_QUERIES

    for q in queries:
        print("\n" + "=" * 80)
        print(f"Q: {q}")
        print("=" * 80)
        try:
            resp = run(q, history=[])
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        print("\n--- step trace ---")
        for s in resp.steps:
            print(f"  [{s.status:>6}] {s.agent:<18} {s.summary}")

        if resp.sql:
            print(f"\n--- SQL ---\n{resp.sql}")

        print("\n--- answer ---")
        print(resp.text)


if __name__ == "__main__":
    main()
