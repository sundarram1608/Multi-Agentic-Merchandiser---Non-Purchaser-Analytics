# Merchandising AI — System Overview

---

## What we're building

A merchandising assistant for the team that owns six retail jewelry stores
(X1..X6). Two interaction surfaces share one data foundation:

1. **Chat** — free-form Q&A driven by a 9-agent pipeline. Most user
   traffic goes here.
2. **Recommendations Report** — a one-click holistic view (top 10 focus
   areas, per-store breakdown, distribution charts). Deterministic
   computation directly against MySQL — no agents, no LLM.

---

## The data layer

```
mysql > merchandising > non_purchasers_feedback   (~1,200 rows)
```

| Column | Source | Purpose |
|---|---|---|
| feedback_id, visit_date, store_code, customer_name, customer_email, user_category, product_looking_for | Original | Captured by the salesman at the storefront |
| reason_for_non_purchase | Original | Verbose free-text reason |
| ground_truth_topic | Original (synthetic) | Eval anchor; would not exist in real production |
| inferred_topic, topic_confidence, non_purchase_type, attribute_value | Enriched by `data_prep/02_enrich_topics.py` | Topic classifier output — what the chat's Coder writes SQL against |

The one-time `02_enrich_topics.py` pre-flight turns verbose free text into
structured columns. Without it, the chat's Coder would have to fight
`LIKE '%design%'` patterns; with it, the SQL is clean.

---

## Use cases the system handles

| # | Use case | Path taken |
|---|---|---|
| A | "What's the top issue at X1?" | Planner(sql) → Coder → sql_executor → Writer → text |
| B | Follow-up: "now show me X4" | Planner resolves "X4" using chat history |
| C | "Tell me about my stores" (vague) | Planner(clarify) → assistant asks a focused follow-up |
| D | "Hi" / "What does X mean?" | Planner(direct) → conversational answer, no DB call |
| E | "Compare size issues across X1 and X4" | Full SQL path + Viz sub-pipeline → text + chart |
| F | Coder writes broken SQL | Code Reviewer catches; retry loop (max 5) |
| G | SQL ran but didn't answer the question | Output Reviewer catches; retry loop (max 2) |
| H | "Show Recommendations" button | Recommendations report — bypasses agents, computes pivots in pandas |
| I | Pre-flight enrichment | `data_prep/02_enrich_topics.py` (one-time) |

---

## Agents (9 in the chat pipeline)

| # | Agent | Job | Model | When |
|---|---|---|---|---|
| 1 | **Planner** | Classify into `sql`/`direct`/`clarify`; resolve chat-history references into a self-contained question | Haiku 4.5 | Every turn |
| 2 | **Coder** | Resolved question → `<reasoning>` (chain-of-thought: dimensions / filters / aggregation / completeness) + `<sql_query>` SQL | **Sonnet 4.6** | sql path |
| 3 | **Code Reviewer** | Technical correctness of SQL + verifies SQL implements the Coder's reasoning (catches LIMIT-vs-no-LIMIT and missing-dimension contradictions) | **Sonnet 4.6** | sql path |
| 4 | **Output Reviewer** | Semantic fit + visualization feasibility | Haiku 4.5 | sql path |
| 5 | **Writer** | Approved result → grounded natural-language answer | Haiku 4.5 | sql path + direct path |
| 6 | **Viz Coder** | Result → matplotlib code inside `<viz_code>` tags | Haiku 4.5 | sql path, when Output Reviewer says viz applies |
| 7 | **Viz Code Reviewer** | Static review of viz code (safety + correctness) | Haiku 4.5 | viz sub-pipeline |
| 8 | **Viz Reviewer** | Multimodal review of the rendered PNG (layout, legibility, fit) | Haiku 4.5 (vision) | viz sub-pipeline |
| 9 | **Supervisor** | Recovers from exhausted retry loops | Haiku 4.5 | Only when a retry cap is hit (rare) |

