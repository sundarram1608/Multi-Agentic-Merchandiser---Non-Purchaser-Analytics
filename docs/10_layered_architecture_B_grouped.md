# Layered Architecture — Grouped Walkthrough (Option B: 8 layers)

A tutorial-style architecture document for technical readers who want
to understand the system end-to-end without diving into every
implementation detail. Three pairs of layers from the full 12-layer
view are merged here:

- **Tool + Validation** → **Tool & Validation Layer** (both are
  deterministic checks/actions; merging makes the determinism story
  read more naturally)
- **Orchestration + Recovery** → **Orchestration & Recovery Layer**
  (both are control-plane; recovery is just a special case of
  orchestration when something fails)
- **Observability + Evaluation** → **Observability & Evaluation
  Layer** (both are after-the-fact quality concerns)

For the full 12-layer version with every implementation detail,
see `10_layered_architecture_A_full.md`. For a 5-bucket executive
view, see `10_layered_architecture_C_executive.md`.

---

## High-level picture

The Merchandising AI is a multi-agent chat assistant fronted by
Streamlit. A rule-based Python Orchestrator coordinates 9 LLM
agents and 11 deterministic tools to turn natural-language
questions into SQL, execute it against MySQL, compose grounded
natural-language answers, optionally render matplotlib charts, and
persist every turn to an auditable `chat_trace` table.

The architecture deliberately rejects the MCP / autonomous-loop
pattern in favor of a rule-based Orchestrator. See the note in
Layer 5 (Orchestration & Recovery).

---

## Foundational design choices

Five cross-cutting decisions touch every layer.

**1. Determinism over creativity.** All 9 LLM agents run at
`temperature=0.0`. The Writer was originally `0.2` for natural
prose but produced inconsistent outputs; dropped to `0.0` because
determinism beats stylistic variation when trust matters.

**2. Rule-based Orchestrator.** The control plane is pure Python.
LLM agents output structured text (`<sql_query>`, `<reasoning>`,
JSON verdicts, prose); the Orchestrator parses and routes
deterministically. See Layer 5 for the full MCP discussion.

**3. Closed enums.** Topic Classifier output, every reviewer's
verdict, Planner path, and Supervisor action are all enum-checked.
Out-of-enum LLM output is rejected as a transient failure.

**4. Hard-block over soft-block for numeric grounding.** When the
Writer's prose contains a number that doesn't reconcile, the
orchestrator retries up to 2 times, then ships an explicit
"I couldn't produce verifiable numbers" message instead of the
unverified text.

**5. Schema vocabulary discipline.** Column names carry semantic
weight for LLMs. The original `attribute_type` / `attribute_value`
pair was renamed (`non_purchase_type` / `attribute_value`) because
the user word "attributes" naturally collided with both columns.
Schema-level disambiguation beats prompt-level disambiguation.

---

## Layer 1: Data Layer

**What it does.** Holds the raw substrate the whole system reads
from, plus the persistent audit log it writes to.

**Components.**

- **MySQL database `merchandising`** — InnoDB + utf8mb4, default
  local connection.
- **Table `non_purchasers_feedback`** — ~1,200 rows. 13 columns:
  9 captured (`feedback_id`, `visit_date`, `store_code`,
  `customer_name`, `customer_email`, `user_category`,
  `product_looking_for`, `reason_for_non_purchase`,
  `ground_truth_topic`) + 4 enriched (`inferred_topic`,
  `topic_confidence`, `non_purchase_type`, `attribute_value`).
  Indexed on `store_code` and `product_looking_for`.
- **Table `chat_trace`** — created lazily by
  `tools/trace_logger.py:ensure_table()`. One row per chat turn
  with derived metadata + full step trace as JSON in `steps_json`.
- **`data_prep/01_generate_feedback_data_mysql.py`** — seeds
  ~1,200 verbose-text rows with store-specific topic/product
  biases. `random.seed(42)` for reproducibility.
