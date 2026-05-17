# Testing Guide — Merchandising Agentic Workflow

This folder holds every test the project ships with, plus the manual
ground-truth eval prompts. Six items total — four executable tests,
one data file, and one manual reference. This document explains each
one: **what it is**, **an example of what it does**, **how to run it**,
**when to run it**, and **how to save the output**.

For the broader eval architecture (the three runtime rings + the
out-of-band evals), see the "Evaluation" section in
`docs/00_system_overview.md`. This file is the *operational* guide
for actually running the tests.

---

## Inventory at a glance

| # | File | Type | What it checks | Latency | Cost / run |
|---|------|------|----------------|---------|------------|
| 1 | `test_planner.py` | Smoke (eyeball) | Planner routes + resolves correctly | ~15s | ~$0.01 |
| 2 | `test_orchestrator.py` | End-to-end smoke | Full pipeline runs cleanly | ~2m | ~$0.10 |
| 3 | `golden_sql_dataset.jsonl` | Data file | (25 entries fed to `test_golden_sql.py`) | — | — |
| 4 | `test_golden_sql.py` | Automated regression | SQL shape + Planner path + row count | ~3m | ~$0.20 |
| 5 | `verify_evals.py` | Integration smoke | Groundedness + trace_logger wiring | ~1s–10s | $0–0.005 |
| 6 | `eval_prompts.md` | Manual ground-truth | Numeric correctness vs Excel pivot | 30–60m | ~$0.10 |

---

## 1. `test_planner.py` — Planner smoke test

### What it does

Runs a fixed set of 9 representative questions through
`agents.planner.plan()` and prints the resulting `path` / `plan` /
`resolved_question` for each. The goal is to eyeball whether the
Planner is routing turns correctly (sql / direct / clarify) and
producing sensible context-resolved questions for the downstream
agents. Does NOT touch MySQL — only hits the LLM.

### Example output

```
Q: What's the top reason for non-purchase in X1?
   path              = sql
   plan              = Group inferred_topic counts where store_code='X1'.
   resolved_question = What is the top reason for non-purchase at store X1?

Q: Hi
   path              = direct
   plan              = Greet and describe capabilities.
   resolved_question = Hi

Q: Tell me about my stores.
   path              = clarify
   plan              = Need a dimension.
   resolved_question = Would you like to see top issues by store, most-asked products, or total feedback volume?
```

What you're looking for:

- Concrete factual questions → `sql`
- Greetings, definitions → `direct`
- Vague open-ended questions → `clarify`
- `resolved_question` reads as a complete English sentence with no
  pronouns or unresolved references

### How to run

```bash
# Built-in 9-question sample suite
python3 tests/test_planner.py

# Test a single custom question
python3 tests/test_planner.py "what is the top issue in X1"
```

### When to run

After editing `agents/prompts/planner.txt`. The fastest sanity check
that your prompt changes haven't broken routing or history-resolution.

### Save output

```bash
mkdir -p tests/results
python3 tests/test_planner.py 2>&1 | tee tests/results/planner_$(date +%Y%m%d_%H%M%S).txt
```

### Requirements