Plus an **offline Topic Classifier** in `data_prep/02_enrich_topics.py` —
functionally another agent, but runs once during pre-flight.

### Loop caps & Supervisor escape hatch

| Loop | Max retries | What happens if exhausted |
|---|---|---|
| Coder ↔ Code Reviewer | 5 | Orchestrator calls Supervisor |
| Output Reviewer ↔ Coder | 2 | Orchestrator calls Supervisor |
| Writer ↔ groundedness check | 2 | **Hard block**: ship `GROUNDEDNESS_FAIL_TEXT` instead of the unverified Writer text |
| Viz Coder ↔ Viz Code Reviewer | 2 | Viz pipeline aborts; the text answer still ships |
| Viz Reviewer revise round | 1 | Ship the last rendered PNG, or drop the chart entirely if verdict was `drop` |

The **Supervisor** sees the full step trace and returns one of four
actions:

- `abort_gracefully` — explain to the user we can't answer this
- `retry_with_strategy` — propose a fundamentally different approach; one final Coder attempt with that hint
- `ship_partial` — ship the best result with a caveat
- `ask_user` — return a clarifying question

---

## Tools (deterministic — no LLM)

Everything in `tools/` is called only by the Orchestrator. No agent ever
calls a tool directly.

| Tool | Module | Purpose |
|---|---|---|
| `sql_parser` | `tools/sql_tools.py` | Extract SQL from `<sql_query>...</sql_query>` tags (with a bare-SELECT fallback) |
| `sql_safety_guard` | `tools/sql_tools.py` | SELECT-only check; rejects banned keywords and multi-statement SQL |
| `sql_executor` | `tools/sql_tools.py` | Run SQL against MySQL; return rows or an error dict |
| `viz_code_parser` | `tools/viz_tools.py` | Extract Python from `<viz_code>...</viz_code>` tags |
| `viz_generator` | `tools/viz_tools.py` | Sandbox-exec matplotlib code with a restricted `__builtins__`; return PNG bytes |
| `png_to_base64` | `tools/viz_tools.py` | PNG → base64 helper (vestigial; `call_llm_vision` encodes internally) |
| `to_excel` | `tools/excel_tools.py` | Convert result rows to `.xlsx` bytes for the chat download button |
| `SCHEMA_TEXT` / `get_schema_for_prompt` | `tools/sql_tools.py` | Single source of truth for the table schema; substituted into the Coder and Code Reviewer prompts at import time |
| `groundedness_check` | `tools/groundedness.py` | Regex-extract every number from the Writer's prose, verify each appears in the result rows (with 0.5-unit rounding tolerance). Wired in-band as a **hard-block retry loop** (`MAX_GROUNDEDNESS_RETRIES = 2`); unverified text is replaced with `GROUNDEDNESS_FAIL_TEXT` |
| `log_turn` / `ensure_table` | `tools/trace_logger.py` | Persist one row per chat turn to the `chat_trace` MySQL table; idempotent table creation; every call wrapped in try/except so logging failures never break the chat |

Shared LLM plumbing lives in `agents/llm.py` (not under `tools/` because
every agent imports it):