- **`data_sample/non_purchasers_feedback.csv`** — CSV snapshot for
  offline ground-truth pivot tables.

**Key design choices.**

- InnoDB chosen for transactional inserts (per-row crash safety in
  the enrichment script).
- The `ground_truth_topic` column carried in production schema so
  the offline classifier eval can measure accuracy in place.
- `chat_trace` schema denormalized — one row per turn with full
  trace as JSON. Cheap to write, common fields queryable without
  parsing JSON.

---

## Layer 2: Knowledge Layer

**What it does.** Provides structured, enriched, classified
knowledge derived from raw data. Lets agents reason at higher
abstraction than free text. Run once per data ingestion; never
during chat.

**Components.**

- **`data_prep/02_enrich_topics.py`** — offline Topic Classifier.
  For each unenriched row, calls Haiku 4.5 (`temperature=0`,
  `max_tokens=200`) to extract `inferred_topic` (10 canonical
  topics), `topic_confidence`, `non_purchase_type` (9 categories),
  `attribute_value` (normalized phrase or NULL). Idempotent and
  resumable.
- **Closed enums** enforced in prompt + validated in code.
  Topics: `Design Unavailable`, `Size Unavailable`,
  `Stock Unavailable`, `Price Too High`, `Quality Concerns`,
  `Weight Concerns`, `Color/Finish Mismatch`,
  `Customization Not Offered`, `Sales Service`, `Others`.
  Non-purchase types: `design`, `size`, `color`, `weight`,
  `price`, `customization`, `service`, `stock`, `none`.
- **Topic → non_purchase_type mapping** enforced in prompt with 8
  worked examples covering each kind of value normalization.
- **`tools/sql_tools.py:SCHEMA_TEXT`** — canonical schema
  description. Single source of truth substituted into Coder /
  Code Reviewer prompts at every call (no cache).

**Key design choices.**

- Offline enrichment vs on-demand: doing this once at ingestion
  means chat SQL aggregates against clean structured columns
  instead of pattern-matching free text.
- 8 few-shot examples cover the exact format the model must
  produce, with one example per attribute_type plus stock-style
  NULL values and Others/postponing NULL values.
- Validation in code (closed enums + range + type) plus validation
  in prompt — belt-and-braces against LLM drift.

---

## Layer 3: Memory Layer

**What it does.** Manages conversation state. Short-term (this
session) via Streamlit session state; long-term (across sessions)
via `chat_trace`.

**Components.**

- **`st.session_state.messages`** — short-term chat history. List
  of dicts; lives in the browser tab.
- **Planner's 8-message look-back** — `history[-8:]` slice in
  `agents/planner.py:_format_history`. Documented inline with the
  rationale: (a) follow-up references almost always point back 1-2
  turns; (b) Planner input budget needs to stay small; (c)
  per-turn cost stays flat as session grows; (d) 8 is a generous
  round number.
- **`resolved_question` mechanism** — Planner produces a
  context-free version of the user's question. Every downstream
  agent reads this, never the raw user message. History resolution
  happens once, in the Planner.
- **`chat_trace` MySQL table** — long-term memory; queryable for
  failure-pattern analysis (see Layer 7).

**Key design choices.**

- History resolution happens once, at the Planner. Downstream
  agents are stateless on history. This prevents history-aware
  bugs from spreading; trade-off is the Planner is a single point
  of failure for history-related errors (mitigated by the
  hierarchical-analysis follow-up rule in the prompt).
- The Planner's prompt includes a special "Multi-turn hierarchical-
  analysis follow-ups" section that distinguishes pattern-extension
  (Case H: re-discover at new scope) from value-carry-forward
  (Case V: preserve specific values).

---

## Layer 4: Reasoning + Reflection Layer

