# Layered Architecture — Full Technical Breakdown (Option A: 12 layers)

The canonical implementation-level reference for the Merchandising AI
project. Written for engineers extending, debugging, or auditing the
system. Every design choice is named, every model parameter recorded,
every layer's responsibilities and interfaces documented at the level
of detail you'd need to confidently touch any part of the code.

For a tutorial-style 8-layer version, see
`10_layered_architecture_B_grouped.md`.
For a 5-bucket executive view, see
`10_layered_architecture_C_executive.md`.

---

## Conventions

- File paths are relative to the project root.
- Numbered retry-budget constants come from `agents/orchestrator.py`:
  `MAX_CODE_REVIEW_RETRIES=5`, `MAX_OUTPUT_REVIEW_RETRIES=2`,
  `MAX_GROUNDEDNESS_RETRIES=2`, `MAX_VIZ_CODE_RETRIES=2`,
  `MAX_VIZ_REVISE=1`.
- Model identifiers come from `agents/llm.py`:
  `MODEL_HAIKU = "anthropic:claude-haiku-4-5-20251001"`,
  `MODEL_SONNET = "anthropic:claude-sonnet-4-6"`.
- All temperatures are `0.0` unless explicitly noted.
- Schema column names reflect the post-rename state
  (`non_purchase_type`, not the historical `attribute_type`).

---

## Foundational design choices (cross-cutting)

Five decisions touch every layer of the system. They are listed first
so they don't have to be re-explained in each layer.

**1. Determinism over creativity.** All 9 LLM agents run at
`temperature=0.0`. The Writer was originally `0.2` for natural prose
but produced inconsistent outputs across same-question retries; it
was dropped to `0.0`. Determinism beats stylistic variation in this
domain, where the same input should yield the same answer for trust
and replay. (Anthropic's API still has slight non-determinism at
temp 0; same input *usually* but not always produces identical
output. This is acknowledged in the limitations.)

**2. Rule-based Orchestrator, not LLM-driven agentic loop.** The
Orchestrator in `agents/orchestrator.py` is pure Python with no LLM
in its decision logic. It calls every agent and every tool itself.
LLM agents output structured text (`<sql_query>`, `<reasoning>`,
`<viz_code>` tags, JSON verdicts, prose); the Orchestrator parses
and routes. This deliberately rejects the MCP / autonomous-loop
pattern. See Layer 9 for the full rationale.

**3. Closed enums whenever possible.** Topic classifier output is
constrained to 10 topics and 9 `non_purchase_type` values, validated
in code (`02_enrich_topics.validate()`). Reviewer verdicts are enum-
checked (`{"ok","retry"}`, `{"ok","revise","drop"}`, etc.).
Planner path is enum-checked against `{"sql","clarify","direct"}`.
Out-of-enum LLM output is treated as a transient failure, not a
silent acceptance.

**4. Hard-block over soft-block for numeric grounding.** When the
Writer's prose contains a number that doesn't reconcile against the
result rows or any safe derivation, the orchestrator retries up to
`MAX_GROUNDEDNESS_RETRIES=2` times, then ships an explicit
`GROUNDEDNESS_FAIL_TEXT` constant instead of the unverified text.
Other systems would let the wrong text through with a warning; here
the chat tells the user the data is available and refuses to invent.

**5. Schema vocabulary discipline.** Column names in the data carry
semantic weight for the LLM. The original `attribute_type` /
`attribute_value` pair conflated two concepts because the user
language ("top 3 attributes") could plausibly map to either column.
Renaming the column to `non_purchase_type` (keeping `attribute_value`
intact) gave the schema a vocabulary the LLM cannot misinterpret.
Schema-level disambiguation beats prompt-level disambiguation.

---

## Layer 1: Data Layer — what we have

**What this layer does.** Holds the raw substrate: the source-of-
truth feedback data that the entire system reads from, and the
persistent audit log it writes to.

**Components.**

- **MySQL database `merchandising`** — InnoDB engine, utf8mb4
  charset. Runs locally by default (host `127.0.0.1`, port `3306`);
  connection config in `.env` via the five `MYSQL_*` variables.
- **Table `non_purchasers_feedback`** — ~1,200 rows. 13 columns
  split into three groups:
  - **Captured columns (9):** `feedback_id` INT PK, `visit_date`
    DATE (last ~90 days), `store_code` VARCHAR(8) one of `'X1'..'X6'`,
    `customer_name` VARCHAR(120), `customer_email` VARCHAR(160),
    `user_category` VARCHAR(40) one of 6 values (Babies, Teen-age
    Girls, Office-going Women, Everyday Wear, Wedding, Birthday),
    `product_looking_for` VARCHAR(40) one of 5 values (Ear Rings,
    Bangles, Necklace, Finger Rings, Anklets),
    `reason_for_non_purchase` TEXT (free-text), `ground_truth_topic`
    VARCHAR(40) (eval anchor — synthetic; would not exist in
    production).
  - **Enriched columns (4):** added by `02_enrich_topics.py` via
    idempotent `ALTER TABLE ... ADD COLUMN`. `inferred_topic`
    VARCHAR(40), `topic_confidence` FLOAT, `non_purchase_type`
    VARCHAR(40), `attribute_value` VARCHAR(200).
  - **Indexes:** `idx_store` on `store_code`, `idx_product` on
    `product_looking_for`.
- **Table `chat_trace`** — created lazily by `tools/trace_logger.py:
  ensure_table()` on first use via `CREATE TABLE IF NOT EXISTS`.
  Columns: `trace_id` BIGINT PK AUTO_INCREMENT, `created_at`
  DATETIME, `user_message` TEXT, `path` VARCHAR(16) (sql/direct/
  clarify), `resolved_question` TEXT, `final_sql` TEXT, `has_chart`
  BOOLEAN, `has_excel` BOOLEAN, `supervisor_invoked` BOOLEAN,
  `groundedness_warned` BOOLEAN, `step_count` INT, `fail_count` INT,
  `retry_count` INT, `answer_text` MEDIUMTEXT, `steps_json`
  MEDIUMTEXT. Indexed on `created_at`, `path`, `supervisor_invoked`,
  `groundedness_warned`.
- **`data_prep/01_generate_feedback_data_mysql.py`** — one-time seed
  script. Synthesizes ~1,200 verbose-text rows with store-specific
  topic/product biases (see `STORE_BIAS` dict — X1 over-indexes on
  Design+Bangles and Size+Finger Rings, X2 on Stock+EarRings and
  Price+Necklace, etc.). Seeds with `random.seed(42)` for
  reproducibility. Drops + recreates the table on every run.
- **`data_sample/non_purchasers_feedback.csv`** — CSV snapshot of
  the ~1,200 seed rows for offline reproduction or pivot-table
  analysis.

**Design decisions.**

- **InnoDB + utf8mb4** over MyISAM/latin1 because: (a) we want
  transactional inserts in `02_enrich_topics.py` for per-row crash
  safety, (b) the data has Unicode (Indian + Western names, attribute
  values like "₹25k" range labels in production).
- **`non_purchase_type` and `attribute_value` as two columns** instead
  of a single normalized table. Two columns keeps SQL simple for the
  Coder (no joins for common questions). The trade-off is denormalized
  redundancy, acceptable at ~1,200 rows.
- **`ground_truth_topic` column carried in production schema** so
  the offline classifier eval (`03_eval_topic_classifier.py`) can
  measure accuracy in place. In a real production system this column
  wouldn't exist.
- **`chat_trace` schema is denormalized** — one row per turn, full
  step trace as JSON. Trade-off: cheap to write, easy to query the
  common fields, full detail still queryable via MySQL JSON functions.
  Alternative would have been a normalized `chat_steps` table; we
  rejected that as overkill for the volume and to keep persistence
  fail-safe.

**Interfaces.** Read by `tools/sql_tools.py:sql_executor` and
`agent_backend.py:_query_df`. Written by
`tools/trace_logger.py:log_turn` (for `chat_trace`) and
`data_prep/02_enrich_topics.py:update_row` (for enrichment column
updates). The schema itself is duplicated in `tools/sql_tools.py:
SCHEMA_TEXT` so the LLMs can read it from their prompts without
hitting `INFORMATION_SCHEMA`.

---

## Layer 2: Knowledge Layer — what we understand about the data

**What this layer does.** Sits above raw data and provides
structured, enriched, classified knowledge that lets downstream
agents reason at higher abstraction than free text. Run once per
data ingestion; never invoked during chat.

**Components.**

- **`data_prep/02_enrich_topics.py`** — the offline Topic
  Classifier. For each row in `non_purchasers_feedback` where
  `inferred_topic IS NULL`, calls Claude Haiku 4.5
  (`anthropic:claude-haiku-4-5-20251001`) at `temperature=0`,
  `max_tokens=200`. Sends a system prompt (~190 lines, embedded in
  the file) with closed enums + 8 worked examples, and a user
  message containing `product_looking_for` + `reason_for_non_purchase`.
  Output is a JSON object with `inferred_topic`, `topic_confidence`,
  `non_purchase_type`, `attribute_value`. Validated against
  `ALLOWED_TOPICS` (10) and `ALLOWED_NON_PURCHASE_TYPES` (9) before
  insertion. Idempotent + resumable (rows with `inferred_topic IS
  NULL` are picked up; successful rows have `inferred_topic` set
  and are skipped on re-run).
