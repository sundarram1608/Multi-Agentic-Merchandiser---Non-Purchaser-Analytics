"""
tools/sql_tools.py
------------------
Deterministic SQL tools used by the agentic Orchestrator.

Public exports (also re-exported from tools/__init__.py):
    SCHEMA_TEXT             — table schema text (single source of truth);
                              includes the `non_purchase_type` and
                              `attribute_value` enrichment columns
    get_schema_for_prompt   — returns SCHEMA_TEXT (substituted into the
                              Coder + Code Reviewer prompts at every call,
                              not cached, so schema edits take effect on
                              the next chat turn)
    sql_parser              — extracts BOTH the Coder's <reasoning>...
                              </reasoning> block and the <sql_query>...
                              </sql_query> block into a single ParseResult.
                              The reasoning is surfaced in the agent trace
                              and passed to the Code Reviewer so it can
                              verify the SQL implements what the Coder
                              said it would.
    sql_safety_guard        — SELECT-only regex check (runs before sql_executor);
                              blocks INSERT/UPDATE/DROP/etc. and multi-
                              statement SQL
    sql_executor            — run SQL against MySQL, return rows or error dict
                              (caps result at 500 rows)
    ParseResult, SafetyResult — small dataclasses for return types
                                (ParseResult.reasoning carries the Coder's
                                 chain-of-thought when present)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# .env is one level up from this file (project root)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import mysql.connector


# -----------------------------------------------------------------------------
# ## MySQL connection config (read from .env)
# -----------------------------------------------------------------------------
def _mysql_config() -> dict:
    return {
        "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("MYSQL_PORT", "3306")),
        "user":     os.environ.get("MYSQL_USER", "ram"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DB", "merchandising"),
    }


# -----------------------------------------------------------------------------
# ## Table schema — passed verbatim into the Coder's prompt
#
# Keeping the schema in code (not just in the prompt) means the Coder always
# sees the up-to-date column list and we have a single source of truth.
# -----------------------------------------------------------------------------
SCHEMA_TEXT = """
Table: non_purchasers_feedback

Columns:
  feedback_id              INT PRIMARY KEY
  visit_date               DATE          -- YYYY-MM-DD, last ~90 days
  store_code               VARCHAR(8)    -- one of: 'X1','X2','X3','X4','X5','X6'
  customer_name            VARCHAR(120)
  customer_email           VARCHAR(160)
  user_category            VARCHAR(40)   -- 'Babies','Teen-age Girls','Office-going Women',
                                         --  'Everyday Wear','Wedding','Birthday'
  product_looking_for      VARCHAR(40)   -- 'Ear Rings','Bangles','Necklace',
                                         --  'Finger Rings','Anklets'
  reason_for_non_purchase  TEXT          -- verbose free-text reason from the customer
  ground_truth_topic       VARCHAR(40)   -- present only for eval; do NOT use in production answers
  inferred_topic           VARCHAR(40)   -- 'Design Unavailable','Size Unavailable',
                                         --  'Stock Unavailable','Price Too High',
                                         --  'Quality Concerns','Weight Concerns',
                                         --  'Color/Finish Mismatch','Customization Not Offered',
                                         --  'Sales Service','Others'
  topic_confidence         FLOAT         -- 0.0 - 1.0
  non_purchase_type           VARCHAR(40)   -- 'design','size','color','weight','price',
                                         --  'customization','service','stock','none'
  attribute_value          VARCHAR(200)  -- normalized phrase, e.g. 'star design',
                                         --  'size 8', '10 inch', 'rose gold', 'under 25k'
                                         --  NULL when non_purchase_type in ('service','stock','none')

Indexes: store_code, product_looking_for

