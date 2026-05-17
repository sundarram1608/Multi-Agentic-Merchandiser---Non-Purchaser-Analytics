"""
test_planner.py
---------------
Smoke test for the Planner agent. Runs a handful of representative
queries through `plan()` and prints the path + plan + resolved_question.

Usage
-----
    # Run the built-in sample suite
    python3 tests/test_planner.py

    # Test one custom question
    python3 tests/test_planner.py "what is the top issue in X1"

What you should see
-------------------
- "sql" path for any question that needs to read the table.
- "direct" path for greetings and definition questions.
- "clarify" path for vague questions where one good follow-up would help.
- "resolved_question" should be a self-contained version of the user's
  query with any chat-history references resolved (history is empty in
  this smoke test, so resolved_question typically just normalizes the
  raw input).

For chart-intent detection, the viz decision is made downstream by the
Output Reviewer's `viz_applies` flag — the Planner itself no longer
emits a viz_hint field.
"""

import sys
from pathlib import Path

# Make the project root importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.planner import plan  # noqa: E402


# A representative spread of the three paths. Add your own to stress-test.
DEFAULT_QUERIES = [
    # sql path
    "What's the top reason for non-purchase in X1?",
    "Compare size issues across all stores.",
    "Which designs are customers asking for in bangles at X1?",
    "Show me the breakdown of reasons by product.",
    # direct path
    "Hi",
    "What can you do?",
    "What does 'Customization Not Offered' mean?",
    # clarify path
    "Tell me about my stores.",
    "What's interesting?",
]


def main() -> None:
    queries = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_QUERIES

    for q in queries:
        print(f"\nQ: {q}")
        try:
            r = plan(q)
            print(f"   path              = {r.path}")
            print(f"   plan              = {r.plan}")
            print(f"   resolved_question = {r.resolved_question}")
        except Exception as e:
            print(f"   ERROR: {e}")


if __name__ == "__main__":
    main()