- **Enriched columns** (already listed in Layer 1 but logically
  belong to this layer):
  - `inferred_topic` — one of 10 canonical topics: `Design
    Unavailable`, `Size Unavailable`, `Stock Unavailable`, `Price
    Too High`, `Quality Concerns`, `Weight Concerns`,
    `Color/Finish Mismatch`, `Customization Not Offered`, `Sales
    Service`, `Others`
  - `topic_confidence` — float in [0.0, 1.0]
  - `non_purchase_type` — one of 9: `design`, `size`, `color`,
    `weight`, `price`, `customization`, `service`, `stock`, `none`
  - `attribute_value` — normalized phrase; NULL by design when
    `non_purchase_type IN ('service','stock','none')`
- **Topic → non_purchase_type mapping** — enforced in the
  classifier's prompt. Design Unavailable → "design", Size →
  "size", Color/Finish Mismatch → "color", Weight → "weight",
  Price Too High → "price", Customization Not Offered →
  "customization", Sales Service → "service", Stock Unavailable
  → "stock", Quality Concerns → "none", Others → "none".
- **Normalization rules for `attribute_value`** — also enforced in
  the prompt with 8 worked examples. design: "star design",
  "jhumka", "temple design"; size: "size 8", "10 inch", "kids
  size"; color: "rose gold", "white gold"; weight: "under 8g",
  "around 12g"; price: "under 25k", "below 1 lakh";
  customization: "engraving", "made-to-order"; service/stock/
  none: NULL.
- **`tools/sql_tools.py:SCHEMA_TEXT`** — a triple-quoted Python
  string that is the canonical schema description. Read by
  `get_schema_for_prompt()` and substituted into the Coder and
  Code Reviewer prompts (`{SCHEMA}` placeholder). Single source
  of truth — when the schema changes, this changes; when this
  changes, every dependent prompt picks it up automatically.

**Design decisions.**

- **Offline enrichment, not on-demand.** Doing this once at
  ingestion (vs running the classifier on every chat turn) means
  the chat SQL aggregates against clean structured columns
  (`WHERE non_purchase_type='design'`) instead of pattern-matching
  the free-text `reason_for_non_purchase` (`WHERE
  reason_for_non_purchase LIKE '%design%'`). Massive simplification
  for the downstream Coder.
- **Closed enums enforced both in prompt and in Python validation.**
  Belt-and-braces: the prompt tells the LLM the allowed values; the
  `validate()` function rejects any out-of-enum output. Bad rows are
  left unenriched (`inferred_topic IS NULL`) so the script can
  retry them on re-run.
- **Resumability without a state file.** The "what's unenriched"
  query is `WHERE inferred_topic IS NULL`. Successfully classified
  rows aren't re-processed on re-run. Failure (LLM call error,
  validation failure) leaves the row unenriched and idempotent
  retry works without external state.
- **Per-row commits, not batched.** `update_row()` commits each
  row individually. Bottleneck is the LLM API, not MySQL, so
  per-row commits cost nothing and isolate any crash to a single
  row.
- **`temperature=0` for the classifier** even though it's an LLM —
  same input always produces the same classification, which
  stabilizes re-runs.
- **`max_tokens=200` cap** — the output JSON is tiny (~50-100
  tokens); the cap protects against runaway generations.
- **8 few-shot examples in the prompt** specifically cover:
  design (star design), size (size 8, 10 inch), color (rose gold),
  price (under 25k), customization (engraving), stock (NULL value),
  Others/postponing (NULL value). Each example is the exact format
  the model must produce.

**Interfaces.** Provides the structured enrichment columns the
Reasoning Layer (Coder) writes SQL against. Consumes the Data
Layer via `mysql.connector`. Exposes `SCHEMA_TEXT` to the
Reasoning + Reflection layers via `get_schema_for_prompt()`.

---

## Layer 3: Memory Layer — what we remember

**What this layer does.** Manages conversation state across turns
in a session, and provides the long-term log that lets you analyze
patterns across sessions.

**Components.**

- **`st.session_state.messages`** — short-term in-process chat
  history. Stored as a list of dicts `{"role": "user"|"assistant",
  "content": str, "sql"?, "chart_png"?, "viz_code"?, "excel_bytes"?,
  "steps"?}`. Lives in the user's browser tab; dies when the tab
  closes or the user clicks Clear chat.
- **Planner 8-message look-back** — `agents/planner.py:
  _format_history` returns `history[-8:]` formatted as
  `"role: content"` lines. Documented inline with the rationale
  (reference-resolution payoff drops sharply with depth; planner
  input budget needs to stay small; per-turn cost stays roughly
  flat as session grows).
- **`resolved_question` mechanism** — the Planner produces a
  context-free version of the user's question that bakes in all
  history references ("now show me X4" → "What is the top reason
  at store X4?"). Every downstream agent (Coder, Output Reviewer,
  Writer, Viz Coder) reads `resolved_question`, not the raw user
  message. This is the single point at which history is
  consulted; everything else is stateless on history.
- **`chat_trace` MySQL table** — long-term persistent memory. Every
  turn writes one row via `trace_logger.log_turn`. Schema captures
  enough to reconstruct the turn's path, retry behavior, and full
  step trace; intended for offline pattern analysis (query
  examples below).

**Design decisions.**