| Helper | Purpose |
|---|---|
| `call_llm` / `call_llm_json` | aisuite text + JSON entry point; JSON fence stripping built in |
| `call_llm_vision` / `call_llm_vision_json` | Multimodal calls via the native `anthropic` SDK (aisuite's multimodal handling for Anthropic is patchy) |
| `load_prompt` | Read prompt `.txt` from `agents/prompts/` |

---

## Chat turn — SQL path, end-to-end

```
1. User types: "Compare size issues between X1 and X4"
   ▼
2. Streamlit → agent_backend.chat_with_agent → orchestrator.run
   ▼
3. Planner            → {path: "sql", plan: "...", resolved_question: "..."}
   ▼
4. Coder              → "<reasoning>...four named lines...</reasoning>
                         <sql_query>SELECT store_code, COUNT(*) ... </sql_query>"
   ▼
5. sql_parser         → extract reasoning + SQL
   ▼
6. sql_safety_guard   → ok (SELECT-only, no banned keywords)
   ▼
7. Code Reviewer      → ok (verifies SQL implements the reasoning)   ←── inner retry loop if not ok (max 5)
   ▼
8. sql_executor       → rows from MySQL
                      ←── on error: Code Reviewer with execution_error; retry
   ▼
9. Output Reviewer    → {verdict: ok, viz_applies: true}   ←── outer retry loop (max 2)
   ▼
   ┌─── runs in sequence ───┐
   ▼                        ▼
10a. Writer ↔ groundedness  10b. Viz Coder ↔ Viz Code Reviewer (max 2)
     (max 2 — hard block)        ▼ viz_generator (sandboxed exec)   → PNG bytes
     → grounded text             ▼ Viz Reviewer (multimodal)        → ok | revise (max 1) | drop
       OR GROUNDEDNESS_FAIL_TEXT
   └────────────┬────────────┘
                ▼
11. to_excel          → .xlsx bytes (whenever row_count > 0)
                ▼
12. AgentResponse{text, sql, chart_png, viz_code, excel_bytes, steps}
                ▼
13. Streamlit renders: text + chart + Excel download + Show SQL + Show chart code + Agent trace
                ▼
14. trace_logger.log_turn → INSERT row into chat_trace MySQL table (fail-safe)
```

If a retry budget is exhausted before reaching step 9 (or at any point in
the viz sub-pipeline), the Orchestrator calls the Supervisor, which
decides whether to abort, ship a partial result with a caveat, ask for
clarification, or attempt one final strategy retry.

---

## Evaluation

The system layers validation in **three concentric runtime rings** (every
turn, in-band) plus **four out-of-band evals** that you run on a cadence
or in CI. Together they cover both "is *this* answer right?" (rings) and
"did the system regress?" (out-of-band).

### Runtime rings — every turn, in-band

**Ring 1 — Deterministic guards.** Can't lie because they're regex /
Python validators.

- `sql_parser` and `viz_code_parser` enforce that the Coder / Viz Coder
  produced tag-wrapped output.
- `sql_safety_guard` enforces SELECT-only, blocks a banned-keyword list
  (`insert|update|delete|drop|alter|truncate|...|outfile`), and rejects
  multi-statement SQL.
- `viz_generator`'s sandbox restricts `__builtins__` so the viz code
  can't `open`, `exec`, `eval`, or `__import__`.
- Every reviewer's `verdict` is checked against a closed enum; the
  Planner's `path` is checked against `{sql, clarify, direct}`. Out-of-
  enum output raises, and the Orchestrator catches it as a transient
  failure (counted against the relevant retry budget).
- `02_enrich_topics.validate()` enforces the closed topic / non_purchase_type
  enums, the confidence range `[0, 1]`, and the string-or-null shape on
  `attribute_value`. Bad rows are left unenriched so a re-run picks
  them up.

**Ring 2 — Cross-reviewing LLM agents.** These ARE LLMs, so they can be
wrong — but they catch the bulk of subtle errors the deterministic
guards can't see.

- **Code Reviewer** judges SQL technical correctness twice per attempt:
  statically (before execution) and again after `sql_executor` returns
  an error (with the DB error string as extra context).
- **Output Reviewer** judges whether the returned rows semantically
  answer the question, and whether a chart would help. Bouncing here is
  the system's defense against "SQL ran cleanly but answers the wrong
  question."
- **Viz Reviewer** is multimodal — inspects the rendered PNG and judges
  layout, legibility, and fit. Catches cases where the code ran fine
  but the chart is confusing or mislabeled.

**Ring 3 — Retry loops with Supervisor escape hatch.** Every reviewer
veto bounces back to the relevant coder with the feedback string as a
hint. Budgets: 5 inner Coder ↔ Code Reviewer attempts, 2 outer Output
Reviewer rounds, 2 Viz Coder ↔ Viz Code Reviewer rounds, 1 Viz Reviewer
revise round. When the SQL loops exhaust, the **Supervisor** takes over
and picks one of four recovery actions (`abort_gracefully` /
`retry_with_strategy` / `ship_partial` / `ask_user`).

### Out-of-band evals — offline / CI

**1. Topic Classifier eval** — `data_prep/03_eval_topic_classifier.py`.
Reads every enriched row from MySQL, compares `inferred_topic` against
the synthetic `ground_truth_topic` anchor, and prints overall accuracy,
per-topic precision / recall / F1, the full confusion matrix, average
confidence on correct vs incorrect predictions, and the lowest-
confidence mistakes for spot-checking. Run after every enrichment (or
after a Topic Classifier prompt change) to catch regressions.

```
python3 data_prep/03_eval_topic_classifier.py
python3 data_prep/03_eval_topic_classifier.py --csv eval_report.csv
```

**2. Golden Q→SQL regression suite** — `tests/golden_sql_dataset.jsonl`
+ `tests/test_golden_sql.py`. 26 starter questions covering simple
single-store queries, comparisons, attribute drill-downs, the
colloquial-vs-literal vocabulary distinction (e.g. "what's missing"
should aggregate `attribute_value`, NOT filter on `inferred_topic =
'Stock Unavailable'`; and "what should we stock more of at X1?" —
no product named — must group by BOTH `product_looking_for` AND
`attribute_value` so the answer is operational per-product),
direct-path greetings, and clarify-path vague
questions. The runner pushes each through the full Orchestrator and
asserts: Planner picks the expected path, generated SQL contains the
required tokens and avoids the forbidden ones, executed row count is
within bounds. Exit code 0 if all pass, so it plugs into CI. Grow the
dataset as you find new failure modes.

```
python3 tests/test_golden_sql.py
python3 tests/test_golden_sql.py --only 001,019
python3 tests/test_golden_sql.py --verbose
```

This is a **shape check** — it confirms the pipeline didn't break,
but doesn't verify the actual numeric correctness of the answer. For
ground-truth answer comparison, see `tests/eval_prompts.md` — 15
manually-curated prompts at three difficulty tiers (single-level
aggregation, two-dimension top-N, multi-step discover-then-drilldown).
The two are complementary: golden_sql runs in CI on every change;
eval_prompts runs manually with Excel pivot tables on a slower
cadence.

**3. Writer groundedness check** — `tools/groundedness.py`. Cheap,
deterministic regex extraction of every number in the Writer's prose,
matched against a candidate set built from the result rows: every
individual cell value, every column grand total, each row's percentage
of its column total, per-group subtotals (sum of a numeric column for
each distinct value of a categorical column), per-group row counts,
and top-N partial sums (N ∈ {2..5}) of each numeric column sorted
descending. All with a 0.5-unit tolerance for rounding. The broad
candidate set matches the Writer's prompted compositional styles
("Necklace shows 41 requests total"), keeping false-positive rejections
low without weakening the safety net.
Wired **in-band** as a **hard-block retry loop** inside the
`_write_grounded_answer` orchestrator helper, called from
`_finalize_sql_answer` and from both Supervisor recovery branches.

The loop budget is `MAX_GROUNDEDNESS_RETRIES = 2` Writer attempts. On
each failure the unmatched numbers are fed back to the Writer as
`groundedness_feedback`, and the prior text is sent in as
`previous_text` so the next attempt knows exactly what NOT to repeat.
Every attempt emits a `writer` step (start / ok / fail) and a
`groundedness` step (ok / retry / fail) so the full retry history is
visible in the "How I got this answer" expander.

On budget exhaustion the Orchestrator **does not ship** the unverified
text — it replaces it with `GROUNDEDNESS_FAIL_TEXT`, an explicit
"I couldn't produce an answer with verifiable numbers" message that
points the user at the **Show SQL** and **Download data as Excel**
options for direct inspection. The `chat_trace` row for that turn
still carries `groundedness_warned = TRUE` for offline triage.

**4. Persistent trace logging** — `tools/trace_logger.py` writes one
row per chat turn to a new `chat_trace` table in the same MySQL
database as `non_purchasers_feedback`. Schema captures the user
message, resolved question, final SQL, `has_chart` / `has_excel`
flags, the `supervisor_invoked` flag, the `groundedness_warned`
flag, total / fail / retry step counts, the answer text, and the
full step trace as JSON in `steps_json`. Logging is idempotent
(`CREATE TABLE IF NOT EXISTS`, cached after the first call) and
fail-safe (every call wrapped in try/except — a logging error
never breaks the chat).

Example offline queries you can run against `chat_trace`:

```sql
-- How often does the Supervisor fire?
SELECT COUNT(*) FROM chat_trace WHERE supervisor_invoked = TRUE;

-- Average retry count per path
SELECT path, AVG(retry_count), AVG(fail_count), COUNT(*)
  FROM chat_trace
 GROUP BY path;

-- Turns where the groundedness check flagged a warning
SELECT trace_id, created_at, user_message, final_sql, answer_text
  FROM chat_trace
 WHERE groundedness_warned = TRUE
 ORDER BY created_at DESC;

-- Most expensive failures (lots of fails + steps)
SELECT trace_id, fail_count, step_count, user_message
  FROM chat_trace
 ORDER BY fail_count DESC, step_count DESC
 LIMIT 20;
```

### How to believe the system is right

In short: trust the **runtime rings** to filter most errors in real
time, run the **out-of-band evals** on a cadence to catch regressions,
and monitor the **`chat_trace`** table for patterns the runtime rings
missed.

The runtime rings reduce the surface area of obviously-wrong answers on
every turn. The out-of-band evals catch the harder failure mode where a
prompt tweak or a model upgrade silently shifts behavior — run them
before and after a change and confirm nothing regressed. The trace log
is the long-term feedback channel: once you've shipped, real usage
will surface failure patterns that no synthetic eval anticipated, and
the JSON `steps_json` blob lets you drill into exactly what went wrong.

### Verifying the integrations are alive

`tests/verify_evals.py` is a self-contained smoke test that confirms the
in-band evals are wired up. It runs four checks:

1. `tools.groundedness` imports and correctly flags a hallucinated number.
2. `tools.trace_logger.ensure_table()` succeeds — creates `chat_trace`
   if missing.
3. `log_turn` inserts a synthetic row and the script reads it back.
4. (Only if `ANTHROPIC_API_KEY` + `MYSQL_PASSWORD` are set) sends a
   real numeric question through `orchestrator.run`, confirms a
   `groundedness` step event landed in the trace AND a matching row
   appeared in `chat_trace`.

```
python3 tests/verify_evals.py
python3 tests/verify_evals.py --skip-live    # no LLM call needed
```

Exit code 0 if everything that can run passes; non-zero on any failure.

### The full test suite — overview

Six items live under `tests/`. Each plays a distinct role in the
trust-but-verify story:

| Test | Type | What it checks | When to run |
|---|---|---|---|
| `test_planner.py` | Eyeball smoke | Planner routes turns correctly + resolves history references | After editing `planner.txt` |
| `test_orchestrator.py` | End-to-end smoke | Full pipeline runs cleanly with real MySQL + LLM | After refactors / model changes |
| `golden_sql_dataset.jsonl` | Data file | 25 Q→SQL pattern assertions consumed by the runner below | Append entries as new failure modes surface |
| `test_golden_sql.py` | Automated regression | SQL shape + Planner path + row count bounds; CI-style exit codes | After ANY prompt change |
| `verify_evals.py` | Integration smoke | Groundedness check + trace_logger wiring | After `tools/groundedness.py` or `tools/trace_logger.py` edits |
| `eval_prompts.md` | Manual ground-truth | 15 prompts (Tier 1/2/3); compare actual numbers vs Excel pivots | Weekly, or before/after big changes |

The distinction between `test_golden_sql.py` and `eval_prompts.md`
matters: the former is a **shape check** (did the pipeline produce
structurally reasonable SQL?), the latter is an **answer-correctness
check** (is the answer numerically right?). Both are needed.

See **`tests/README.md`** for the operational guide — every test
explained with example output, exact run commands, output-saving
recipes, dependency requirements, and a recommended cadence matrix
keyed off "what did you just edit".

---

## Repo layout

```
non_purchaser_feedback/
├── .env / .env.example / .gitignore
├── requirements.txt
├── streamlit_app.py             UI — sidebar + chat view + report view
├── agent_backend.py             seam between UI and DB/agents
│
├── agents/
│   ├── __init__.py
│   ├── schemas.py               shared dataclasses (PlannerOutput, AgentResponse, StepEvent, ...)
│   ├── llm.py                   aisuite + native anthropic vision entry point
│   ├── orchestrator.py          state machine wiring the 9 agents + tools
│   ├── planner.py               sql / direct / clarify routing + history resolution
│   ├── coder.py                 NL question → SQL
│   ├── code_reviewer.py         static + post-exec SQL review
│   ├── output_reviewer.py       semantic fit + viz decision
│   ├── writer.py                rows → grounded English answer
│   ├── viz_coder.py             rows → matplotlib snippet
│   ├── viz_code_reviewer.py     static review of viz code
│   ├── viz_reviewer.py          multimodal review of rendered PNG
│   ├── supervisor.py            recovery brain when a retry loop exhausts
│   └── prompts/                 system prompts as .txt files (one per agent)
│
├── tools/
│   ├── __init__.py              public surface re-exports
│   ├── sql_tools.py             schema text, parser, safety guard, executor
│   ├── viz_tools.py             viz parser, sandboxed matplotlib runner, png_to_base64
│   ├── excel_tools.py           rows → .xlsx bytes
│   ├── groundedness.py          regex-based Writer numeric grounding check (in-band)
│   └── trace_logger.py          MySQL chat_trace persistence (one row per turn)
│
├── data_prep/
│   ├── 01_generate_feedback_data_mysql.py   synthesize ~1,200 rows → MySQL
│   ├── 02_enrich_topics.py                  one-time LLM topic enrichment
│   └── 03_eval_topic_classifier.py          offline accuracy / confusion matrix
│
├── data_sample/
│   └── non_purchasers_feedback.csv          1,200 seed rows
│
├── tests/
│   ├── README.md                   operational guide for every test — what, how, when
│   ├── test_planner.py             Planner smoke test (eyeball-mode runner)
│   ├── test_orchestrator.py        End-to-end smoke test against real MySQL
│   ├── golden_sql_dataset.jsonl    25 Q→SQL pattern assertions
│   ├── test_golden_sql.py          CI-style regression runner
│   ├── verify_evals.py             7-check smoke test for the eval / persistence integrations
│   └── eval_prompts.md             15 manual ground-truth eval prompts (Tier 1/2/3 difficulty)
│
└── docs/
    ├── 00_system_overview.md                  this file
    ├── 06_topology_graph.{svg,png,jpg}        call-graph view (Orchestrator at the hub)
    ├── 07_workflow_tree.{svg,png,jpg}         temporal-flow view (Planner → ... → Writer)
    ├── 08_prompts_reference.md                catalogue of every prompt
    ├── 09_agents_and_tools_reference.md       per-component spec table
    ├── 10_layered_architecture.md             index for the three layered-architecture docs
    ├── 10_layered_architecture_A_full.md      full 12-layer deep-dive (engineers)
    ├── 10_layered_architecture_B_grouped.md   8-layer grouped tutorial (onboarding)
    └── 10_layered_architecture_C_executive.md 5-bucket high-level view (stakeholders)
```
