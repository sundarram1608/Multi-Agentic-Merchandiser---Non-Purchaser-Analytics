# Layered Architecture — Index

Three versions of the architecture document exist at three depth
levels. Pick the one that matches your audience and use case.

| Document | Layers | Length | Audience | Best for |
|---|---|---|---|---|
| **[10_layered_architecture_C_executive.md](./10_layered_architecture_C_executive.md)** | 5 buckets | Short | Product owners, stakeholders, design reviewers | Understanding what the system does at a high level + the major design choices without implementation detail |
| **[10_layered_architecture_B_grouped.md](./10_layered_architecture_B_grouped.md)** | 8 layers | Medium | Engineers onboarding to the codebase, tutorial readers | Understanding the system end-to-end with enough detail to extend or debug it, but without every parameter |
| **[10_layered_architecture_A_full.md](./10_layered_architecture_A_full.md)** | 12 layers | Long | Engineers actively modifying the system, audit / compliance readers | Canonical reference — every model parameter, every retry budget, every design decision with its trade-off |

---

## At a glance — the 5 conceptual buckets (Option C view)

For the impatient reader, the system is organized into these five
buckets, working from foundational data up to user-facing surfaces:

1. **Data & Knowledge** — MySQL `non_purchasers_feedback` table + the
   offline Topic Classifier that enriches it with structured columns
   (`inferred_topic`, `topic_confidence`, `non_purchase_type`,
   `attribute_value`).

2. **Memory** — short-term chat history (last 8 messages, Planner's
   look-back) and long-term `chat_trace` MySQL persistence of every
   turn.

3. **Agents + Tools** — 9 LLM agents (Sonnet 4.6 for SQL Coder + Code
   Reviewer; Haiku 4.5 for the other 7) and 11 deterministic Python
   tools (parsers, executor, sandboxed viz runner, groundedness
   check, etc.).

4. **Orchestration + Recovery + Validation** — rule-based Python
   Orchestrator (NOT an MCP / autonomous agent loop), 5 retry loops
   with bounded budgets, Supervisor escape hatch, hard-block fallbacks,
   regex-based deterministic guards.

5. **Observability + Evaluation + Interface** — `StepEvent` traces
   surfaced in the chat UI, `chat_trace` queryable for failure
   patterns, 3 runtime validation rings, 4 out-of-band evals (manual
   + automated), Streamlit chat + recommendations views.

---

## The non-MCP design choice (read first if you're curious)

The most architecturally important decision in the system is that
it deliberately does NOT use the Model Context Protocol or any
autonomous-agent / function-calling pattern. The LLMs emit
structured text (`<sql_query>`, `<reasoning>`, `<viz_code>` tags,
JSON verdicts, prose), and a pure-Python rule-based Orchestrator
parses the text and decides deterministically which tool to call
next. The Coder is just a text emitter; it has no awareness that
`sql_safety_guard` and `sql_executor` exist.

This trades agent autonomy for predictability, reliable retry
budgets, easier validation, cheaper execution, and easier debugging.
For a domain-specific chat where the same control flow applies every
turn, autonomy is the wrong trade. The detail and rationale appear
in Option A's Layer 9 and Option B's Layer 5.

---

## Where to start

- **If you're a new engineer onboarding to the codebase:** start
  with Option C for the conceptual model (10 minutes), then read
  Option B for the implementation walkthrough.
- **If you're debugging a specific behavior:** Option A is the
  canonical reference. Use the glossary at the bottom to find
  which layer owns the component you're looking at.
- **If you're presenting the system to a non-engineering audience:**
  Option C is written for that.
- **If you're auditing the system for correctness or compliance:**
  Option A's "Known failure modes and limitations" section is the
  honest accounting of what the validation layers don't fully cover.

---

## Related docs

- `docs/00_system_overview.md` — the older canonical overview;
  faster-paced and more example-driven than the layered docs.
- `docs/06_topology_graph.svg` — call-topology view (who calls
  whom). The Orchestrator at the hub, agents and tools as spokes.
- `docs/07_workflow_tree.svg` — temporal-flow view (what happens
  in what order on a typical SQL-path chat turn).
- `docs/08_prompts_reference.md` — every system prompt's actual
  text, kept in sync with `agents/prompts/*.txt`.
- `docs/09_agents_and_tools_reference.md` — per-agent / per-tool
  spec table (call signature, retry budget, etc.).
- `tests/README.md` — operational guide for running every test
  and saving outputs.
