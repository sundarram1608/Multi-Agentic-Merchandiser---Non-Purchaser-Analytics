"""
agent_backend.py
-----------------
Thin backend layer that the Streamlit frontend talks to.

Two kinds of functions in here:

  REAL (already wired to MySQL)
    - get_data_summary()           : row counts + freshness for the sidebar
    - generate_full_recommendations(): aggregates the table for the report view

  STUB (to be replaced when we build the agentic workflow)
    - chat_with_agent()            : the conversational endpoint; right now it
                                     just echoes a placeholder so the chat UI
                                     can be demoed end-to-end.

Design intent
-------------
The frontend never imports MySQL directly. Everything that talks to the DB
flows through this module, so when we replace the stub `chat_with_agent` with
the real multi-agent system, the Streamlit code does not change.

Environment
-----------
Reads these env vars (with sensible defaults):
  MYSQL_HOST       (default: 127.0.0.1)
  MYSQL_PORT       (default: 3306)
  MYSQL_USER       (default: ram)
  MYSQL_PASSWORD   (REQUIRED for live data; if missing, falls back to mocks)
  MYSQL_DB         (default: merchandising)
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

# Load .env from the project directory so MYSQL_* vars are available without
# the user having to `export` them in the shell. Silently no-ops if the file
# is missing or python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

try:
    import mysql.connector
    _HAS_CONNECTOR = True
except ImportError:
    _HAS_CONNECTOR = False


# ---------------------------------------------------------------------------
# Connection plumbing
# ---------------------------------------------------------------------------

def _mysql_config() -> dict:
    return {
        "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("MYSQL_PORT", "3306")),
        "user":     os.environ.get("MYSQL_USER", "ram"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DB", "merchandising"),
    }


@contextmanager
def _conn():
    """Yields a live MySQL connection. Raises on failure so callers can fall
    back gracefully."""
    if not _HAS_CONNECTOR:
        raise RuntimeError("mysql-connector-python is not installed.")
    cfg = _mysql_config()
    if not cfg["password"]:
        raise RuntimeError("MYSQL_PASSWORD env var is not set.")
    c = mysql.connector.connect(**cfg)
    try:
        yield c
    finally:
        c.close()


def _query_df(sql: str, params: tuple | None = None) -> pd.DataFrame:
    """Run a SQL query and return the result as a pandas DataFrame.

    Uses a plain cursor (not pd.read_sql) to avoid the pandas 2.x SQLAlchemy
    deprecation warning when working with mysql-connector-python connections.
    """
    with _conn() as c:
        cur = c.cursor()
        try:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
        finally:
            cur.close()
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _where_clause(filters: dict | None) -> tuple[str, list]:
    """Build a parameterized WHERE clause from the frontend filter dict.

    Expected shape:
        {"stores": ["X1", "X2", ...], "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}
    """
    filters = filters or {}
    clauses, params = [], []

    stores = filters.get("stores") or []
    if stores:
        placeholders = ",".join(["%s"] * len(stores))
        clauses.append(f"store_code IN ({placeholders})")
        params.extend(stores)

    if filters.get("date_from"):
        clauses.append("visit_date >= %s")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("visit_date <= %s")
        params.append(filters["date_to"])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ---------------------------------------------------------------------------
# Data summary (sidebar)
# ---------------------------------------------------------------------------

def get_data_summary() -> dict:
    """Cheap query used by the sidebar to show freshness + total rows.

    Falls back to a clearly-marked mock if MySQL isn't reachable so the
    frontend never crashes.
    """
    try:
        df = _query_df(
            # "SELECT COUNT(*) AS total, MAX(visit_date) AS latest "
            # "FROM non_purchasers_feedback"
            "SELECT COUNT(*) AS total "
            "FROM non_purchasers_feedback"
        )
        return {
            "total_rows":  int(df["total"].iloc[0]),
            # "latest_date": str(df["latest"].iloc[0]),
            "source":      "mysql",
        }
    except Exception as e:
        return {
            "total_rows":  0,
            # "latest_date": "—",
            "source":      "mock",
            "error":       str(e),
        }


# ---------------------------------------------------------------------------
# Full recommendations report
# ---------------------------------------------------------------------------

# Suggested merchandising actions, keyed by topic.
ACTION_TEMPLATES = {
    "Design Unavailable":
        "Add the specific designs customers asked for to {store}'s next replenishment for {product}.",
    "Size Unavailable":
        "Audit the size matrix for {product} at {store}; expand SKUs covering the missing sizes.",
    "Stock Unavailable":
        "Raise reorder frequency for {product} at {store}; review safety-stock thresholds.",
    "Price Too High":
        "Introduce entry-level {product} SKUs at lower price bands at {store}.",
    "Quality Concerns":
        "Review supplier quality control on {product} at {store}; train sales team on hallmark talk-track.",
    "Weight Concerns":
        "Stock lighter-weight {product} variants at {store} aimed at daily-wear customers.",
    "Color/Finish Mismatch":
        "Expand finish options (rose / white gold) for {product} at {store}.",
    "Customization Not Offered":
        "Pilot in-store engraving / customization service for {product} at {store}.",
    "Sales Service":
        "Increase floor coverage at {store}; refresh product training for the {product} category.",
    "Others":
        "Investigate the long-tail comments captured under 'Others' at {store} for {product}.",
}


def generate_full_recommendations(filters: dict | None = None) -> dict:
    """Aggregates the feedback table into a structured report the frontend
    can render. Uses `ground_truth_topic` for now — when the Topic Modeling
    agent ships, swap it for the inferred topic column.
    """
    where, params = _where_clause(filters)

    # ---------- headline rollups ----------
    # Pull the enriched columns too so the per-store deep dive can show
    # the top attribute_values for each store's most-asked product.
    df_all = _query_df(
        f"""SELECT store_code, product_looking_for, ground_truth_topic,
                   inferred_topic, non_purchase_type, attribute_value
              FROM non_purchasers_feedback {where}""",
        tuple(params),
    )
    total_rows = len(df_all)

    if total_rows == 0:
        return {"empty": True}

    topic_dist   = df_all["ground_truth_topic"].value_counts()
    product_dist = df_all["product_looking_for"].value_counts()

    # ---------- Top-10 focus areas ----------
    # For each store, find the (topic, product) pair with the highest count.
    # Rank all such pairs across stores by count to form the Top 10.
    pair_counts = (
        df_all.groupby(["store_code", "ground_truth_topic", "product_looking_for"])
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    # Take the top 10 distinct rows across the whole frame
    top_10_rows = pair_counts.head(10).to_dict("records")

    top_10 = []
    for r in top_10_rows:
        store, topic, product, count = r["store_code"], r["ground_truth_topic"], r["product_looking_for"], r["count"]
        store_total = int(df_all[df_all["store_code"] == store].shape[0])
        pct = (count / max(store_total, 1)) * 100
        top_10.append({
            "title": f"{topic} on {product} at {store}",
            "store": store,
            "product": product,
            "topic": topic,
            "evidence_count": int(count),
            "store_share_pct": round(pct, 1),
            "why": (
                f"{count} customer feedbacks at {store} — about {pct:.0f}% of "
                f"that store's non-purchases — flag {topic.lower()} for {product}."
            ),
            "action": ACTION_TEMPLATES.get(topic, "Investigate further.").format(
                store=store, product=product.lower()
            ),
        })

    # ---------- per-store breakdown ----------
    # For each store, find the reasons whose CUMULATIVE share covers ~70% of
    # that store's non-purchases. For each such reason, also pre-compute:
    #   - products affected by that reason (top 5)
    #   - specific (product, attribute_value) pairs customers asked for (top 8)
    # The frontend renders one popover button per reason; clicking it opens
    # the product + attribute drill-down inline.

    by_store = {}

    # Use inferred_topic when available (post-enrichment); fall back to
    # ground_truth_topic if the enrichment hasn't run or has gaps.
    topic_col = (
        "inferred_topic"
        if df_all["inferred_topic"].notna().any()
        else "ground_truth_topic"
    )

    for store, sub in df_all.groupby("store_code"):
        store_n = int(len(sub))
        sub_t = sub.dropna(subset=[topic_col])
        if sub_t.empty:
            sub_t = sub  # nothing classified yet — show everything we have

        topic_counts = sub_t[topic_col].value_counts()

        top_reasons: list[dict] = []
        running_pct = 0.0
        for topic, count in topic_counts.items():
            pct = float(count) / store_n * 100

            topic_slice = sub_t[sub_t[topic_col] == topic]

            # Affected products for this reason
            prod_counts = topic_slice["product_looking_for"].value_counts().head(5)
            products = [
                {
                    "product": p,
                    "count":   int(n),
                    "percent": float(n) / int(count) * 100,
                }
                for p, n in prod_counts.items()
            ]

            # Specific attribute_values customers asked for under this reason.
            # Topics that don't carry an attribute (Stock Unavailable, Sales
            # Service, Quality, Others) drop out automatically because their
            # attribute_value is NULL.
            attr_slice = topic_slice.dropna(subset=["attribute_value"])
            attributes: list[dict] = []
            if not attr_slice.empty:
                attr_counts = (
                    attr_slice
                    .groupby(["product_looking_for", "attribute_value"])
                    .size()
                    .reset_index(name="cnt")
                    .sort_values("cnt", ascending=False)
                    .head(8)
                )
                attributes = [
                    {
                        "product":         r["product_looking_for"],
                        "attribute_value": r["attribute_value"],
                        "count":           int(r["cnt"]),
                    }
                    for r in attr_counts.to_dict("records")
                ]

            top_reasons.append({
                "topic":      topic,
                "count":      int(count),
                "percent":    pct,
                "products":   products,
                "attributes": attributes,
            })

            running_pct += pct
            # Stop once we've covered ~70% — but always show at least 3
            # reasons so the view feels substantive, and cap at 8 so the
            # popover list doesn't get unwieldy.
            if len(top_reasons) >= 3 and running_pct >= 70:
                break
            if len(top_reasons) >= 8:
                break

        top_product = sub["product_looking_for"].value_counts().idxmax()
        top_product_count = int(sub["product_looking_for"].value_counts().max())

        by_store[store] = {
            "total":             store_n,
            "top_reasons":       top_reasons,
            "coverage_pct":      round(running_pct, 1),
            "top_product":       top_product,
            "top_product_count": top_product_count,
            # kept for backward compat (callers that still read top_issues)
            "top_issues": [
                {"topic": r["topic"], "count": r["count"], "percent": r["percent"]}
                for r in top_reasons[:3]
            ],
        }

    return {
        "empty": False,
        "total_rows": int(total_rows),
        "top_issue":   topic_dist.idxmax(),
        "top_product": product_dist.idxmax(),
        "top_10":      top_10,
        "by_store":    by_store,
        "topic_distribution":   topic_dist.to_dict(),
        "product_distribution": product_dist.to_dict(),
    }


# ---------------------------------------------------------------------------
# Chat endpoint (STUB)
# ---------------------------------------------------------------------------

@dataclass
class AgentResponse:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    chart_png: bytes | None = None        # populated when the viz pipeline ran
    sql: str | None = None                # populated when the SQL path ran
    viz_code: str | None = None           # the matplotlib snippet (for transparency)
    excel_bytes: bytes | None = None      # .xlsx of the SQL result for download


def chat_with_agent(user_message: str, history: list[dict], filters: dict | None = None) -> AgentResponse:
    """
    Delegates to the real agentic Orchestrator (agents/orchestrator.py).
    Filters are accepted for forward-compatibility but are not yet wired
    into the SQL generation — they will be in a later chunk.
    """
    # Lazy import so the recommendations view doesn't pay the import cost
    # of the agents package on every page load.
    from agents.orchestrator import run as orchestrator_run

    try:
        resp = orchestrator_run(user_message, history=history or [])
    except Exception as e:
        return AgentResponse(
            text=(f"Sorry, I hit an unexpected error: `{e}`. "
                  f"Try rephrasing the question, or check the logs."),
            tool_calls=[],
        )

    # Map the orchestrator's step trace into the existing tool_calls
    # slot the frontend already knows about.
    return AgentResponse(
        text=resp.text,
        tool_calls=[
            {"agent": s.agent, "status": s.status, "summary": s.summary}
            for s in resp.steps
        ],
        chart_png=resp.chart_png,
        sql=resp.sql,
        viz_code=resp.viz_code,
        excel_bytes=resp.excel_bytes,
    )
