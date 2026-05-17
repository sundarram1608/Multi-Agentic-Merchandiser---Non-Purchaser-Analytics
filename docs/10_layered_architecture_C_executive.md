# Layered Architecture — Executive View (Option C: 5 buckets)

A high-level architecture summary for stakeholders, product owners,
and technical leads who want to understand what the system does and
the major design choices without going into implementation detail.

For the operational walkthrough with implementation specifics, see
`10_layered_architecture_B_grouped.md`. For the full implementation-
level deep-dive, see `10_layered_architecture_A_full.md`.

---

## One-paragraph summary

The Merchandising AI is a multi-agent chat assistant that turns
natural-language questions about non-purchase feedback into
grounded SQL answers, optional matplotlib charts, and downloadable
Excel files. Nine LLM agents (Sonnet 4.6 for the SQL Coder + Code
Reviewer; Haiku 4.5 for everyone else) are coordinated by a
rule-based Python Orchestrator — deliberately NOT an autonomous
LLM agent loop. The system has 5 conceptual buckets working from
foundational data up to user-facing surfaces, with three runtime
validation rings + four out-of-band evals + a `chat_trace` MySQL
audit log together making the system answerable for its outputs.

---

## Bucket 1: Data & Knowledge

**What lives here.**

- MySQL `non_purchasers_feedback` table — ~1,200 rows of feedback
  with 13 columns (9 captured + 4 LLM-enriched).
- `chat_trace` audit table — auto-created, holds one row per chat
  turn.
- Offline enrichment script `02_enrich_topics.py` — runs once at
  ingestion, classifies free-text feedback into 10 canonical
  topics + 9 categories of non-purchase reason via Claude Haiku.
- Canonical schema description `SCHEMA_TEXT` — substituted into
  agent prompts at every call.
- Closed enums for topic + non_purchase_type validated in code.

**Big choices.**

- **Offline enrichment over on-demand.** Doing classification once
  at data ingest means chat SQL aggregates against clean
  structured columns instead of pattern-matching free text. Massive
  simplification for the downstream Coder.
- **Schema vocabulary discipline.** The column rename
  `attribute_type` → `non_purchase_type` was a deliberate move:
  the original name collided with the natural-language word
  "attributes" and caused the LLM to disambiguate incorrectly.
  Schema-level naming clarity beats prompt-level scaffolding.
- **Closed enums + Python validation** — belt-and-braces against
  LLM vocabulary drift.

---

## Bucket 2: Memory

**What lives here.**

- Short-term per-session chat history (Streamlit session state).
- The Planner's 8-message look-back window — the system's working
  memory of recent turns.
- The `resolved_question` mechanism — every downstream agent reads
  a context-resolved question that bakes in chat history; only the
  Planner sees the raw history.
- Long-term `chat_trace` MySQL persistence — every turn audit-
  logged for offline analysis.

**Big choices.**

- **History resolution happens ONCE, at the Planner.** Every other
  agent is stateless on history. Prevents history-aware bugs from
  spreading; trade-off is the Planner is a single point of failure
  for history-related errors.
- **8-message look-back, not unbounded.** Per-turn cost stays flat
  as session grows; reference-resolution payoff drops sharply with
  depth.
- **Multi-turn hierarchical-analysis rule.** A specific prompt
  section in the Planner teaches it to distinguish "pattern
  extension" follow-ups (re-discover at new scope) from
  "value carry-forward" follow-ups (preserve specific values).
  Added after observing the X3 → X1/X2 failure mode.

---

## Bucket 3: Agents + Tools

**What lives here.**

- **9 LLM agents:** Planner (routing), Coder (SQL generation with
  chain-of-thought), Code Reviewer (SQL + reasoning verification),
  Output Reviewer (semantic fit), Writer (markdown answer), Viz
  Coder (matplotlib), Viz Code Reviewer (static review), Viz
  Reviewer (multimodal chart review), Supervisor (recovery
  decisions when retries exhaust).
- **11 deterministic tools:** `sql_parser`, `sql_safety_guard`,
  `sql_executor`, `viz_code_parser`, `viz_generator` (sandboxed),
  `to_excel`, `groundedness_check`, `safe_derivations_summary`,
  `log_turn`, `ensure_table`, `SCHEMA_TEXT`/`get_schema_for_prompt`.
- **9 prompt files** in `agents/prompts/` (one per LLM agent).

**Big choices.**

- **Sonnet 4.6 for Coder + Code Reviewer, Haiku 4.5 for everyone
  else.** SQL quality is the system's bottleneck; Haiku struggled
  on MySQL window-function syntax and column disambiguation.
  Upgrading just these two agents gave the quality benefit
  without 3-5x-ing cost across the whole pipeline.
- **All agents at temperature 0.** Determinism over creativity.
  Writer was originally 0.2 for natural prose; dropped to 0 after
  observing inconsistent outputs on same-question retries.