- `ANTHROPIC_API_KEY` in `.env`
- MySQL NOT needed (Planner doesn't query the database)

---

## 2. `test_orchestrator.py` — End-to-end pipeline smoke test

### What it does

Pushes 5 default questions through the **complete** `orchestrator.run`
pipeline against real MySQL — every agent fires, every retry budget
is in play, real SQL executes, real groundedness check runs, real
trace logging happens. For each question, prints the complete step
trace, the final SQL, and the Writer's answer text.

Use this after a structural change to verify the whole chain still
works end-to-end. It's slower and more expensive than the other
tests but gives the most realistic signal.

### Example output (truncated)

```
================================================================================
Q: What is the top reason for non-purchase across all stores?
================================================================================

--- step trace ---
  [    ok] planner            path=sql
  [    ok] coder              SQL produced
  [    ok] safety_guard       SELECT-only — passed
  [    ok] code_reviewer      SQL technically sound
  [    ok] sql_executor       Returned 10 row(s)
  [    ok] output_reviewer    semantic=ok · viz_applies=False
  [ start] writer             Composing answer (attempt 1/2)...
  [    ok] writer             Answer drafted
  [    ok] groundedness       All 4 number(s) in answer matched against rows
  [    ok] to_excel           Excel workbook ready (5142 bytes)

--- SQL ---
SELECT inferred_topic, COUNT(*) AS n
FROM non_purchasers_feedback
GROUP BY inferred_topic
ORDER BY n DESC
LIMIT 10

--- answer ---
Across all six stores, "Size Unavailable" is the top reason for non-purchase
with 312 feedbacks, followed by "Design Unavailable" (245) and ...
```

What you're looking for:

- Every step in the trace is `ok` (no `retry` or `fail`)
- SQL is technically correct + uses the right columns
- Answer prose grounds every number against the rows
- `to_excel` step appears (means the download button will render)

### How to run

```bash
# Default 5-question suite
python3 tests/test_orchestrator.py

# Single custom question
python3 tests/test_orchestrator.py "Compare size issues between X1 and X4"
```

### When to run

- After refactoring any orchestrator helper (`_inner_sql_loop`,
  `_write_grounded_answer`, `_attach_excel`, etc.)
- After model upgrades (Coder Haiku → Sonnet, etc.)
- After adding a new agent or tool to the pipeline
- As a final check before declaring a big change "done"

### Save output

```bash
mkdir -p tests/results
python3 tests/test_orchestrator.py 2>&1 | tee tests/results/orchestrator_$(date +%Y%m%d_%H%M%S).log
```

### Requirements

- `ANTHROPIC_API_KEY` in `.env`
- `MYSQL_PASSWORD` in `.env`
- `non_purchasers_feedback` table populated and enriched (run the
  scripts in `data_prep/` first)

---

## 3. `golden_sql_dataset.jsonl` — golden SQL dataset

### What it is

The data file that `test_golden_sql.py` consumes. 25 entries, one
JSON object per line. Each entry encodes a question, the expected
Planner path, structural assertions on the generated SQL, and a row-
count range for the result.

### Example entry

```json
{
  "id": "001",
  "question": "What is the top reason for non-purchase at X1?",
  "expected_path": "sql",
  "sql_must_contain": ["X1", "inferred_topic"],
  "sql_must_not_contain": ["ground_truth_topic"],
  "result_min_rows": 1,
  "result_max_rows": 12,
  "notes": "Single-store top-reason query"
}
```

Field reference:

- `expected_path`: which Planner path should fire (`sql` / `direct` /
  `clarify`)
- `sql_must_contain`: every string must appear in the generated SQL
  (case-insensitive)
- `sql_must_not_contain`: no string here may appear in the SQL
- `result_min_rows` / `result_max_rows`: the executed result's row
  count must fall in this range

### How to add a new entry

Just append a JSON line and save. No registration step. The runner
picks it up next time it runs.

### When to add entries

Whenever you find a real bug, write a golden-SQL entry that would
have caught it. The set grows over time and protects against
regressions.

---

## 4. `test_golden_sql.py` — automated SQL regression runner

### What it does

Loads every entry in `golden_sql_dataset.jsonl`, pushes the question
through `orchestrator.run`, and asserts three things:

1. **Planner path** matches `expected_path`
2. **Generated SQL** contains all `sql_must_contain` tokens and none
   of the `sql_must_not_contain` tokens
3. **Executed row count** falls within `[result_min_rows,
   result_max_rows]`

Pass/fail per entry. Exit code 0 on all-pass, non-zero on any
failure — so it plugs straight into CI / pre-commit hooks.

**Important nuance:** this is a **shape check**, not an **answer
correctness check**. It verifies the pipeline produced *structurally
reasonable* output; it does NOT verify the numeric values are
correct. For that, see `eval_prompts.md`.

### Example output

```
Running 25 golden entries...

  [PASS] 001  What is the top reason for non-purchase at X1?
  [PASS] 002  What is the top reason for non-purchase across all stores?
  [FAIL] 003  Which store has the highest count of Design Unavailable feedbacks?
         sql_must_contain: missing token 'Design Unavailable'
  [PASS] 004  How many size-unavailable feedbacks does X4 have?
  ...

=== 24 pass / 1 fail / 25 total ===
```

A failure tells you exactly which assertion broke. Re-run with
`--verbose` to also see the failing SQL.

### How to run

```bash
# Full suite
python3 tests/test_golden_sql.py

# Run specific entries only (useful for debugging one regression)
python3 tests/test_golden_sql.py --only 001,019,020

# Verbose: shows passing rows too, plus actual SQL on failures
python3 tests/test_golden_sql.py --verbose

# CI integration — exit code 0 if all pass
python3 tests/test_golden_sql.py && echo "all pass" || echo "regressions!"
```

### When to run

**This is the regression gate.** Run it after ANY change to:

- `agents/prompts/coder.txt`
- `agents/prompts/code_reviewer.txt`
- `agents/prompts/planner.txt`
- `agents/coder.py`, `agents/code_reviewer.py`, `agents/planner.py`
- `tools/sql_tools.py` (especially `SCHEMA_TEXT`)
- Model assignments in `agents/llm.py`

Ideally wire it into a pre-commit hook or CI workflow.

### Save output

```bash
mkdir -p tests/results
python3 tests/test_golden_sql.py --verbose \
  > tests/results/golden_$(date +%Y%m%d_%H%M%S).log 2>&1
```

### Requirements

- `ANTHROPIC_API_KEY` + `MYSQL_PASSWORD` in `.env`
- `non_purchasers_feedback` table populated and enriched

---

## 5. `verify_evals.py` — eval & persistence integration smoke test

### What it does

Confirms the in-band eval / persistence integrations are alive. Runs
up to four checks (the last one is skipped if API/DB creds are
missing so the script stays useful even on a laptop with no API key):

1. **`tools.groundedness`** — imports cleanly and behaves correctly
   on 7 toy-data sub-cases:
   - Toy grounded text passes
   - Hallucinated number is caught
   - Empty text + empty rows is vacuously grounded
   - Per-group subtotal is accepted (e.g. "Necklace 41")
   - Top-N partial sum is accepted (e.g. "top 3 = 36")
   - Per-group row count is accepted
   - Arbitrary subset sum is STILL rejected (so the check stays
     meaningful)

2. **`tools.trace_logger.ensure_table()`** — creates the `chat_trace`
   table if missing

3. **`tools.trace_logger.log_turn`** — inserts a synthetic row and
   the script reads it back via SELECT

4. **(only with creds)** — sends a real chat turn through
   `orchestrator.run`, confirms a `groundedness` step event lands in
   the trace AND a matching row appears in `chat_trace`

### Example output

```
========================================================================
  [1] tools.groundedness — deterministic numeric check
========================================================================
  [PASS]  import tools.groundedness
  [PASS]  grounded answer (47, 24%)
  [PASS]  hallucinated 1500 caught
  [PASS]  empty rows + no numbers
  [PASS]  group-sum (Necklace=41) accepted
  [PASS]  top-3 partial sum (36) accepted
  [PASS]  group row count (Necklace=4) accepted
  [PASS]  non-group arbitrary subset sum (23) STILL rejected

========================================================================
  [2] tools.trace_logger — chat_trace table creation
========================================================================
  [PASS]  MYSQL_PASSWORD set
  [PASS]  import tools.trace_logger
  [PASS]  ensure_table()

========================================================================
  Summary
========================================================================
  [PASS]  groundedness
  [PASS]  ensure_table
  [PASS]  log_turn
  [PASS]  live_turn

  4 / 4 checks passed
```

### How to run

```bash
# Full run (needs MYSQL_PASSWORD; live turn needs ANTHROPIC_API_KEY too)
python3 tests/verify_evals.py

# Skip the live LLM turn (useful on a laptop with no API key)
python3 tests/verify_evals.py --skip-live
```

### When to run

- After editing `tools/groundedness.py` (especially
  `_candidate_values` or `safe_derivations_summary`)
- After editing `tools/trace_logger.py`
- After editing the orchestrator's groundedness loop or
  `_attach_excel`
- As a pre-flight sanity check before pushing a release

### Save output

```bash
mkdir -p tests/results
python3 tests/verify_evals.py 2>&1 | tee tests/results/verify_$(date +%Y%m%d_%H%M%S).log
```

### Requirements

- For `--skip-live`: no API key or DB needed (~1 second)
- For full run: `MYSQL_PASSWORD` required;
  `ANTHROPIC_API_KEY` enables check 4

---

## 6. `eval_prompts.md` — manual ground-truth eval (15 prompts)

### What it is

15 manually-curated prompts at three difficulty tiers (5 easy, 5
complex, 5 very complex) for **answer-correctness** evaluation. The
key difference from `test_golden_sql.py`: this checks the *actual
numeric values* in the chat's answer against ground truth you compute
in Excel pivot tables. Catches semantic errors, hallucinated numbers,
and broken multi-turn discovery — things shape-checking can't see.

See `tests/eval_prompts.md` itself for the full prompts and ground-
truth methods.

### Example workflow for one prompt

```
1. Open Excel, build pivot table from data_sample/non_purchasers_feedback.csv:
     Rows: store_code  ·  Values: Count of customer_name  ·  Sort: descending
   → Note the top store and its count (e.g., X3 = 201)

2. Open the chat, type the exact prompt from eval_prompts.md:
     "Which store has the highest number of non-purchase feedbacks?"

3. Compare:
     Agent answer says: "X3 with 201 feedbacks"
     Excel ground truth: X3 = 201
     → Numeric ✓, Complete ✓, Attribution ✓

4. Log in a spreadsheet:
     prompt_id | tier | numeric | complete | attribution | notes
     ----------|------|---------|----------|-------------|------
     2         | T1   | ✓       | ✓        | ✓           | passed
```

### How to "run"

There's no script — open `tests/eval_prompts.md` in any markdown
viewer, work through each prompt manually, with Excel + the running
Streamlit app side by side.

### When to run

- Weekly, as ongoing quality monitoring
- Before/after model upgrades or major prompt changes
- When `chat_trace` queries show a spike in supervisor invocations or
  groundedness warnings — pick a few prompts from the relevant tier
  to spot-check

### Save output (scoring template)

```bash
mkdir -p tests/results
cat > tests/results/eval_scores_$(date +%Y%m%d).csv << 'EOF'
prompt_id,tier,numeric,complete,attribution,discovery,notes
1,T1,,,,N/A,
2,T1,,,,N/A,
3,T1,,,,N/A,
4,T1,,,,N/A,
5,T1,,,,N/A,
6,T2,,,,N/A,
7,T2,,,,N/A,
8,T2,,,,N/A,
9,T2,,,,N/A,
10,T2,,,,N/A,
11,T3,,,,,
12,T3,,,,,
13,T3,,,,,
14,T3,,,,,
15,T3,,,,,
EOF
```

Fill in `✓` / `✗` / `partial` as you work through. The "Discovery"
column applies only to Tier 3 (tests the hierarchical-analysis rule).

### Requirements

- The chat (Streamlit app) running and reachable
- Excel or any pivot-table tool
- ~30-60 minutes for a full pass

---

## Recommended cadence

| Trigger | Test to run | Why |
|---|---|---|
| You edited `agents/prompts/planner.txt` | `test_planner.py` then `test_golden_sql.py` | Eyeball routing, then catch regressions |
| You edited `agents/prompts/coder.txt` or `code_reviewer.txt` | `test_golden_sql.py --verbose` | Catch SQL-shape regressions |
| You edited `tools/groundedness.py` | `verify_evals.py --skip-live` then full `verify_evals.py` | Confirm the candidate set still behaves |
| You edited `tools/trace_logger.py` | `verify_evals.py` (full) | Confirm logging still works |
| You changed a model (Coder, Code Reviewer, etc.) | `test_orchestrator.py` then `test_golden_sql.py` | Full pipeline sanity, then shape check |
| Big refactor / new agent / new tool | `test_orchestrator.py`, `verify_evals.py`, `test_golden_sql.py` (all three) | End-to-end safety net |
| Before pushing a release | All 4 executable tests + spot-check 3-5 entries from `eval_prompts.md` | Last line of defense |
| Weekly quality monitoring | Walk through `eval_prompts.md` (all 15 prompts) | Catches drift that shape-checks miss |

## One-liner to run them all

```bash
mkdir -p tests/results
T=$(date +%Y%m%d_%H%M%S)
python3 tests/verify_evals.py        2>&1 | tee tests/results/${T}_verify.log
python3 tests/test_planner.py        2>&1 | tee tests/results/${T}_planner.log
python3 tests/test_golden_sql.py -v  2>&1 | tee tests/results/${T}_golden.log
python3 tests/test_orchestrator.py   2>&1 | tee tests/results/${T}_orch.log
```

Produces four timestamped logs in `tests/results/` for the run.
Total wall time ~6 minutes; total cost ~$0.40. Add
`tests/results/` to `.gitignore` if you don't want test logs
checked in.

For `eval_prompts.md`, copy the prompts one-by-one into the chat
and fill in your CSV in `tests/results/eval_scores_YYYYMMDD.csv`
as you go.

## Reading the output

For every test, the same status conventions apply:

- `ok` / `PASS` — the step or assertion succeeded
- `retry` — the agent had to redo a step (acceptable in moderation;
  alarming if frequent)
- `fail` / `FAIL` — the step or assertion broke
- `warn` (groundedness only) — a numeric value couldn't be reconciled
  against rows, hard-block fired

If you see frequent `retry`s on a fresh chat (no chat history), suspect
prompt drift. If you see `supervisor` step events, the SQL pipeline
exhausted its retry budgets — drill into that turn's trace.

---

## What's NOT in this folder

A few related things live elsewhere and are worth knowing about:

- **`data_prep/03_eval_topic_classifier.py`** — offline accuracy
  eval for the Topic Classifier. Compares `inferred_topic` against
  `ground_truth_topic` and prints overall accuracy, per-topic F1,
  confusion matrix. Run after every enrichment.

  ```bash
  python3 data_prep/03_eval_topic_classifier.py
  python3 data_prep/03_eval_topic_classifier.py --csv eval_report.csv
  ```

- **`chat_trace` MySQL table** — every chat turn is automatically
  logged. Query it for failure patterns:

  ```sql
  SELECT COUNT(*) FROM chat_trace WHERE supervisor_invoked = TRUE;
  SELECT path, AVG(retry_count), AVG(fail_count), COUNT(*)
    FROM chat_trace GROUP BY path;
  ```

  See `docs/00_system_overview.md` Eval section for more example
  queries.

That's everything. The four runnable tests + one manual reference + one
auto-logging table + one offline classifier eval give you the full
trust-but-verify story for the system.
