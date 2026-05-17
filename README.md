# Multi-Agent Merchandising Assistant — Non-Purchaser Feedback Analytics

A multi-agent LLM application that turns natural-language merchandising questions into grounded, action-ready answers over a MySQL database of non-purchase feedback from a fictional jewelry retailer.

Ask *"What should we stock more of at store X1?"* and the system plans the question, generates SQL, reviews it, runs it, drafts an answer, verifies every number against the rows, optionally renders a chart, persists a full agent trace, and surfaces the result in a Streamlit chat UI — complete with an Excel download of the underlying data and a collapsible "How I got this answer" trace.

Built on top of Anthropic's Claude (Sonnet 4.6 for SQL-quality-critical agents, Haiku 4.5 elsewhere) via the [aisuite](https://github.com/andrewyng/aisuite) abstraction.

---

## Table of contents

- [Highlights](#highlights)
- [Architecture at a glance](#architecture-at-a-glance)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Getting started](#getting-started)
  - [1. Fork and clone](#1-fork-and-clone)
  - [2. Create a virtual environment](#2-create-a-virtual-environment)
  - [3. Install dependencies](#3-install-dependencies)
  - [4. Create your `.env` file](#4-create-your-env-file)
  - [5. Set up the MySQL database](#5-set-up-the-mysql-database)
  - [6. Run the Streamlit app](#6-run-the-streamlit-app)
- [Evaluation suite](#evaluation-suite)
- [Documentation](#documentation)
- [Configuration reference](#configuration-reference)
- [Contributing](#contributing)
- [License](#license)

---

## Highlights

- **9 specialized agents** (Planner, Coder, Code Reviewer, Output Reviewer, Writer, Viz Coder, Viz Code Reviewer, Viz Reviewer, Supervisor) coordinated by a deterministic, rule-based Orchestrator — no autonomous tool calling, every routing decision is auditable.
- **Hard-block groundedness check** — every numeric value the Writer produces is verified against the SQL result rows using a 10-pattern deterministic candidate set (cell values, label-embedded numbers, grand totals, per-row percentages, per-group subtotals & shares, within-group and across-group top-N partial sums). Unverifiable text is rejected; the Writer is retried up to twice before a safe fallback is shipped.
- **Chain-of-thought scaffolding** — the Coder emits a `<reasoning>` block (Dimensions / Filters / Aggregation / Completeness) before any SQL; the Code Reviewer checks that the SQL implements the stated reasoning.
- **Persistent agent traces** — every chat turn is logged to a `chat_trace` MySQL table with full step-by-step JSON, queryable from any BI tool.
- **Offline evals built in** — Topic Classifier accuracy report, a 26-question golden Q→SQL regression suite, a 7-check smoke test, and 15 manual evaluation prompts.
- **Streamlit front-end** with two modes: free-form chat with Excel download / chart / trace, and a static Recommendations report.

---

## Architecture at a glance

```
User question
     │
     ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          Orchestrator                                │
│        (deterministic rules — not an autonomous agent)               │
└──────────────────────────────────────────────────────────────────────┘
   │                       │                          │
   ▼                       ▼                          ▼
Planner          Coder ─► Code Reviewer ─► run SQL  Viz Coder ─► Viz Reviewer
(sql/direct/                  │                          │
 clarify)                     ▼                          ▼
                         Output Reviewer ─► Writer ─► Groundedness check
                                                          │
                                                          ▼
                                                    chat_trace (MySQL)
                                                          │
                                                          ▼
                                                   Streamlit UI
```

For the full topology and workflow diagrams, see [`docs/06_topology_graph.png`](docs/06_topology_graph.png) and [`docs/07_workflow_tree.png`](docs/07_workflow_tree.png). The complete narrative walk-through lives in [`docs/00_system_overview.md`](docs/00_system_overview.md).

---

## Repository layout

```
non_purchaser_feedback/
├── streamlit_app.py            # Streamlit front-end (Chat + Recommendations)
├── agent_backend.py            # Thin wrapper the UI calls
├── agents/                     # The 9 agents
│   ├── orchestrator.py         #   deterministic router + groundedness loop
│   ├── planner.py              #   sql / direct / clarify decision
│   ├── coder.py                #   reasoning + SQL generation (Sonnet 4.6)
│   ├── code_reviewer.py        #   reasoning ↔ SQL consistency check (Sonnet 4.6)
│   ├── output_reviewer.py      #   inspects executed rows for sanity
│   ├── writer.py               #   final natural-language answer
│   ├── viz_coder.py            #   matplotlib chart generation
│   ├── viz_code_reviewer.py    #   chart-code sanity check
│   ├── viz_reviewer.py         #   chart-vs-data sanity check
│   ├── supervisor.py           #   recovery decisions on hard failures
│   ├── llm.py                  #   aisuite wrapper, model constants
│   ├── schemas.py              #   pydantic outputs for Planner et al.
│   └── prompts/                #   editable prompt files (one per agent)
├── tools/
│   ├── sql_tools.py            # MySQL connection + run_sql()
│   ├── groundedness.py         # 10-pattern numeric verification
│   ├── excel_tools.py          # download_data_as_excel
│   ├── viz_tools.py            # safe matplotlib executor
│   └── trace_logger.py         # chat_trace persistence
├── data_prep/
│   ├── 01_generate_feedback_data_mysql.py   # seed the table
│   ├── 02_enrich_topics.py                  # closed-enum topic classifier
│   └── 03_eval_topic_classifier.py          # offline accuracy eval
├── data_sample/
│   ├── non_purchasers_feedback.csv          # raw seed CSV
│   ├── non_purchasers_feedback_topicenriched.xlsx
│   └── Manual Eval.xlsx
├── tests/
│   ├── test_planner.py
│   ├── test_orchestrator.py
│   ├── test_golden_sql.py
│   ├── golden_sql_dataset.jsonl             # 26 regression cases
│   ├── verify_evals.py                      # 7-check smoke test
│   ├── eval_prompts.md                      # 15 manual prompts (3 tiers)
│   └── README.md
├── docs/                       # System overview, prompt reference, layered
│                               # architecture (full / grouped / executive),
│                               # topology + workflow diagrams, slide deck.
├── requirements.txt
├── LICENSE                     # Apache 2.0
└── README.md
```

---

## Prerequisites

You will need:

1. **Python 3.10+** — the codebase uses `from __future__ import annotations` and modern typing.
2. **MySQL 8.0+** — locally installed, or accessible over the network. The default config expects a database called `merchandising` with a `non_purchasers_feedback` table; a `chat_trace` table is auto-created on first use.
3. **An Anthropic API key** — sign up at [console.anthropic.com](https://console.anthropic.com/) and create a key. The system uses Claude Sonnet 4.6 (Coder + Code Reviewer) and Claude Haiku 4.5 (every other agent). Make sure your account has access to those models.
4. **A C compiler** — only if `mysql-connector-python` needs to build a wheel on your platform. Most platforms ship a pre-built wheel, so this is rarely required.

---

## Getting started

### 1. Fork and clone

Fork the repository on GitHub (so you can keep your own changes and contribute back), then clone your fork locally:

```bash
git clone https://github.com/sundarram1608/Multi-Agentic_Merchandiser-NonPurchaser_Analytics.git
cd non_purchaser_feedback
```

### 2. Create a virtual environment

Use a fresh virtual environment so your system Python stays clean. The example below uses `venv`, which ships with Python.

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt should now show `(.venv)` in front. If you prefer `conda`, `pipenv`, `poetry`, or `uv`, any of them will work — just install the same dependencies listed in `requirements.txt`.

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This pulls in Streamlit, the Anthropic SDK (through `aisuite`), `mysql-connector-python`, pandas, matplotlib, `openpyxl` (for Excel exports), `python-dotenv`, and numpy.

### 4. Create your `.env` file

The application loads configuration from a `.env` file at the repository root. **This file is NOT committed to git** — keep your secrets out of source control.

Create a file named `.env` in the project root with the following contents (replace the placeholder values):

```dotenv
# ---- Anthropic / Claude API ----
ANTHROPIC_API_KEY=sk-ant-api03-...your-key-here...

# ---- MySQL connection ----
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_DB=merchandising
```

Defaults if you omit a variable: `MYSQL_HOST=127.0.0.1`, `MYSQL_PORT=3306`, `MYSQL_USER=ram`, `MYSQL_PASSWORD=""`, `MYSQL_DB=merchandising`. The `ANTHROPIC_API_KEY` has no default — the app will fail at the first LLM call if it is missing.

> **Tip.** On macOS / Linux, you can also export these in your shell rather than using a `.env` file. The code uses `python-dotenv` only as a convenience; standard environment variables work equally well.

### 5. Set up the MySQL database

Create the database and a user that can read/write it:

```sql
CREATE DATABASE IF NOT EXISTS merchandising
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'your_mysql_user'@'localhost'
  IDENTIFIED BY 'your_mysql_password';

GRANT ALL PRIVILEGES ON merchandising.* TO 'your_mysql_user'@'localhost';
FLUSH PRIVILEGES;
```

Then populate it with the seed feedback and run the topic-enrichment classifier:

```bash
# Loads data_sample/non_purchasers_feedback.csv into MySQL
python data_prep/01_generate_feedback_data_mysql.py

# Runs the closed-enum topic classifier and writes
# inferred_topic + non_purchase_type + attribute_value back to MySQL
python data_prep/02_enrich_topics.py
```

The `chat_trace` table is created automatically the first time the Streamlit app handles a turn — no separate migration is required.

### 6. Run the Streamlit app

```bash
streamlit run streamlit_app.py
```

Streamlit opens the app at `http://localhost:8501`. The sidebar lets you switch between **Chat** (free-form Q&A) and **Recommendations** (a static, parameter-filtered report).

Some questions to try in Chat:

- *"What's the top reason for non-purchase at store X1?"*
- *"What should we stock more of at store X1?"*
- *"Compare the top reasons across X1 and X3 by product."*
- *"What sizes are customers asking for in finger rings at X4?"*
- *"How many Stock Unavailable feedbacks does X2 have?"*

For each Chat answer the UI surfaces three affordances: an Excel download of the underlying rows, a *Show SQL* expander, and a *How I got this answer* agent trace.

---

## Evaluation suite

Three offline checks live in `tests/`. Run them after editing prompts, swapping models, or before publishing a release.

```bash
# 1. Topic Classifier accuracy (compares 02_enrich_topics output against
#    Manual Eval.xlsx ground truth)
python data_prep/03_eval_topic_classifier.py
python data_prep/03_eval_topic_classifier.py --csv eval_report.csv

# 2. Golden Q→SQL regression suite — pushes 26 starter questions through
#    the full Orchestrator and asserts Planner path, SQL tokens, and
#    row-count bounds. Exit code 0 if all pass, so it plugs into CI.
python tests/test_golden_sql.py

# 3. 7-check smoke test — verifies the eval scaffolding itself
python tests/verify_evals.py
```

A 15-prompt manual evaluation list (3 tiers — happy path, edge cases, recovery) lives in [`tests/eval_prompts.md`](tests/eval_prompts.md). See [`tests/README.md`](tests/README.md) for the full operational guide.

---

## Documentation

The `docs/` folder is the canonical source of truth for design decisions:

| File | What it covers |
|---|---|
| [`00_system_overview.md`](docs/00_system_overview.md) | End-to-end narrative — read this first. |
| [`06_topology_graph.png`](docs/06_topology_graph.png) | Agents, tools, and data destinations. |
| [`07_workflow_tree.png`](docs/07_workflow_tree.png) | Decision tree the Orchestrator walks per turn. |
| [`08_prompts_reference.md`](docs/08_prompts_reference.md) | Mirror of every prompt file with rationale. |
| [`09_agents_and_tools_reference.md`](docs/09_agents_and_tools_reference.md) | One-page reference for each agent / tool. |
| [`10_layered_architecture.md`](docs/10_layered_architecture.md) | Index into the three layered-architecture variants (A: full, B: grouped, C: executive). |
| `Agentic_AI_Technical_Report.pdf` | Long-form technical writeup. |
| `Merchandising_Agentic_AI_Presentation.pdf` | Slide deck for a 15-minute talk. |

---

## Configuration reference

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Authenticates Claude calls (Sonnet 4.6 + Haiku 4.5). |
| `MYSQL_HOST` | No | `127.0.0.1` | MySQL hostname. |
| `MYSQL_PORT` | No | `3306` | MySQL port. |
| `MYSQL_USER` | No | `your_user_name` | MySQL username. |
| `MYSQL_PASSWORD` | No | empty | MySQL password. |
| `MYSQL_DB` | No | `merchandising` | Database name; holds both `non_purchasers_feedback` and `chat_trace`. |

Model selection lives in `agents/llm.py`:

```python
MODEL_HAIKU  = "anthropic:claude-haiku-4-5-20251001"   # most agents
MODEL_SONNET = "anthropic:claude-sonnet-4-6"           # Coder + Code Reviewer
```

All agents run at `temperature=0.0` for reproducibility. Prompt files are re-read from disk on every call (no module-level caching) so you can iterate on `agents/prompts/*.txt` without restarting Streamlit.

---

## Contributing

Pull requests welcome. Before opening one:

1. Run the three eval commands above and confirm all 26 golden-suite cases pass.
2. If you change a prompt, mirror your edit into [`docs/08_prompts_reference.md`](docs/08_prompts_reference.md) so the documentation stays byte-identical to the source.
3. If you add a new failure mode to the golden suite, bump the count in [`docs/00_system_overview.md`](docs/00_system_overview.md).

For larger architectural changes, open an issue first and reference the relevant layer in [`docs/10_layered_architecture.md`](docs/10_layered_architecture.md).

---

## License

Released under the [Apache License 2.0](LICENSE). You're free to use, modify, and redistribute the code; the license requires preservation of copyright notices and a `NOTICE` of any significant changes.

The sample dataset in `data_sample/` is **fictional** — generated synthetically for demonstration purposes and not derived from any real retailer.