- **History resolution happens once, at the Planner.** Downstream
  agents are stateless. This is a deliberate architectural choice
  that prevents history-aware bugs from spreading across the
  pipeline. The trade-off: the Planner is a single point of
  failure for history-related errors (if it mis-resolves "the
  same", no downstream agent can recover). The hierarchical-
  analysis follow-up rule (Case H / Case V) was added to the
  Planner prompt to mitigate the most common failure mode.
- **8 messages, not more, not fewer.** The cap was originally
  hardcoded with no comment; we added a long comment block in
  `planner.py` explaining the four reasons: (a) follow-up
  references almost always point back 1-2 turns; (b) Planner's
  output budget is small (max_tokens=400); (c) per-turn cost
  stays flat instead of O(n²) growing with session length; (d) 8
  is the smallest round number that gives generous coverage of
  the common case.
- **`chat_trace` is fail-safe.** `log_turn` is wrapped in
  try/except at two levels (inside the function and inside the
  Orchestrator's call site). A MySQL connection failure or
  missing column never breaks the chat. The trade-off: turns may
  silently fail to log. Mitigated by the `verify_evals.py` smoke
  test that asserts logging round-trips.
- **Lazy `CREATE TABLE`** — `chat_trace` is created on first
  insert via `CREATE TABLE IF NOT EXISTS`, cached after success
  via a module-level `_TABLE_ENSURED` flag so subsequent turns
  skip the check.
- **Denormalized `steps_json` column** — the full step trace is
  JSON-serialized into a single column rather than a separate
  `chat_steps` table. Trade-off: cheap to write, joins-free to
  query common fields, full detail still queryable via MySQL JSON
  functions (`JSON_EXTRACT`). Normalization could come later if
  trace volume grows.
- **Bool/INT derived fields** (`supervisor_invoked`,
  `groundedness_warned`, etc.) are computed during `log_turn` so
  common filter queries don't have to parse JSON.

**Interfaces.** Read by the Planner (chat history slice). Written
by the Orchestrator after every turn (via `log_turn`). Queryable
by humans via direct SQL against `chat_trace`. Sample queries
documented in `docs/00_system_overview.md` Eval section.

---

## Layer 4: Reasoning Layer — agents that interpret and generate

**What this layer does.** The LLM-driven "brain" of the system.
Five agents that turn intent into output. Reasoning is not the
same as reflection (which sits in Layer 6) — these agents
*produce*, the reflection layer *critiques*.

**Components.**

| Agent | Model | Temp | max_tokens | Cached? | Role |
|---|---|---|---|---|---|
| Planner | Haiku 4.5 | 0.0 | 400 | Yes | Route turn + resolve history |
| Coder | **Sonnet 4.6** | 0.0 | 900 | **No** | Reasoning + SQL generation |
| Writer | Haiku 4.5 | 0.0 | 800 | Yes | Compose markdown answer |
| Viz Coder | Haiku 4.5 | 0.0 | 900 | Yes | Generate matplotlib code |
| Supervisor | Haiku 4.5 | 0.0 | 400 | Yes | Recovery decision (Layer 8) |

(Supervisor is technically Reasoning but operationally Recovery; we
list it in Layer 8.)

**Planner.** `agents/planner.py:plan(user_message, history)`.
Returns `PlannerOutput(path: Literal["sql","clarify","direct"],
plan: str, resolved_question: str)`. Prompt at
`agents/prompts/planner.txt` (~150 lines). Critical prompt
sections: (a) the four-case attribute interpretation decision
tree; (b) the multi-turn hierarchical-analysis Case H / Case V
rule (added after the X3 → X1/X2 failure mode); (c) the
colloquial-vs-literal vocabulary mapping (stock more / what's
missing → aggregate `attribute_value`, not filter
`inferred_topic='Stock Unavailable'`), including the
product-dimension rule: when no specific product is named in the
question, the resolved_question must explicitly call for a
breakdown by BOTH `product_looking_for` AND `attribute_value`
(otherwise stock decisions cannot be acted on per-product).
Validated against `ALLOWED_PATHS = {"sql","clarify","direct"}`.

**Coder.** `agents/coder.py:write_sql(resolved_question,
feedback, previous_sql)`. Returns raw text containing both a
`<reasoning>...</reasoning>` block (four named lines: Dimensions,
Filters, Aggregation, Completeness) and a `<sql_query>...</sql_query>`
block. Prompt at `agents/prompts/coder.txt` (~330 lines) — the
largest prompt in the system. Critical sections: (a) the
mandatory `<reasoning>` block requirement at the top; (b) the
preamble forcing `non_purchase_type` for the word "attributes";
(c) the LIMIT rule (NEVER on aggregations unless user says "top
N"); (d) the four-step attribute interpretation decision tree;
(e) the dimensions rule (compound questions must include every
named dimension in GROUP BY); (f) 7 worked examples including the
Example 7 nested-CTE discovery-then-drilldown case. Schema text
(`{SCHEMA}`) substituted at every call (no cache — see Design
Decisions).

**Writer.** `agents/writer.py:write_answer(user_question, rows,
columns, path, plan, caveat, previous_text, groundedness_feedback,
safe_values)`. Returns markdown text. Prompt at
`agents/prompts/writer.txt`. Critical sections: (a) UI-affordance
prohibition (forbids referencing the Excel button, Show SQL
expander, etc. — the Streamlit frontend renders those
automatically); (b) the groundedness constraint section listing
the 6 safe derivation patterns; (c) instruction to pick from the
injected `## Safe values for grounding` section when present. The
Writer at this temperature + with safe-values injection rarely
needs to compute arithmetic on its own.

**Viz Coder.** `agents/viz_coder.py:write_viz_code(
resolved_question, rows, columns, feedback, previous_code)`.
Returns raw text containing a `<viz_code>...</viz_code>` block
with matplotlib code. Prompt at `agents/prompts/viz_coder.txt`.
Constraints: code receives `df`, `rows`, `columns`, `plt`, `pd`,
`np` pre-loaded in scope; must set `fig`; no `import` statements
allowed (the sandbox blocks them); `__builtins__` is a curated
safe whitelist.

**Support helper — `safe_derivations_summary`.** Lives in
`tools/groundedness.py` (Layer 5 / 7). Computes every legitimate
numeric value the Writer can cite given the result rows, formats
as a Markdown section, and is injected into the Writer's user
block by the Orchestrator. Lists: individual cell values; numbers
embedded in string labels (e.g. "10 inch"); per-column grand
totals; per-row column percentages; per-group subtotals (sum of a
numeric column for each distinct value of a categorical column);
per-group row counts; top-N partial sums for N ∈ {2..5}. The
Writer picks from this list instead of computing on its own —
this is the single biggest contributor to first-attempt
groundedness pass rate.

**Design decisions.**

- **Why Sonnet 4.6 for Coder + Code Reviewer specifically.** The
  SQL-quality bottleneck. Empirical observation: Haiku consistently
  failed on MySQL window-function syntax (5 retries to land
  `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY n DESC)` via a
  subquery rather than referencing `COUNT()` directly), and on the
  `non_purchase_type` vs `attribute_value` disambiguation despite
  explicit prompt scaffolding. Sonnet handles both reliably on the
  first attempt. The Code Reviewer must be at the same competence
  tier as the Coder it reviews; a weaker reviewer can't catch a
  stronger coder's mistakes.
- **Why everyone else stays on Haiku.** The Planner does routing
  (a simpler task than SQL); the Writer composes prose from
  pre-computed safe values (no arithmetic burden); the Output
  Reviewer judges semantic fit from a 30-row preview; the Viz
  pipeline writes short matplotlib snippets. Haiku is plenty for
  these. Upgrading them would multiply cost 3-5x for marginal
  quality gain.
- **`max_tokens=900` on the Coder** — bumped from the original 600
  to accommodate the `<reasoning>` block (~200 tokens) before the
  SQL itself. Reasoning verification by the Code Reviewer is
  central to the system's quality; the token budget reflects that.
- **`max_tokens=400` on the Code Reviewer** — bumped from 300 for
  the same reason; the reviewer's feedback may need to quote
  reasoning-vs-SQL contradictions explicitly.
- **Coder and Code Reviewer prompts are NOT cached** in module-
  level variables — they re-read from disk on every call. The
  rationale (documented in `agents/coder.py:_system_prompt()`):
  Streamlit's hot-reload reloads `.py` files but doesn't re-import
  modules, so a cached prompt string persists across `.txt` edits.
  For prompt-engineering iteration speed, edit-and-refresh beats
  the few-hundred-microsecond saving of caching. The other 7
  agents still cache their prompts because their prompts change
  less often.
- **The `<reasoning>` block requirement** — added after observing
  that Sonnet would still occasionally pick wrong columns or wrong
  filters on compound questions. Chain-of-thought forces
  compositional commitment before SQL: the Coder must explicitly
  state Dimensions / Filters / Aggregation / Completeness, and the
  Code Reviewer can then verify the SQL actually implements the
  stated reasoning (catches the "reasoning says NO LIMIT but SQL
  has LIMIT 30" failure mode).
- **The `safe_derivations_summary` injection** — added after the
  Writer at temp 0.2 hallucinated numbers despite the broadened
  groundedness candidate set. With safe-values explicitly listed
  in the user block, the Writer picks rather than computes. This
  + dropping Writer temp from 0.2 to 0.0 + Sonnet for Coder /
  Code Reviewer made the same-question-twice determinism issue
  vanish in practice.

**Interfaces.** Reads `resolved_question` from Memory Layer.
Writes raw model output to Tool Layer (via `sql_parser`,
`viz_code_parser`). Read by Reflection Layer (reviewers see the
Coder's reasoning + SQL). Configured by `agents/llm.py` (centralized
LLM client).

---

## Layer 5: Tool Layer — deterministic actions

**What this layer does.** Pure / deterministic Python helpers
that the Orchestrator calls. No LLMs. Each tool has a single,
well-defined responsibility and is unit-testable in isolation.

**Components.** All re-exported from `tools/__init__.py`.

- **`sql_parser(text) -> ParseResult`** — extracts SQL from
  `<sql_query>...</sql_query>` tags via
  `re.compile(r"<sql_query>\s*(.*?)\s*</sql_query>", re.DOTALL |
  re.IGNORECASE)`. Also extracts the optional `<reasoning>` block
  via `_REASONING_RE`. Returns `ParseResult(ok, sql, reason,
  reasoning)`. Falls back to bare-SELECT detection if tags
  missing.
- **`sql_safety_guard(sql) -> SafetyResult`** — enforces
  SELECT-only by regex. Checks: starts with `SELECT` or `WITH`
  (lookahead); no semicolon followed by non-whitespace (blocks
  multi-statement); no banned keyword from `_BANNED` (compiled
  regex matching `insert|update|delete|drop|alter|truncate|
  rename|create|grant|revoke|replace|merge|call|execute|exec|
  load|outfile|into\s+outfile`).
- **`sql_executor(sql, row_limit=500) -> dict`** — opens MySQL
  connection per call (no pool — single-user assumption), runs
  the SQL via `cursor.execute()` with `dictionary=True`, fetches
  up to 500 rows. Returns `{ok: True, columns, rows, row_count,
  truncated}` on success or `{ok: False, error}` on failure.
- **`viz_code_parser(text) -> ParseResult`** — extracts Python
  from `<viz_code>...</viz_code>` tags. Strips an optional ` ```python `
  fence if present. Reuses `ParseResult` dataclass (the `sql`
  field carries the code payload, legacy naming).
- **`viz_generator(viz_code, rows, columns) -> dict`** — sandboxed
  `exec()`. Sets `matplotlib.use("Agg")` (headless backend).
  Builds a `safe_builtins` dict containing only `len`, `range`,
  `enumerate`, `zip`, `sum`, `min`, `max`, `abs`, `round`,
  `sorted`, `reversed`, `list`, `dict`, `set`, `tuple`, `str`,
  `int`, `float`, `bool`, `isinstance`, `print`, and the literals
  `True/False/None`. The code's `__builtins__` is replaced with
  this dict, so `open`, `exec`, `eval`, `__import__`, `input` are
  inaccessible. Pre-builds `df = pd.DataFrame(rows,
  columns=columns)` and exposes `plt`, `pd`, `np`. Calls
  `exec(viz_code, globals_dict, locals_dict)`. Returns
  `{ok: True, png: bytes}` from `fig.savefig(..., format='png',
  dpi=120, bbox_inches='tight')` or `{ok: False, error: str}`.
- **`png_to_base64(png_bytes) -> str`** — base64 encoding helper.
  Currently unused — `call_llm_vision` does its own encoding
  internally. Retained for audit logging or future use.
- **`to_excel(rows, columns, sheet_name='Result') -> bytes`** —
  builds an openpyxl-backed `.xlsx` workbook in memory via
  `pd.ExcelWriter(buf, engine='openpyxl')`. Returns the bytes.
  On failure (e.g., openpyxl missing or version mismatch) returns
  `b""` AND populates `LAST_TO_EXCEL_ERROR` (module-level
  diagnostic string) with the exception type + message, plus
  prints to stderr.
- **`groundedness_check(text, rows, columns, tolerance=0.5) ->
  GroundednessResult`** — see Layer 7 (Validation) for detail.
- **`safe_derivations_summary(rows, columns,
  max_groups_per_column=8) -> str`** — see Layer 4 (Reasoning)
  for detail. Generates the Markdown section injected into the
  Writer's user block.
- **`log_turn(user_message, resp) -> None`** and
  **`ensure_table() -> bool`** — see Layer 10 (Observability).
- **`SCHEMA_TEXT`** and **`get_schema_for_prompt()`** — see
  Layer 2 (Knowledge).

**Design decisions.**

- **Every tool is deterministic.** No LLM calls inside the Tool
  Layer. This is the floor of predictability: tools either do
  what they say or return a structured error. This makes the
  system auditable.
- **No connection pooling for MySQL.** The system assumes single-
  user. `sql_executor` opens a connection per call. Trade-off:
  small per-call latency overhead (~10-30ms) in exchange for no
  pool management. Connection-pool helpers would slot in cleanly
  at a future multi-user point.
- **Row limit at 500 in `sql_executor`.** Caps memory + writer
  context size. The Writer only sees the first 30 rows anyway
  (Layer 4); 500 is generous for safety + still bounded.
- **Sandbox with restricted `__builtins__`, not subprocess
  isolation.** Trade-off: 100x faster than subprocess (matplotlib
  startup is the slow part). Reasonable for single-user dev.
  Documented as NOT production-grade in `viz_tools.py`. For
  multi-tenant deployment, replace with subprocess + resource
  limits.
- **`LAST_TO_EXCEL_ERROR` module-level diagnostic.** Surfaces tool
  failure cause into the Orchestrator's step trace. Replaced the
  earlier silent-swallow design where a missing openpyxl caused
  the download button to silently disappear with no diagnostic.
- **The `viz_code_parser` reuses `ParseResult.sql` field for code
  payload** — legacy from when this was a `parse_tagged_content`
  helper. Annotated in the code comment but not refactored
  because the cost is zero and changing it risks breaking the
  Orchestrator's call site.

**Interfaces.** All tools are called only by the Orchestrator
(Layer 9). No tool calls another tool directly (except internal
helpers like `_attach_excel` calling `to_excel`). No LLM agent
calls any tool directly. The Orchestrator is the only hub.

---

## Layer 6: Reflection Layer — agents that critique other agents

**What this layer does.** LLM-based cross-checking. Each
reflection agent is paired with a reasoning agent and exists to
catch its mistakes. They are themselves LLMs and can be wrong, but
they catch the bulk of subtle errors the deterministic Validation
Layer can't see.

**Components.**

| Reviewer | Model | Reviews | Verdict enum |
|---|---|---|---|
| Code Reviewer | **Sonnet 4.6** | Coder's SQL + reasoning | `{ok, retry}` |
| Output Reviewer | Haiku 4.5 | Execution result vs question | `{ok, retry}` (+ `viz_applies: bool`) |
| Viz Code Reviewer | Haiku 4.5 | Viz Coder's matplotlib code (static) | `{ok, retry}` |
| Viz Reviewer | Haiku 4.5 (vision) | Rendered chart PNG (multimodal) | `{ok, revise, drop}` |

**Code Reviewer.** `agents/code_reviewer.py:review(user_question,
sql, execution_error=None, reasoning=None)`. Sonnet 4.6 at temp 0,
max_tokens 400. Two call patterns: (a) static review — called
right after the Coder produces SQL, no `execution_error`; (b)
post-error review — called after `sql_executor` returns
`{ok: False, error}`, with the error string passed in. Both use
the same prompt. The reasoning verification rule was added in a
recent iteration: when the Coder's `<reasoning>` block is
provided, the reviewer must verify the SQL actually implements
that reasoning. Common contradictions to flag (enumerated in the
prompt with two worked examples): reasoning says N dimensions but
GROUP BY has fewer; reasoning says NO LIMIT but SQL has LIMIT;
reasoning requires a filter that the SQL omits; reasoning says
group by X but SQL doesn't include X. Prompt re-read per call
(no cache).

**Output Reviewer.** `agents/output_reviewer.py:review(
resolved_question, sql, rows, columns, original_user_message)`.
Haiku 4.5 at temp 0, max_tokens 300. Sees: the resolved
question, the original user message (if different — Planner-
specific resolution might have changed intent), the SQL, and the
first 30 rows of the result as JSON. Two distinct jobs: (a)
verdict = `ok` if the rows semantically answer the question,
otherwise `retry` with feedback for the Coder; (b)
`viz_applies: bool` — does the result shape warrant a chart?
This drives the entire viz sub-pipeline (Layer 4 viz path
activates only when `viz_applies=True` and `row_count>0`).

**Viz Code Reviewer.** `agents/viz_code_reviewer.py:review(
viz_code, resolved_question)`. Haiku 4.5 at temp 0, max_tokens
250. Static review of matplotlib code BEFORE the sandbox runs
it. Checks include: no banned identifiers (`open`, `exec`,
`eval`, `__import__`, `os`, `subprocess`, `requests`, `urllib`,
`shutil`, `sys`, `getattr`, `setattr`, `globals`, `locals`, `dir`,
`__`, `file`, `input`); has axis labels; uses `df` correctly
given the result shape; sets `fig`; matches the resolved
question's intent (comparison vs ranking vs distribution).

**Viz Reviewer.** `agents/viz_reviewer.py:review(image_png,
resolved_question)`. Haiku 4.5 vision via the native
`anthropic` SDK (aisuite's multimodal handling for Anthropic is
patchy; the native SDK path is used in `agents/llm.py:
call_llm_vision_json`). Temp 0, max_tokens 300. Multimodal: sees
the actual rendered PNG + the resolved question. Three verdicts:
`ok` (ship the chart), `revise` (one retry round; passes the
verbal feedback to the Viz Coder), `drop` (terminal — chart
abandoned, text answer still ships).

**Design decisions.**

- **Each reviewer is enum-constrained.** Out-of-enum verdicts
  raise (caught by the Orchestrator as a transient failure).
  Forces the model into a binary or trinary decision rather than
  generating prose that needs further parsing.
- **Code Reviewer at Sonnet, others at Haiku.** Two reasons: (a)
  the Coder is at Sonnet, so the reviewer needs to be at the
  same tier to catch its mistakes (a Haiku reviewer can't reliably
  catch a Sonnet coder's subtle errors); (b) the other reviewers
  do narrower tasks (semantic-fit binary, static-syntax binary,
  multimodal layout judgement) where Haiku is sufficient.
- **Viz Reviewer is the only multimodal agent.** Uses the native
  Anthropic SDK because aisuite's multimodal path is unreliable
  for Anthropic-hosted models. PNG is base64-encoded inside
  `call_llm_vision_json` and sent as a content block of type
  `image`.
- **The Output Reviewer also decides `viz_applies`.** This double
  role is intentional — the viz decision needs the same semantic
  understanding of the rows that the semantic-fit check needs, so
  doing both in one call is cheaper than splitting.
- **Reasoning verification is the Code Reviewer's most important
  job.** Before chain-of-thought scaffolding was added, the
  reviewer caught straightforward errors (wrong column, typo).
  Now it catches the most damaging class of failure: SQL that
  *looks plausible* but doesn't match the Coder's own stated
  intent. Two worked examples in the prompt show the LIMIT-
  mismatch and missing-dimension cases.

**Interfaces.** Reads from Reasoning Layer outputs (Coder's
text, Viz Coder's text, executor's rows, viz_generator's PNG).
Writes verdict + feedback back to the Orchestrator (Layer 9),
which decides whether to retry the upstream agent.

---

## Layer 7: Validation Layer — deterministic guards

**What this layer does.** Non-LLM checks. The floor of correctness
that survives even if every LLM agent is compromised. These exist
to catch the narrow but most-dangerous class of failures.

**Components.**

- **`sql_safety_guard`** — already in Layer 5; conceptually also
  here. Enforces SELECT-only, blocks banned-keyword list, rejects
  multi-statement. This is the only barrier between an LLM-
  generated SQL string and the database; it cannot be social-
  engineered because it's regex, not an LLM.
- **`viz_generator` sandbox** — already in Layer 5; the restricted
  `__builtins__` dict prevents arbitrary file I/O, subprocess
  spawning, network calls, or module loading from the viz code.
- **`groundedness_check`** — `tools/groundedness.py`. Deterministic
  regex extraction of every numeric token in the Writer's prose,
  matched against a candidate set built from the result rows. The
  candidate set includes (numbered as in `_candidate_values`):
  1. Every numeric cell value in the result.
  2. Numbers embedded in string cell values (handles labels like
     "10 inch", "size 8", "under 25k").
  3. Per-column grand totals.
  4. Each row's percentage of its column total (with rounded
     variants for tolerance).
  5. Per-group subtotals (sum of a numeric column for each
     distinct value of a categorical column, computed per
     categorical × numeric pair).
  6. Per-group row counts.
  7. Top-N partial sums for N ∈ {2, 3, 4, 5} of each numeric
     column sorted descending.
  Numeric extraction regex: `\b(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?
  |\d+(?:\.\d+)?)(?:\s*%)?(?!\w)`. The `(?!\w)` lookahead is
  critical — it prevents extracting `25` from `25k` (a label
  with a unit suffix). Tolerance: 0.5 absolute units handles
  common rounding (e.g., 23.5% ≈ 24%). Numbers below 2.0 are
  ignored (`_IGNORE_BELOW` constant) to avoid noise from years,
  IDs, etc.
- **Enum validators (cross-layer).**
  - `agents/planner.py:ALLOWED_PATHS = {"sql","clarify","direct"}`
  - `agents/code_reviewer.py:ALLOWED_VERDICTS = {"ok","retry"}`
  - `agents/output_reviewer.py:ALLOWED_VERDICTS = {"ok","retry"}`
  - `agents/viz_code_reviewer.py:ALLOWED = {"ok","retry"}`
  - `agents/viz_reviewer.py:ALLOWED = {"ok","revise","drop"}`
  - `agents/supervisor.py:ALLOWED_ACTIONS = {"abort_gracefully",
    "retry_with_strategy","ship_partial","ask_user"}`
- **`02_enrich_topics.validate()`** — closed-enum + range checks
  on Topic Classifier output. Rejects rows where `inferred_topic`
  is outside `ALLOWED_TOPICS`, `non_purchase_type` is outside
  `ALLOWED_NON_PURCHASE_TYPES`, `topic_confidence` is outside
  [0, 1], or `attribute_value` is neither string nor null.

**Design decisions.**

- **Regex over LLM-as-judge for groundedness.** LLM-as-judge is
  more powerful but introduces a second source of LLM error (the
  judge can be wrong about whether the writer was right). Regex
  is deterministic, free, and verifiable. Trade-off: regex can't
  judge attribution ("Necklace dominates with 41" passes if 41 is
  in the candidate set, even if 41 is actually Bangles' count) —
  this is acknowledged in the limitations.
- **Broadened candidate set vs strict check.** Earlier versions
  rejected any number not directly in a cell. After observing
  legitimate Writer compositional patterns (citing group sums,
  top-N partial sums) being false-flagged, the candidate set was
  expanded to 7 derivation patterns. This reduces false-negative
  Writer rejections without significantly weakening the floor —
  a hallucinated number still has to land on one of the
  derivation patterns by coincidence, which is rare for counts in
  the tens-to-hundreds range.
- **Hard-block, not soft-block.** When groundedness fails after 2
  retries, the system ships `GROUNDEDNESS_FAIL_TEXT` instead of
  the unverified text. Original implementation was non-blocking
  (warning in trace, text ships anyway); changed to hard-block
  per user requirement that wrong numbers should never reach the
  user. Trade-off: occasionally an answer with legitimately-
  derived numbers gets blocked due to false-negative candidate
  set; the user is directed to inspect the data via Show SQL /
  Download Excel. The user-named-not-system-named constant ensures
  this fallback text doesn't mention specific UI elements that
  may or may not be present.
- **Sandbox blocks `__import__` specifically.** This is the most
  common Python escape vector. Combined with the restricted
  `__builtins__`, even calls like `getattr(__builtins__,
  '__import__')` fail because `__builtins__` itself isn't the
  full set.

**Interfaces.** Called by the Orchestrator (Layer 9) at the
relevant control-flow points: `sql_safety_guard` before
`sql_executor`; `groundedness_check` after the Writer (inside
`_write_grounded_answer`); enum validators inside each agent's
parsing logic.

---

## Layer 8: Recovery Layer — what happens when things fail

**What this layer does.** Graceful degradation, fallback flows,
and escape hatches. Lets the system ship *something* useful even
when individual agents or tools fail.

**Components.**

- **Retry loops with budgets** (constants in `orchestrator.py`):
  - `MAX_CODE_REVIEW_RETRIES = 5` — inner Coder ↔ Code Reviewer
    loop. The Coder re-runs with the reviewer's feedback as
    `feedback` and the previous SQL as `previous_sql`. Each
    iteration: Coder writes → parse → safety guard → static
    review → execute → post-error review (if execute failed).
    Exhaustion → Supervisor.
  - `MAX_OUTPUT_REVIEW_RETRIES = 2` — outer Output Reviewer loop.
    Wraps the inner Coder loop. If Output Reviewer says `retry`,
    the inner loop runs again with the reviewer's feedback seeded
    into the Coder. Exhaustion → Supervisor.
  - `MAX_GROUNDEDNESS_RETRIES = 2` — Writer ↔ groundedness loop
    inside `_write_grounded_answer`. The Writer rewrites with the
    unmatched-numbers feedback. Exhaustion → ship
    `GROUNDEDNESS_FAIL_TEXT`. **NO Supervisor escalation from
    this loop.**
  - `MAX_VIZ_CODE_RETRIES = 2` — Viz Coder ↔ Viz Code Reviewer
    static loop. Exhaustion → drop chart, text answer ships
    without it. **NO Supervisor escalation.**
  - `MAX_VIZ_REVISE = 1` — Viz Reviewer revise round. After the
    multimodal review of the rendered PNG. Exhaustion → ship the
    last PNG, or drop entirely if Viz Reviewer's last verdict
    was `drop`.
- **Supervisor agent** — `agents/supervisor.py:decide(
  user_question, trace)`. Haiku 4.5 at temp 0, max_tokens 400.
  Sees: the original user message + the full step trace as a list
  of dicts. Returns `SupervisorDecision(action, message,
  strategy_hint)` where action is one of four:
  - `abort_gracefully` — ship the supervisor's message to the
    user, no further pipeline activity.
  - `retry_with_strategy` — the Orchestrator runs the Coder ONE
    more time with `feedback=f"SUPERVISOR STRATEGY:
    {decision.strategy_hint}"`. No further retry budget. If this
    final attempt fails, the Supervisor's message is shipped.
  - `ship_partial` — the Writer is invoked on the last successful
    SQL result with `caveat=decision.message` prepended to the
    answer. The Writer still passes through the groundedness loop.
  - `ask_user` — the supervisor's message is returned as a
    clarifying question.
  If the Supervisor returns an invalid action, the Orchestrator
  fail-safes to `ship_partial` with a generic caveat.
- **`GROUNDEDNESS_FAIL_TEXT`** — module-level constant in
  `orchestrator.py`. The exact text: *"I couldn't produce an
  answer with verifiable numbers after 2 attempts — every draft I
  wrote cited values that don't appear in the underlying data.
  The SQL I ran and the rows it returned are surfaced under this
  message when available; please inspect the data directly to
  answer this question."* Deliberately written to NOT reference
  specific UI elements (no "Excel button" or "Show SQL panel")
  because those may or may not be present depending on whether
  rows / chart / Excel succeeded.
- **Fail-safe trace logging.** `trace_logger.log_turn` is wrapped
  in try/except internally; the Orchestrator additionally wraps
  the call in `try/except Exception` (defense-in-depth). A MySQL
  outage cannot break the chat — the worst case is a turn is
  silently un-logged.
- **`viz_generator` failure handling** — on sandbox exec failure
  returns `{ok: False, error: str}` with the exception type and
  message. The Orchestrator surfaces this in the trace, feeds the
  error back to the Viz Coder as feedback, and counts it toward
  the viz code retry budget.
- **`LAST_TO_EXCEL_ERROR` diagnostic** — replaces the historical
  silent-swallow behavior of `to_excel`. When openpyxl is missing
  or the workbook write fails, the Orchestrator emits a
  `to_excel · fail · ...` step event with the actual error message
  visible in the trace.

**Design decisions.**

- **Different loops escalate to different fallbacks.** The Coder /
  Output Reviewer loops escalate to Supervisor because SQL
  failures often need a strategy change. The Writer / Viz loops
  escalate to fixed fallbacks (failure text / chart drop) because
  the failure mode is local and a strategy change wouldn't help.
- **The Supervisor sees the FULL trace**, not just the failing
  agent's output. This lets it reason about *patterns* — e.g.,
  "the Coder kept hitting the same window-function syntax error;
  the strategy should sidestep window functions."
- **Why no Supervisor for Writer/Viz loops.** Writer failure
  means "I can't make verifiable prose from these rows" — there's
  no alternative SQL strategy that would help. Viz failure means
  "the chart can't be rendered" — the text answer is still
  useful, dropping the chart is the right call. Adding Supervisor
  to these loops would add cost without benefit.
- **`retry_with_strategy` has no retry budget.** It's a one-shot.
  If the Supervisor's hint doesn't work, the Supervisor's
  fallback message is shipped. This bounds worst-case cost per
  turn.
- **The Supervisor's prompt is informational** — it doesn't
  enforce strict heuristics for which action to pick. The action
  enum is enforced by Python validation; the *choice* of action
  is delegated to the Supervisor's judgment given the trace.

**Interfaces.** Activated by the Orchestrator at retry-budget
exhaustion points. The Supervisor reads from the Memory Layer
(via the trace it receives). All recovery paths converge back to
producing an `AgentResponse` for the Interface Layer.

---

## Layer 9: Orchestration Layer — the control plane

**What this layer does.** The conductor. Wires every other layer
together. Pure Python, no LLMs in the decision logic — every
control-flow decision is hardcoded.

**Components.**

- **`agents/orchestrator.py`** — the module. ~530 lines. The
  entire chat-turn lifecycle lives here.
- **Public entry: `run(user_message, history) -> AgentResponse`** —
  wraps `_run_impl` and persists the turn to `chat_trace` via
  `log_turn`. Logging failure is swallowed.
- **`_run_impl(user_message, history)`** — the actual flow. Calls
  Planner first; routes to `clarify`, `direct`, or `_run_sql_path`
  based on the result.
- **`_run_sql_path(original_user_message, resolved_question,
  steps)`** — outer Output Reviewer loop (max 2). Calls
  `_inner_sql_loop`, then runs the Output Reviewer, then either
  calls `_finalize_sql_answer` (on `verdict='ok'`) or loops with
  feedback (on `verdict='retry'`), or calls `_supervisor_fallback`
  on exhaustion.
- **`_inner_sql_loop(resolved_question, steps, seed_feedback,
  seed_previous_sql)`** — inner Coder ↔ Code Reviewer ↔ executor
  loop (max 5 attempts). Returns `(sql_text, result)` on success
  or `(None, None)` on exhaustion.
- **`_finalize_sql_answer(resolved_question, sql_text, result,
  viz_applies, steps)`** — runs the Writer (via
  `_write_grounded_answer`), runs the viz pipeline if applicable,
  attaches Excel, returns `AgentResponse`.
- **`_run_viz_pipeline(resolved_question, result, steps)`** —
  nested two-loop structure. Outer Viz Reviewer revise loop
  (max 1) wraps inner Viz Coder ↔ Viz Code Reviewer static loop
  (max 2). Returns `(png_bytes, viz_code)` or `(None, None)` on
  failure.
- **`_supervisor_fallback(user_message, steps, last_sql,
  last_result, resolved)`** — dispatches on the Supervisor's
  chosen action. Implements the `ship_partial` and
  `retry_with_strategy` branches inline (both of which also
  invoke `_write_grounded_answer` and `_attach_excel`).
- **`_write_grounded_answer(resolved_question, rows, columns,
  steps, caveat)`** — the hard-block Writer ↔ groundedness loop
  helper. Called from `_finalize_sql_answer` and both Supervisor
  recovery branches. Returns `(text, grounded: bool)`. On
  `grounded=False`, the caller is expected to substitute
  `GROUNDEDNESS_FAIL_TEXT`.
- **`_attach_excel(result, steps)`** — Excel-attachment helper.
  Returns `bytes | None`. Called from all three SQL-path return
  sites (happy path + both Supervisor branches) so every grounded
  answer ships with the same data-download affordance.
- **`_step(steps, agent, status, summary, **detail)`** — append a
  `StepEvent` to the running trace.
- **`AgentResponse` dataclass** — `text`, `sql`, `chart_png`,
  `viz_code`, `excel_bytes`, `steps`. The contract returned to
  `agent_backend.chat_with_agent` and rendered by Streamlit.
- **`StepEvent` dataclass** — `agent`, `status`
  (`{start,ok,retry,fail}`), `summary`, `detail` (dict). The
  unit of the trace; rendered in the "How I got this answer"
  expander.

**Design decisions.**

- **Rule-based, not LLM-driven.** This is the most important
  architectural choice in the whole system. See the MCP note
  below.
- **Single hub.** Every agent and every tool is called by the
  Orchestrator. No agent calls another agent; no agent calls a
  tool directly. This makes the control flow exhaustively visible
  in one file.
- **Helpers extracted for the three Writer call sites.** Without
  the `_write_grounded_answer` and `_attach_excel` helpers, the
  hard-block grounding loop and the Excel attachment had to be
  duplicated at three places (happy path + ship_partial +
  retry_with_strategy). Earlier versions had this duplication;
  recovery branches silently dropped the Excel button. The
  helpers ensure every SQL-path answer has identical affordances.
- **Step events emit on every transition.** This means even a
  successful turn produces 12+ step events visible in the trace.
  Verbose but the right call: the trace is the primary debugging
  surface, so granular events make root-causing easy.

### Note: no MCP layer (by design)

Unlike fully autonomous agentic systems where the LLM itself
selects tools via the **Model Context Protocol (MCP)** or
function-calling APIs, this project deliberately does NOT use
that pattern. Tools are not exposed as MCP servers, the LLM
agents do not emit tool-use requests, and there is no MCP
client connection anywhere in the codebase.

Instead, the system uses the **rule-based Orchestrator pattern**:

| | MCP-style autonomous agent | This system |
|---|---|---|
| Who chooses which tool to call? | The LLM emits `tool_use` requests | The Orchestrator's Python control flow |
| What does the LLM produce? | Tool calls + final answers | Structured text (`<sql_query>`, `<reasoning>`, `<viz_code>`, JSON verdicts, prose) |
| Who runs the tool? | An MCP server or function-call handler | The Orchestrator: `sql_executor(sql)`, `groundedness_check(...)`, etc. |
| Control flow | Determined turn-by-turn by the LLM | Pre-baked into `_inner_sql_loop`, `_run_sql_path`, `_finalize_sql_answer`, `_run_viz_pipeline`, `_supervisor_fallback` |
| Failure handling | LLM recovers within its tool-use loop | Orchestrator has hardcoded retry budgets, hard-blocks, Supervisor escape hatch |

The Coder is just an LLM that emits SQL text wrapped in tags. It
has no awareness that `sql_safety_guard` and `sql_executor`
exist. The Orchestrator pulls the SQL out, runs it through the
safety guard, executes it, and feeds the results to the next
agent. The Coder *never decides* "I'll call sql_executor next" —
that decision is hardcoded.

**Why the non-MCP pattern was chosen.**

1. **Predictability** for a domain-specific task. The control flow
   is the same every turn (Planner → Coder ↔ Code Reviewer →
   executor → Output Reviewer → Writer/Viz). Letting an LLM
   decide tool sequencing turn-by-turn would add non-determinism
   without obvious benefit.
2. **Reliability of retry budgets.** All retry semantics are
   enforced by Python. In an MCP-style system, the LLM would
   have to "decide" to retry, which is brittle.
3. **Easier validation.** Every tool call goes through the same
   control-flow point, so the trace, the `chat_trace` log, and
   the deterministic guards all attach cleanly to specific
   points.
4. **Cheaper.** No tool-use round trips; each agent makes one LLM
   call and the Orchestrator handles the wiring.
5. **Easier to debug.** When something goes wrong, you look at
   `_inner_sql_loop` and `_finalize_sql_answer` — actual Python
   control flow. With MCP, you'd trace through the LLM's tool-
   use decisions.

**If MCP were to be added later** (not currently planned), three
options exist with increasing scope: (1) tools as MCP servers
exposing the same Python implementations externally — for use by
other Claude clients (Claude Desktop, Claude Code) — without
changing internal control flow; (2) replace the Orchestrator with
an MCP-aware single agent, collapsing Planner/Coder/Reviewers
into one autonomous loop (substantial rewrite, much more
autonomy, much less determinism); (3) hybrid — keep the
Orchestrator for the core SQL path, expose some tools via MCP
for user-defined extensions.

For this project's goals (reliable internal merchandising chat),
option (1) might be worth doing for external reusability but
doesn't change the architecture. Options (2) and (3) would be
substantial rewrites.

**Interfaces.** Reads from Reasoning, Tool, Reflection, and
Validation layers. Writes to Observability and Interface layers.
Itself called by `agent_backend.chat_with_agent` (Interface
Layer).

---

## Layer 10: Observability Layer — what we record

**What this layer does.** The audit trail. Every turn produces a
detailed step trace; the trace persists to MySQL for offline
analysis; the user sees a curated subset in the UI.

**Components.**

- **`StepEvent` dataclass** — `agent: str`, `status: Literal[
  "start","ok","retry","fail"]`, `summary: str`, `detail: dict`.
  The unit of the trace. Built up across the turn by every
  `_step()` call in the Orchestrator.
- **`AgentResponse.steps`** — the list of `StepEvent` for the
  turn. Carried back to Streamlit and rendered in the "How I got
  this answer" expander.
- **`tools/trace_logger.py:log_turn(user_message, resp)`** —
  inserts one row into `chat_trace`. Derived fields extracted
  from `resp.steps` via `_extract_meta`: `path`,
  `resolved_question` (from the Planner step's `detail.resolved`),
  `supervisor_invoked` (presence of a `supervisor` step),
  `groundedness_warned` (presence of a `groundedness · fail`
  step), `fail_count` (count of `status='fail'` events),
  `retry_count` (count of `status='retry'` events). Full step
  trace JSON-serialized in `steps_json` (with `MEDIUMTEXT` column
  to handle long traces).
- **`tools/trace_logger.py:ensure_table()`** — idempotent table
  creation via `CREATE TABLE IF NOT EXISTS chat_trace`. Cached
  via module-level `_TABLE_ENSURED` flag so subsequent calls
  short-circuit.
- **Streamlit rendering** in `streamlit_app.py` — the chat-view
  loop reads `msg.get("steps")` for each assistant message and
  renders the trace in a `st.expander("How I got this answer
  (agent trace)")`. The trace is also surfaced in two other
  expanders: `Show SQL` and `Show chart code`.
- **`LAST_TO_EXCEL_ERROR` propagation** — `_attach_excel` reads
  the module-level diagnostic and emits a `to_excel · fail · ...`
  step when bytes are empty, so failures surface in the trace
  rather than being silent.

**Example offline queries** (against `chat_trace`):

```sql
-- How often does the Supervisor fire?
SELECT COUNT(*) FROM chat_trace WHERE supervisor_invoked = TRUE;

-- Average retry count per path
SELECT path, AVG(retry_count), AVG(fail_count), COUNT(*)
  FROM chat_trace GROUP BY path;

-- Turns where the groundedness check flagged a warning
SELECT trace_id, created_at, user_message, final_sql, answer_text
  FROM chat_trace WHERE groundedness_warned = TRUE
  ORDER BY created_at DESC;

-- Most expensive turns (high fail or retry count)
SELECT trace_id, fail_count, step_count, user_message
  FROM chat_trace ORDER BY fail_count DESC, step_count DESC LIMIT 20;
```

**Design decisions.**

- **Step events emit at every transition.** Verbose but the right
  call for debugging.
- **Logging is fail-safe.** Double try/except around `log_turn`.
  MySQL outage cannot break the chat.
- **JSON column for full detail, derived columns for common
  queries.** Trade-off: cheap to write, easy to query the common
  failure-pattern questions without parsing JSON, but full detail
  still queryable via `JSON_EXTRACT(steps_json, '$[N].detail')`.
- **No automated alerting.** The system writes the data; it's up
  to humans to query it on a cadence. A simple cron-based query
  could close this gap if needed.

**Interfaces.** Read by Streamlit (for UI surfaces) and humans
(for offline analysis). Written by the Orchestrator on every
turn.

---

## Layer 11: Evaluation Layer — quality assurance

**What this layer does.** Verifies that the system is doing its
job. Spans both in-band (every turn) and out-of-band (manual or
CI-driven) checks.

**Components.**

**In-band — the three runtime rings** (already detailed in
Layers 6, 7, 8). Briefly:

- Ring 1 (deterministic guards) — Layer 7
- Ring 2 (cross-reviewing LLM agents) — Layer 6
- Ring 3 (retry loops + Supervisor) — Layer 8

**Out-of-band — runnable tests.**

- **`tests/verify_evals.py`** — 7-check integration smoke test:
  groundedness imports + behaves on toy data (catches
  hallucinated `1500`, accepts grounded `47, 24%`, vacuously
  grounds empty rows, accepts group-sum `41`, accepts top-N
  partial sum `36`, accepts per-group row count `4`, rejects
  arbitrary subset sum `23`). Also tests `ensure_table` and
  `log_turn` round-trip via synthetic row. Optional live chat
  turn (skipped with `--skip-live`).
- **`tests/test_planner.py`** — 9-question eyeball smoke test.
  Prints path / plan / resolved_question for each. No assertions
  — manual inspection.
- **`tests/test_orchestrator.py`** — 5-question end-to-end smoke
  test. Prints full step trace + SQL + answer. No assertions.
- **`tests/test_golden_sql.py` + `tests/golden_sql_dataset.jsonl`**
  — automated regression suite. 25 starter entries with
  assertions on Planner path + SQL token presence/absence + row
  count bounds. Exit code 0 on all-pass, plugs into CI. This is a
  **shape check**, not an **answer correctness check**.
- **`tests/eval_prompts.md`** — 15 manually-curated prompts at
  three difficulty tiers (5 easy single-aggregation, 5 complex
  two-dimension top-N, 5 very-complex multi-step
  discover-then-drilldown). Compares actual numeric values in
  the chat's answer against ground truth computed in Excel pivot
  tables. **Answer correctness check** — complements the
  golden_sql shape check.
- **`tests/README.md`** — operational guide. Per-test what /
  example output / how to run / when to run / how to save
  output. Includes a recommended-cadence matrix keyed off "what
  did you edit".
- **`data_prep/03_eval_topic_classifier.py`** — offline Topic
  Classifier accuracy eval. Compares `inferred_topic` vs
  `ground_truth_topic` across all enriched rows. Outputs overall
  accuracy, per-topic precision/recall/F1, full confusion matrix,
  average confidence on correct vs incorrect predictions,
  lowest-confidence mistakes for spot-checking.

**Design decisions.**

- **Three runtime rings + four out-of-band evals.** The three
  rings catch errors at the per-turn level; the out-of-band
  evals catch *regressions* when prompts or models change.
- **Golden SQL is a shape check, not an answer check.** It
  asserts the SQL structure looks right (right Planner path,
  required tokens, no forbidden tokens, row count in range), not
  that the numbers in the answer are correct. The latter is
  delegated to `eval_prompts.md` (manual).
- **Two complementary regression strategies.** `test_golden_sql.
  py` runs in CI on every change; `eval_prompts.md` runs
  manually on a slower cadence (weekly, or before/after big
  changes). Both are needed.
- **Manual eval is unavoidable for answer correctness.**
  Computing exact ground truth for compound discover-then-
  drilldown questions requires multiple pivot tables and human
  judgment; automating it would be prohibitively complex.
- **`chat_trace` queries are the long-term feedback signal.**
  Real usage surfaces failure modes that no synthetic eval
  anticipates. The trace log is queryable indefinitely.

**Interfaces.** Reads from every layer (the runtime rings sit in
Layers 6/7/8; out-of-band tests exercise the full pipeline).
Tests write logs to `tests/results/` (gitignored if you want).

---

## Layer 12: Interface Layer — what the user sees

**What this layer does.** User-facing surfaces. Insulates the
backend agentic system from UI implementation details.

**Components.**

- **`streamlit_app.py`** — the Streamlit application. Two views:
  - **Chat view** (`render_chat_view`) — the default. Suggested
    starter buttons (3 options shown when chat is empty), history
    rendering via `st.chat_message`, `st.chat_input` at the
    bottom. Each assistant message renders the text, the chart
    PNG (if present), the Excel download button (if bytes
    present), the "Show SQL" expander, the "Show chart code"
    expander, and the "How I got this answer (agent trace)"
    expander.
  - **Recommendations view** (`render_report_view`) — a one-
    click deterministic report. Headline metrics, Top-10 focus
    areas, per-store deep-dive tabs with drill-down popovers,
    distribution bar charts. No agents, no LLM — pure pandas.
- **`agent_backend.py`** — the seam between UI and backend.
  - `chat_with_agent(user_message, history, filters)` — lazily
    imports `agents.orchestrator.run`, calls it, maps the
    response into an `AgentResponse` dataclass the UI knows.
    Wrapped in try/except so any agent crash surfaces as a
    polite error in the UI rather than crashing Streamlit.
  - `generate_full_recommendations(filters)` — the deterministic
    pivot-computation function for the Recommendations view. ~165
    lines of pandas. No LLM.
  - `get_data_summary()` — the cheap sidebar query (row count +
    max visit_date).
  - `_query_df(sql, params)` — parameterized SQL helper.
  - `_where_clause(filters)` — builds the parameterized WHERE
    clause from sidebar filters.
- **Sidebar layout** (`streamlit_app.py:with st.sidebar`):
  - Show Recommendations / Back to Chat button (primary action)
  - Filters block (Stores multi-select, Visit date range) — only
    rendered when view is "report"; hidden on chat view to avoid
    the misleading "I set filters, why didn't chat respect them?"
    failure mode (chat doesn't currently use the filters)
  - Data freshness card (latest visit date + total row count)
  - Clear chat button
- **Session state** (`st.session_state`):
  - `view: "chat" | "report"` — current view
  - `messages: list[dict]` — chat history
  - `filters: dict` — store / date filters

**Design decisions.**

- **`agent_backend.py` is the seam.** The UI never imports
  `agents` or `tools` directly. This means the agentic system
  can be swapped out (e.g., for a different LLM provider) without
  touching Streamlit.
- **Lazy import of `orchestrator.run`** — done inside
  `chat_with_agent` rather than at module load. Reason: the
  Recommendations view doesn't need the agent imports, and
  loading the agent package on every page render would be
  wasteful.
- **Filters are hidden on chat view.** The chat path currently
  ignores filters (documented in `agent_backend.chat_with_agent`
  docstring). Showing them would suggest they apply when they
  don't — a UX failure. Hidden until either (a) filters get
  wired into chat or (b) users explicitly need them on chat.
- **Recommendations view is deterministic.** No agents, no LLM —
  the same data + same filters always produces the same report.
  Used for stakeholder-facing summaries where consistency
  matters more than conversational flexibility.
- **Error envelope around `orchestrator.run`** — if the agent
  pipeline raises, the UI shows a polite message ("Sorry, I hit
  an unexpected error: `<err>`. Try rephrasing the question, or
  check the logs."). Better than a Streamlit traceback.

**Interfaces.** Reads from Orchestration Layer (via
`chat_with_agent`), Data Layer (via `_query_df`,
`get_data_summary`, `generate_full_recommendations`). Writes
nothing to the backend — the UI is read-only from a backend
perspective. User input flows through `st.chat_input` → session
state → `chat_with_agent` → orchestrator.

---

## Cross-layer concerns

A few topics that touch multiple layers and don't belong cleanly
in any one.

**Determinism end-to-end.** Every LLM agent at temp 0. Coder /
Code Reviewer at Sonnet 4.6 for stable SQL generation. Writer at
temp 0 with safe-values injection so prose is reproducible.
Trace is logged per turn. The same question on the same data on
the same prompts SHOULD produce the same answer; in practice
Anthropic's API has slight non-determinism at temp 0, so
identical-replay testing isn't reliable.

**Chain-of-thought scaffolding.** The `<reasoning>` block in
the Coder, the safe-values list for the Writer, the reasoning-
verification rule in the Code Reviewer — these together force
the Coder to commit to its plan before SQL, let the reviewer
verify alignment, and take arithmetic off the Writer. They were
added in response to specific empirical failure modes (LIMIT on
aggregations, wrong column choice, Writer hallucination) and are
now the system's primary correctness mechanism.

**Hard-block vs soft-block.** Most reviewer vetos are soft (retry
with feedback). Two are hard: groundedness exhaustion (ship
`GROUNDEDNESS_FAIL_TEXT` instead of unverified text) and viz
exhaustion (drop the chart). Hard blocks acknowledge that
sometimes the right thing is to refuse rather than ship a wrong
answer.

**Cost profile per turn (rough).**
- Direct path turn: 2 Haiku calls (Planner + Writer-direct) ≈ $0.002
- Clarify path turn: 1 Haiku call (Planner) ≈ $0.001
- SQL path turn happy case: 1 Haiku (Planner) + 2 Sonnet (Coder +
  Code Reviewer) + 2 Haiku (Output Reviewer + Writer) + 3 Haiku
  (Viz pipeline) ≈ $0.02 (Sonnet calls dominate)
- SQL path turn with retries: up to 5x Coder + 5x Code Reviewer
  attempts on top, capping around $0.10-0.15 worst case

For a single-user internal tool this is well within reasonable.
Per-day cost for typical usage stays under $1.

**Latency profile.**
- Planner: ~1-2 s
- Coder (Sonnet): ~3-5 s per attempt
- Code Reviewer (Sonnet): ~2-3 s per attempt
- Output Reviewer (Haiku): ~1-2 s
- Writer (Haiku): ~1-2 s
- Viz Coder + Reviewer + Generator + Multimodal Reviewer: ~6-10 s total
- Total SQL turn happy path: ~15-20 s
- SQL turn with retries: 30-60 s

---

## Known failure modes and limitations

Honest accounting of what the validation layers do NOT fully
cover.

1. **Groundedness verifies values, not attributions.** *"Necklace
   dominates with 41"* passes the check if 41 is in the
   candidate set, even if 41 is actually Bangles' total. The
   regex has no semantic model. Mitigation: low realistic impact
   because the safe-values list per turn is small.

2. **Planner is a single point of failure for history.** Only the
   Planner sees prior turns; every other agent gets only the
   `resolved_question`. The hierarchical-analysis rule mitigates
   the most common failure mode but doesn't eliminate it.

3. **Output Reviewer at Haiku can rubber-stamp.** The Coder /
   Code Reviewer upgrade to Sonnet wasn't extended to the Output
   Reviewer because the failure mode there (rubber-stamping)
   was less observed in practice. Still possible.

4. **PII not guarded.** `customer_name` and `customer_email` are
   queryable columns; the Coder will write SQL that returns them
   if asked. For a single-user internal tool this is defensible;
   for any wider deployment, a five-layer fix would be needed
   (schema annotation + Coder prompt rule + safety-guard column
   block + Writer prompt rule + redaction helper).

5. **Sandbox is dev-grade.** The restricted `__builtins__` blocks
   `open/exec/eval/__import__`, but matplotlib has callbacks and
   code-object reachable internals that a determined adversary
   could exploit. Documented as not production-grade in
   `viz_tools.py`.

6. **Tests are not in CI.** They exist but must be run manually.
   A `pre-commit` hook running `verify_evals.py --skip-live`
   plus `test_golden_sql.py` would close this.

7. **Golden SQL set is 25 entries — starter size.** Real
   coverage probably needs 100+ entries.

8. **Multimodal Viz Reviewer is flaky.** Occasional
   `Extra data: line 5 column 1 (char 40)` errors from the
   native Anthropic SDK's JSON parser on vision responses.
   Chart still ships; verdict missing. aisuite's multimodal
   path for Anthropic is patchy.

9. **No CI alerting on `chat_trace` patterns.** Trace data is in
   MySQL but nothing watches it. Spikes in supervisor invocations
   or groundedness warnings won't reach you unless you query.

10. **Sonnet at temp 0 isn't strictly deterministic.** Anthropic's
    API has slight non-determinism even at temp 0. Same input can
    produce slightly different outputs. Identical-replay testing
    is unreliable.

11. **Prompt-version traceability is git-only.** `chat_trace`
    doesn't record which prompt version produced each row. Tying
    past failures to specific prompt versions is manual via git
    history.

12. **Cost increased ~3-5x per SQL turn with the Sonnet upgrade.**
    Still pennies for internal use. If usage scales to multi-user,
    cost curve matters.

---

## Glossary / file index

| Path | Role | Layer |
|---|---|---|
| `streamlit_app.py` | UI entry | 12 |
| `agent_backend.py` | UI↔backend seam | 12 |
| `agents/orchestrator.py` | Control plane | 9 |
| `agents/llm.py` | Centralized LLM client | (cross) |
| `agents/schemas.py` | Shared dataclasses | (cross) |
| `agents/planner.py` | Routing + history resolution | 4 |
| `agents/coder.py` | SQL generation | 4 |
| `agents/code_reviewer.py` | SQL review + reasoning verification | 6 |
| `agents/output_reviewer.py` | Semantic fit + viz decision | 6 |
| `agents/writer.py` | Markdown answer composition | 4 |
| `agents/viz_coder.py` | matplotlib code generation | 4 |
| `agents/viz_code_reviewer.py` | Static viz code review | 6 |
| `agents/viz_reviewer.py` | Multimodal chart review | 6 |
| `agents/supervisor.py` | Recovery decision | 8 |
| `agents/prompts/` | System prompts (9 .txt files) | 4/6/8 |
| `tools/sql_tools.py` | SQL parser, safety, executor, schema | 5/7 |
| `tools/viz_tools.py` | Viz parser, sandbox runner | 5/7 |
| `tools/excel_tools.py` | Excel workbook generation | 5 |
| `tools/groundedness.py` | Numeric verification + safe values | 4/7 |
| `tools/trace_logger.py` | chat_trace persistence | 10 |
| `data_prep/01_generate_feedback_data_mysql.py` | Seed data | 1 |
| `data_prep/02_enrich_topics.py` | Topic Classifier (offline) | 2 |
| `data_prep/03_eval_topic_classifier.py` | Classifier eval | 11 |
| `tests/*.py + .jsonl + .md` | Eval suite | 11 |
| `docs/` | Project documentation | (meta) |