**What it does.** Houses every LLM agent in the system. The
*reasoning* agents (Planner, Coder, Writer, Viz Coder, Supervisor)
produce structured outputs; the *reflection* agents (Code
Reviewer, Output Reviewer, Viz Code Reviewer, Viz Reviewer)
critique those outputs and request retries when needed.

(This grouping conflates Reasoning and Reflection from the 12-
layer view; they share enough LLM-mechanics in common that
treating them as one layer makes the tutorial clearer. If you
need the strict separation, see Option A.)

### Reasoning agents

| Agent | Model | Temp | max_tokens | Cached? | Role |
|---|---|---|---|---|---|
| Planner | Haiku 4.5 | 0.0 | 400 | Yes | Route + resolve history |
| Coder | **Sonnet 4.6** | 0.0 | 900 | **No** | Reasoning + SQL |
| Writer | Haiku 4.5 | 0.0 | 800 | Yes | Compose answer |
| Viz Coder | Haiku 4.5 | 0.0 | 900 | Yes | matplotlib code |
| Supervisor | Haiku 4.5 | 0.0 | 400 | Yes | Recovery decision |

- **Planner** routes to `sql` / `direct` / `clarify` and produces
  a `resolved_question`. Critical prompt sections: four-case
  attribute interpretation tree, multi-turn hierarchical-analysis
  rule, colloquial-vs-literal vocabulary mapping (with the
  product-dimension rule: if no product is named in a stock-more
  question, the resolved_question must require a breakdown by
  both `product_looking_for` and `attribute_value`).
- **Coder** outputs a `<reasoning>` block (Dimensions / Filters /
  Aggregation / Completeness) followed by a `<sql_query>` block.
  Prompt has the preamble forcing `non_purchase_type` for
  "attributes", the LIMIT rule, 7 worked examples including the
  nested-CTE discovery-then-drilldown case.
- **Writer** composes markdown. Receives a pre-computed `## Safe
  values for grounding` section in its user block (from
  `safe_derivations_summary`); picks from that list rather than
  computing arithmetic.
- **Viz Coder** writes matplotlib snippets that run in the
  sandbox. No `import` allowed; `df` and helpers pre-loaded.