- **Chain-of-thought `<reasoning>` block in Coder.** Forces
  Dimensions / Filters / Aggregation / Completeness commitment
  before SQL. Code Reviewer can then verify SQL matches reasoning,
  catching the most damaging failure (plausible-looking SQL that
  doesn't match intent).
- **`safe_derivations_summary` injection for Writer.** Takes
  arithmetic OFF the LLM. Writer picks from a pre-computed list
  of legitimate numeric values rather than synthesizing them.
- **Coder + Code Reviewer prompts re-read from disk on every
  call.** No module-level cache. Lets prompt edits take effect on
  the next chat turn without a full Streamlit restart — critical
  for prompt-engineering iteration speed.

---

## Bucket 4: Orchestration, Recovery & Validation

**What lives here.**

- **The Orchestrator** (`agents/orchestrator.py`) — pure Python
  control plane. No LLM in the decision logic. Calls every agent
  and every tool itself.
- **5 retry loops** with hardcoded budgets (5 / 2 / 2 / 2 / 1) and
  defined exhaustion behaviors.
- **The Supervisor agent** as the escape hatch when SQL loops
  exhaust. Picks one of four recovery actions.
- **Hard-block fallbacks:** `GROUNDEDNESS_FAIL_TEXT` (Writer ↔
  groundedness exhaustion), chart drop (Viz exhaustion).
- **Deterministic guards:** SELECT-only safety guard, sandboxed
  Python exec for charts, regex-based groundedness check on Writer
  output, enum validators on every agent's structured output.

**Big choices.**

- **Rule-based Orchestrator, NOT an MCP / autonomous LLM agent
  loop.** This is the single most important architectural choice
  in the system. The LLMs emit structured text; the Orchestrator
  parses and routes deterministically. Trade-off: less agent
  autonomy, much more predictability, reliable retry budgets,
  easier validation, easier debugging, cheaper. For a focused
  domain-specific task this is the right trade; for a "do
  anything" autonomous use case it wouldn't be.
- **Hard-block over soft-block for groundedness.** When the Writer
  can't produce verifiable numbers after 2 attempts, the system
  ships a fixed "I couldn't verify" message and points the user
  at Show SQL / Excel download. Better than letting wrong numbers
  through.
- **Bounded retry budgets.** Every loop has a fixed maximum. The
  worst-case turn cost is bounded (~$0.15) even with full retries.
- **Defense in depth.** SQL goes through parsing → safety guard →
  reviewer → executor → reviewer (post-error). Each layer can
  reject.

---

## Bucket 5: Observability, Evaluation & Interface

**What lives here.**

- **Step trace** — every transition emits a `StepEvent`; full
  trace visible in the chat's "How I got this answer" expander
  and persisted to `chat_trace` MySQL.
- **`chat_trace` table queries** for offline pattern analysis —
  supervisor invocation rate, retry rate, groundedness warning
  rate.
- **3 runtime validation rings** (in-band, every turn):
  deterministic guards → cross-reviewing LLM agents → retry loops
  with Supervisor.
- **4 out-of-band evals:**
  - `tests/verify_evals.py` — 7-check integration smoke test
  - `tests/test_golden_sql.py` + `golden_sql_dataset.jsonl` —
    automated regression suite (shape check, CI-style)
  - `tests/eval_prompts.md` — 15 manual prompts at three
    difficulty tiers for answer-correctness comparison vs Excel
  - `data_prep/03_eval_topic_classifier.py` — offline classifier
    accuracy + confusion matrix
- **`tests/README.md`** — operational guide for running tests.
- **Streamlit UI** — chat view (text + chart + Excel + Show SQL +
  trace) and Recommendations view (deterministic pandas report,
  no agents).
- **`agent_backend.py`** — seam between UI and the agentic
  pipeline.

**Big choices.**

- **Three runtime rings + four out-of-band evals.** The rings
  catch errors at the per-turn level; the out-of-band evals catch
  *regressions* when prompts or models change.
- **Shape check (golden_sql) vs answer correctness check
  (eval_prompts) are complementary.** The former is fast,
  automated, runs in CI. The latter is slow, manual, requires
  Excel — but verifies the actual numbers.
- **Fail-safe logging.** A MySQL outage on `chat_trace` cannot
  break the chat.
- **Filters hidden on chat view.** Chat path doesn't use sidebar
  filters; showing them would suggest they apply. Filters live
  only on Recommendations view.
- **Recommendations view is deterministic.** No agents, no LLM —
  pure pandas pivots. Same data + filters always produces the
  same report; for stakeholder-facing consistency.

---

## What the system is good at

- **Compound SQL questions** with multiple dimensions, ordered
  rankings, per-group top-N queries (the chain-of-thought
  scaffolding shines here).
- **Multi-turn follow-ups** with proper pattern-extension vs
  value-carry-forward distinction.
- **Grounded prose answers** that cite numbers verified against
  the data.
- **Self-explanation** — every answer surfaces its SQL, chart
  code, and full agent trace.
- **Auditability** — every turn is logged with enough detail to
  reconstruct what happened and why.

## What the system is honestly limited at

- **Semantic attribution** — the groundedness check verifies
  numeric values appear in the data but can't verify they're
  attributed to the right groups.
- **Privacy** — `customer_name` and `customer_email` are
  queryable; no redaction layer is in place.
- **Production multi-tenancy** — the viz sandbox is dev-grade;
  cost per turn is acceptable for single-user but scales linearly.
- **Strict determinism** — Sonnet at temp 0 has slight non-
  determinism; identical-replay testing isn't reliable.
- **Automated CI** — tests exist but must be run manually.

For full detail on each limitation, see the limitations section in
`10_layered_architecture_A_full.md`.

---

## When to use which doc

- **This document** (Option C, 5 buckets) — when you need to
  understand the system at a high level for stakeholder
  conversations, design reviews, or onboarding.
- **`10_layered_architecture_B_grouped.md`** (Option B, 8 layers) —
  when you need to understand the system to extend it or debug
  it, but don't need every implementation parameter.
- **`10_layered_architecture_A_full.md`** (Option A, 12 layers) —
  when you need the canonical reference. Every model parameter,
  every retry budget, every design decision with its trade-off
  documented in implementation-level detail.