Use `inferred_topic` and `attribute_value` (not the verbose `reason_for_non_purchase` text)
for all aggregations and filtering. They are pre-classified for clean SQL.
""".strip()


def get_schema_for_prompt() -> str:
    return SCHEMA_TEXT


# -----------------------------------------------------------------------------
# ## sql_parser — extract SQL from <sql_query>...</sql_query> tags
#
# Also extracts the optional <reasoning>...</reasoning> block that the Coder
# emits before its SQL. The reasoning isn't strictly needed to run the query,
# but the Orchestrator surfaces it in the agent trace and passes it to the
# Code Reviewer so the reviewer can verify the SQL actually implements the
# Coder's stated reasoning (chain-of-thought verification).
# -----------------------------------------------------------------------------
@dataclass
class ParseResult:
    ok: bool
    sql: str = ""
    reason: str = ""
    reasoning: str = ""   # Coder's <reasoning>...</reasoning> content, if present


_TAG_RE = re.compile(r"<sql_query>\s*(.*?)\s*</sql_query>", re.DOTALL | re.IGNORECASE)
_REASONING_RE = re.compile(r"<reasoning>\s*(.*?)\s*</reasoning>", re.DOTALL | re.IGNORECASE)

def sql_parser(text: str) -> ParseResult:
    if not text or not text.strip():
        return ParseResult(ok=False, reason="empty input")

    # Pull out the reasoning block if present — independent of SQL extraction.
    rm = _REASONING_RE.search(text)
    reasoning = rm.group(1).strip() if rm else ""

    m = _TAG_RE.search(text)
    if m:
        sql = m.group(1).strip()
        if not sql:
            return ParseResult(ok=False, reason="empty <sql_query> tags", reasoning=reasoning)
        return ParseResult(ok=True, sql=sql.rstrip(";").strip(), reasoning=reasoning)
    # Fallback: maybe the model emitted SQL without tags
    candidate = text.strip().rstrip(";").strip()
    if candidate.lower().startswith("select"):
        return ParseResult(
            ok=True, sql=candidate,
            reason="tags missing — used fallback",
            reasoning=reasoning,
        )
    return ParseResult(ok=False, reason="no <sql_query> tags and no SELECT found",
                       reasoning=reasoning)


# -----------------------------------------------------------------------------
# ## sql_safety_guard — SELECT-only check, runs before sql_executor
# -----------------------------------------------------------------------------
@dataclass
class SafetyResult:
    ok: bool
    reason: str = ""


_BANNED = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|rename|create|grant|revoke|"
    r"replace|merge|call|execute|exec|load|outfile|into\s+outfile)\b",
    re.IGNORECASE,
)

def sql_safety_guard(sql: str) -> SafetyResult:
    s = (sql or "").strip()
    if not s:
        return SafetyResult(ok=False, reason="empty SQL")
    if re.search(r";\s*\S", s):
        return SafetyResult(ok=False, reason="multi-statement SQL is not allowed")
    head = re.match(r"^\s*(select|with)\b", s, re.IGNORECASE)
    if not head:
        return SafetyResult(ok=False, reason="only SELECT (or WITH ... SELECT) is allowed")
    bad = _BANNED.search(s)
    if bad:
        return SafetyResult(ok=False, reason=f"banned keyword: {bad.group(0).upper()}")
    return SafetyResult(ok=True)


# -----------------------------------------------------------------------------
# ## sql_executor — run the SQL and return rows or an error dict
# -----------------------------------------------------------------------------
def sql_executor(sql: str, row_limit: int = 500) -> dict:
    try:
        conn = mysql.connector.connect(**_mysql_config())
    except mysql.connector.Error as err:
        return {"ok": False, "error": f"connection failed: {err}"}

    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql)
            rows = cur.fetchmany(row_limit)
            columns = [d[0] for d in cur.description] if cur.description else []
            return {
                "ok": True,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": cur.rowcount > row_limit if cur.rowcount != -1 else False,
            }
        except mysql.connector.Error as err:
            return {"ok": False, "error": str(err)}
        finally:
            cur.close()
    finally:
        conn.close()