- **Supervisor** is logically here (it's an LLM agent) but
  operationally belongs to Recovery; covered in Layer 5.

### Reflection agents

| Reviewer | Model | Temp | max_tokens | Verdict enum |
|---|---|---|---|---|
| Code Reviewer | **Sonnet 4.6** | 0.0 | 400 | `{ok, retry}` |
| Output Reviewer | Haiku 4.5 | 0.0 | 300 | `{ok, retry}` (+ `viz_applies`) |
| Viz Code Reviewer | Haiku 4.5 | 0.0 | 250 | `{ok, retry}` |
| Viz Reviewer | Haiku vision | 0.0 | 300 | `{ok, revise, drop}` |

- **Code Reviewer** judges SQL technical correctness AND verifies
  the SQL implements the Coder's `<reasoning>` block. Two worked
  examples in the prompt show the reasoning-vs-SQL contradiction
  cases (LIMIT mismatch, missing dimension).
- **Output Reviewer** sees the resolved question, the SQL, and 30
  rows; decides semantic fit + whether a chart applies.
- **Viz Code Reviewer** static-reviews the matplotlib code before
  sandbox execution. Banned identifiers enforced.
- **Viz Reviewer** is the only multimodal agent — uses the native
  Anthropic SDK because aisuite's Anthropic multimodal handling is
  patchy. Sees the rendered PNG + the question.

### Key design choices

- **Sonnet 4.6 for Coder + Code Reviewer.** The SQL-quality
  bottleneck. Haiku failed on window-function syntax and column
  disambiguation; Sonnet handles both. The reviewer must match the
  Coder's tier to catch its mistakes.
- **Haiku 4.5 for everyone else.** Planner does routing (lighter
  task); Writer composes from pre-computed safe values; Output
  Reviewer judges binary fit; Viz pipeline does short snippets.
  3-5x cost saving with no observed quality drop.
- **`<reasoning>` block on Coder.** Chain-of-thought forces
  compositional commitment before SQL. The reviewer can verify SQL
  matches reasoning, catching the most damaging failure mode
  (plausible-looking SQL that doesn't match intent).
- **`safe_derivations_summary` injection for Writer.** Takes
  arithmetic OFF the LLM. Writer picks from a pre-computed list of
  legitimate numeric values rather than synthesizing them. This +
  temp 0 + Sonnet upgrade for Coder eliminated the
  same-question-twice-gives-different-answers problem.
- **Coder / Code Reviewer prompts NOT cached.** Re-read from disk
  on every call so prompt edits take effect without a Streamlit
  restart. Saves debugging time during iteration. Other agents
  still cache their prompts because they're edited less often.

---

## Layer 5: Orchestration & Recovery Layer

**What it does.** The conductor. Wires every other layer together
and handles graceful degradation when things fail.

**Components.**

- **`agents/orchestrator.py`** — pure Python, no LLMs in the
  decision logic. Public entry `run(user_message, history) ->
  AgentResponse`, wrapping `_run_impl` plus a persistence call to
  `chat_trace`.
- **Major helpers:** `_run_sql_path`, `_inner_sql_loop`,
  `_finalize_sql_answer`, `_run_viz_pipeline`,
  `_supervisor_fallback`, `_write_grounded_answer`,
  `_attach_excel`, `_step`.
- **Retry budgets:**
  - `MAX_CODE_REVIEW_RETRIES = 5` (Coder ↔ Code Reviewer + executor)
  - `MAX_OUTPUT_REVIEW_RETRIES = 2` (Output Reviewer outer loop)
  - `MAX_GROUNDEDNESS_RETRIES = 2` (Writer ↔ groundedness, hard-block)
  - `MAX_VIZ_CODE_RETRIES = 2` (Viz Coder ↔ Viz Code Reviewer)
  - `MAX_VIZ_REVISE = 1` (Viz Reviewer revise)
- **Supervisor escape hatch.** Activated when the SQL loops
  exhaust. Picks one of four actions:
  `abort_gracefully` / `retry_with_strategy` / `ship_partial` /
  `ask_user`. `retry_with_strategy` is a one-shot.
- **`GROUNDEDNESS_FAIL_TEXT`** — fixed string shipped when the
  Writer groundedness loop exhausts.
- **Fail-safe logging.** `log_turn` is wrapped at two levels; a
  MySQL outage cannot break the chat.

### Key design choices

- **Rule-based, not LLM-driven.** Every control-flow decision is
  hardcoded. The Coder doesn't decide "I'll call sql_executor
  next" — that's hardcoded in `_inner_sql_loop`. This trade-off
  buys predictability, reliable retry budgets, easier validation,
  cheaper execution, and easier debugging at the cost of agent
  autonomy.
- **Single hub.** Every agent and every tool is called only by
  the Orchestrator. No agent calls another agent; no agent calls a
  tool directly. This makes control flow exhaustively visible in
  one file.
- **Helpers extracted for the three Writer call sites.** The
  hard-block grounding loop and Excel attachment are invoked from
  three places: happy path, ship_partial Supervisor branch,
  retry_with_strategy Supervisor branch. The helpers prevent
  recovery branches from silently dropping affordances.
- **Different loops escalate to different fallbacks.** Coder /
  Output Reviewer loops → Supervisor. Writer / Viz loops → fixed
  fallbacks (failure text / chart drop). Because Writer / Viz
  failure is local and doesn't benefit from a strategy change.

### Note: no MCP layer (by design)

This project deliberately does NOT use the **Model Context
Protocol (MCP)** or any autonomous-agent / function-calling
pattern. There is no MCP server, no MCP client, no `tool_use`
messages from the LLM.

Instead, the LLM agents output structured text (`<sql_query>`,
`<reasoning>`, `<viz_code>` tags, JSON verdicts, prose), and the
Orchestrator's Python control flow parses the text and decides
deterministically which tool to call next. The Coder is just a
text-emitter; it has no awareness that `sql_safety_guard` and
`sql_executor` exist.

**Why the non-MCP pattern was chosen:**

- **Predictability** — same control flow every turn; no LLM
  decisions about tool sequencing.
- **Reliable retry budgets** — enforced by Python, not by LLM
  reasoning.
- **Easier validation** — every tool call goes through a known
  control-flow point; the trace, the chat_trace log, and the
  deterministic guards all attach cleanly.
- **Cheaper** — no tool-use round trips; one LLM call per agent
  per attempt.
- **Easier debugging** — control flow is in actual Python files,
  not in inscrutable LLM tool-use decisions.

The trade-off is agent autonomy. For a focused merchandising chat
where the same control flow applies every turn, autonomy is the
wrong trade. If the project ever opens up to a more autonomous
"do anything" use case, MCP would be the natural pivot.

---

## Layer 6: Tool & Validation Layer

**What it does.** Pure / deterministic Python helpers + non-LLM
checks. The Tool half is what gets called; the Validation half is
what catches errors. They're grouped here because both are
deterministic and cannot be social-engineered.

### Tools

All re-exported from `tools/__init__.py`. Called exclusively by
the Orchestrator.

- **`sql_parser`** — extracts SQL from `<sql_query>` tags. Also
  extracts the `<reasoning>` block. Returns `ParseResult(ok, sql,
  reason, reasoning)`.
- **`sql_safety_guard`** — SELECT-only / banned-keyword check.
- **`sql_executor`** — runs SQL via `mysql.connector`, caps result
  at 500 rows, returns `{ok, columns, rows, row_count,
  truncated}` or `{ok: False, error}`.
- **`viz_code_parser`** — extracts Python from `<viz_code>` tags.
- **`viz_generator`** — sandboxed `exec()` with restricted
  `__builtins__`. Returns PNG bytes or error.
- **`to_excel`** — openpyxl-backed `.xlsx` bytes. Populates
  `LAST_TO_EXCEL_ERROR` on failure for diagnostic propagation.
- **`png_to_base64`** — vestigial; `call_llm_vision` does its own
  base64 encoding.
- **`groundedness_check`** — see Validation below.
- **`safe_derivations_summary`** — pre-computes safe values for
  Writer injection.
- **`log_turn`** + **`ensure_table`** — `chat_trace` persistence.
- **`SCHEMA_TEXT`** + **`get_schema_for_prompt`** — schema source
  of truth (Layer 2).

### Validation (deterministic guards)

- **`sql_safety_guard`** — same tool, also a guard. SELECT-only;
  blocks `insert|update|delete|drop|alter|truncate|rename|create|
  grant|revoke|replace|merge|call|execute|exec|load|outfile|into
  outfile`; rejects multi-statement (`;` followed by non-whitespace).
- **`viz_generator` sandbox** — restricted `__builtins__`. Blocks
  `open`, `exec`, `eval`, `__import__`, `input`. Whitelisted
  builtins: `len`, `range`, `enumerate`, `zip`, `sum`, `min`,
  `max`, `abs`, `round`, `sorted`, `reversed`, `list`, `dict`,
  `set`, `tuple`, `str`, `int`, `float`, `bool`, `isinstance`,
  `print`, `True/False/None`.
- **`groundedness_check`** — verifies every numeric token in the
  Writer's prose appears in (or can be derived from) the result
  rows. Candidate set includes 7 derivation patterns: individual
  cell values, numbers embedded in string labels (e.g. "10 inch"),
  per-column grand totals, per-row column percentages, per-group
  subtotals, per-group row counts, top-N partial sums for
  N ∈ {2..5}. Numeric extraction regex includes `(?!\w)` lookahead
  to skip `25k`-style labels with unit suffixes.
- **Enum validators** — Planner path, all reviewer verdicts,
  Supervisor action, all type-checked at parse time.
- **`02_enrich_topics.validate()`** — closed-enum + range checks
  on Topic Classifier output.

### Key design choices

- **Regex over LLM-as-judge for groundedness.** Deterministic,
  free, verifiable. Trade-off: can't judge semantic attribution.
- **Broadened candidate set vs strict check.** Earlier strict
  versions rejected legitimate Writer compositional patterns;
  expanded to 7 patterns reduces false-negative rejections.
- **Hard-block, not soft-block.** Better to ship a failure message
  than wrong numbers.
- **Sandbox with `__builtins__` restriction, not subprocess.** ~100x
  faster; reasonable for single-user dev. Documented as not
  production-grade.

---

## Layer 7: Observability & Evaluation Layer

**What it does.** Records what happened (Observability) and
verifies the system is doing its job (Evaluation). Grouped here
because both are after-the-fact quality concerns.

### Observability

- **`StepEvent` dataclass** — `agent`, `status` (`start`/`ok`/
  `retry`/`fail`), `summary`, `detail` dict. The unit of the
  trace.
- **`AgentResponse.steps`** — list of StepEvents per turn.
  Surfaced in Streamlit's "How I got this answer" expander.
- **`trace_logger.log_turn`** — extracts derived fields
  (`path`, `resolved_question`, `supervisor_invoked`,
  `groundedness_warned`, `fail_count`, `retry_count`) and inserts
  one row into `chat_trace` per turn with full step JSON.
- **Streamlit surfaces:** answer markdown, chart PNG, Excel
  download, Show SQL expander, Show chart code expander, How I
  got this answer expander.
- **`LAST_TO_EXCEL_ERROR`** — tool-level diagnostic propagated into
  the trace when `to_excel` fails.

### Evaluation

**In-band — three runtime rings:**

1. **Deterministic guards** (Layer 6 validation) — `sql_parser`,
   `sql_safety_guard`, `viz_code_parser`, `viz_generator` sandbox,
   `groundedness_check`, enum validators.
2. **Cross-reviewing LLM agents** (Layer 4 reflection) — Code
   Reviewer, Output Reviewer, Viz Code Reviewer, Viz Reviewer.
3. **Retry loops with Supervisor** (Layer 5) — bounded retry
   budgets per loop, Supervisor escape hatch for SQL-path
   exhaustion.

**Out-of-band — runnable tests:**

- **`tests/verify_evals.py`** — 7-check integration smoke test
  (groundedness behaviors + trace_logger round-trip + optional
  live chat turn).
- **`tests/test_planner.py`** — eyeball smoke test, 9 questions.
- **`tests/test_orchestrator.py`** — end-to-end smoke test, 5
  questions.
- **`tests/test_golden_sql.py`** + **`golden_sql_dataset.jsonl`** —
  25-entry automated regression suite. Asserts Planner path + SQL
  tokens + row-count bounds. Exit code 0 on all-pass. **Shape
  check.**
- **`tests/eval_prompts.md`** — 15 manual prompts at three
  difficulty tiers for ground-truth comparison against Excel
  pivots. **Answer correctness check.**
- **`tests/README.md`** — operational guide for running every
  test.
- **`data_prep/03_eval_topic_classifier.py`** — offline Topic
  Classifier accuracy + confusion matrix.

**Example `chat_trace` queries:**

```sql
SELECT COUNT(*) FROM chat_trace WHERE supervisor_invoked = TRUE;
SELECT path, AVG(retry_count), AVG(fail_count) FROM chat_trace GROUP BY path;
SELECT user_message, answer_text FROM chat_trace
  WHERE groundedness_warned = TRUE ORDER BY created_at DESC;
```

### Key design choices

- **Three runtime rings + four out-of-band evals.** Rings catch
  errors per-turn; out-of-band evals catch *regressions*.
- **Shape check vs answer correctness.** `test_golden_sql.py` (CI)
  asserts SQL structure looks right; `eval_prompts.md` (manual)
  asserts the numbers are correct. Both needed.
- **Fail-safe logging.** Double try/except around `log_turn`.
  MySQL outage cannot break the chat.
- **JSON column for full trace + derived columns for common
  queries.** Cheap to write, easy to query.

---

## Layer 8: Interface Layer

**What it does.** User-facing surfaces. Insulates the backend
agentic system from UI implementation.

**Components.**

- **`streamlit_app.py`** — Streamlit app. Two views:
  - **Chat view** — suggested starters, message history,
    `st.chat_input`. Each assistant message: text, chart PNG,
    Excel download button, Show SQL expander, Show chart code
    expander, agent trace expander.
  - **Recommendations view** — one-click deterministic report.
    No agents, no LLM. Headline metrics, Top-10 focus areas,
    per-store tabs, distribution charts.
- **`agent_backend.py`** — UI↔backend seam.
  - `chat_with_agent(user_message, history, filters)` — lazy
    imports the orchestrator, calls it, maps response into
    `AgentResponse`. Wrapped in try/except.
  - `generate_full_recommendations(filters)` — deterministic
    pandas pivot computation. No LLM.
  - `get_data_summary()` — sidebar freshness card data.
  - `_query_df`, `_where_clause` — SQL helpers.
- **Sidebar layout:** primary action button (Show
  Recommendations / Back to Chat), filters block (Recommendations
  view only — hidden on chat view), Data card, Clear chat.
- **Session state:** `view`, `messages`, `filters`.

**Key design choices.**

- **`agent_backend.py` is the seam.** UI never imports `agents` or
  `tools` directly. The agentic system can be swapped without
  touching Streamlit.
- **Lazy orchestrator import.** Done inside `chat_with_agent` so
  the Recommendations view doesn't pay the import cost.
- **Filters hidden on chat view.** Chat path doesn't use them;
  showing them would suggest they apply.
- **Recommendations view is deterministic.** Same data + same
  filters always produces the same report; for stakeholder
  consistency.
- **Error envelope.** Orchestrator exceptions surface as polite
  UI messages, not Streamlit tracebacks.

---

## Cross-cutting concerns

**Determinism end-to-end.** Every LLM agent at temp 0. Coder /
Code Reviewer at Sonnet for stable SQL. Safe-values injection for
Writer determinism. Trace logged per turn.

**Chain-of-thought scaffolding.** `<reasoning>` block on Coder,
safe-values list on Writer, reasoning-verification rule on Code
Reviewer. Three coordinated mechanisms that collectively moved
the system from "occasionally wrong" to "reliably right on first
attempt."

**Hard-block vs soft-block.** Two hard blocks: groundedness
(ships failure message) and viz exhaustion (drops chart). Most
reviewer vetos are soft (retry with feedback).

**Cost per turn (rough):** $0.001-0.002 direct/clarify; $0.02
SQL happy case; up to $0.15 worst case with full retries.

**Latency per turn (rough):** 15-20 s SQL happy path; 30-60 s
with retries.

---

## Known limitations

1. Groundedness verifies values, not attributions.
2. Planner is single point of failure for history.
3. Output Reviewer at Haiku can rubber-stamp.
4. PII not guarded (`customer_name`, `customer_email`).
5. Sandbox is dev-grade, not production-grade.
6. Tests are not in CI.
7. Golden SQL set is starter-size (25 entries).
8. Multimodal Viz Reviewer is occasionally flaky.
9. No alerting on `chat_trace` patterns.
10. Sonnet at temp 0 isn't strictly deterministic.
11. Prompt-version traceability is git-only.
12. Cost increased ~3-5x with Sonnet upgrade.

For full detail on each, see the limitations section in
`10_layered_architecture_A_full.md`.
