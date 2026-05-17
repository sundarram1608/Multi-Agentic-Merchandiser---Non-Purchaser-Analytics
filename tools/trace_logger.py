"""
tools/trace_logger.py
---------------------
Persistent trace logging for every chat turn. Writes one row per turn
to a `chat_trace` table in the same MySQL database that holds
`non_purchasers_feedback` (default: `merchandising`).

Use this to query failure patterns offline:
    - which turns invoked the Supervisor?
    - how often does the Output Reviewer kick back retry?
    - average step count and retry count per turn?
    - turns where the groundedness check flagged a warning?

Design constraints:
  - NEVER break the chat response. Every call is wrapped in try/except.
  - Idempotent table creation — safe to call every turn (cached after
    first success via `_TABLE_ENSURED`).
  - Schema is denormalized — one row per turn with the full step trace
    serialized as JSON in `steps_json`. Trade-off: cheap to write, easy
    to query the common fields, full detail still queryable via JSON
    functions if needed.

Public functions:
    log_turn(user_message, resp)  — insert one trace row
    ensure_table()                — explicit CREATE TABLE IF NOT EXISTS
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import mysql.connector


# -----------------------------------------------------------------------------
# ## MySQL connection config (mirrors tools/sql_tools.py)
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
# ## Schema
#
# The `chat_trace` table sits alongside `non_purchasers_feedback` in the
# same database. CREATE TABLE IF NOT EXISTS makes this safe to run on
# every process start.
# -----------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS chat_trace (
    trace_id            BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_message        TEXT         NOT NULL,
    path                VARCHAR(16),
    resolved_question   TEXT,
    final_sql           TEXT,
    has_chart           BOOLEAN      DEFAULT FALSE,
    has_excel           BOOLEAN      DEFAULT FALSE,
    supervisor_invoked  BOOLEAN      DEFAULT FALSE,
    groundedness_warned BOOLEAN      DEFAULT FALSE,
    step_count          INT,
    fail_count          INT,
    retry_count         INT,
    answer_text         MEDIUMTEXT,
    steps_json          MEDIUMTEXT,
    INDEX idx_created    (created_at),
    INDEX idx_path       (path),
    INDEX idx_supervisor (supervisor_invoked),
    INDEX idx_grounded   (groundedness_warned)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


# Cache the "table ensured" status across calls in the same process so we
# don't issue CREATE TABLE on every single turn.
_TABLE_ENSURED = False


def ensure_table() -> bool:
    """Create the chat_trace table if missing. Returns True on success."""
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return True
    try:
        conn = mysql.connector.connect(**_mysql_config())
    except mysql.connector.Error as e:
        print(f"[trace_logger] connect failed: {e}", file=sys.stderr)
        return False
    try:
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        cur.close()
        _TABLE_ENSURED = True
        return True
    finally:
        conn.close()


def _extract_meta(steps: list) -> dict:
    """Pull the most useful summary fields out of the step trace."""
    meta = {
        "path": "",
        "resolved_question": "",
        "supervisor_invoked": False,
        "groundedness_warned": False,
        "fail_count": 0,
        "retry_count": 0,
    }
    for s in steps:
        # Planner emits its path + resolved_question in the detail dict
        # at the "ok" status.
        if getattr(s, "agent", None) == "planner" and getattr(s, "status", None) == "ok":
            d = getattr(s, "detail", {}) or {}
            if isinstance(d, dict):
                meta["path"] = d.get("path", "") or ""
                meta["resolved_question"] = d.get("resolved", "") or ""
        if getattr(s, "agent", None) == "supervisor":
            meta["supervisor_invoked"] = True
        if getattr(s, "agent", None) == "groundedness" and getattr(s, "status", None) == "fail":
            meta["groundedness_warned"] = True
        if getattr(s, "status", None) == "fail":
            meta["fail_count"] += 1
        if getattr(s, "status", None) == "retry":
            meta["retry_count"] += 1
    return meta


def log_turn(*, user_message: str, resp) -> None:
    """Persist one chat turn. Catches every exception — never raises."""
    try:
        if not ensure_table():
            return

        steps = list(getattr(resp, "steps", []) or [])
        meta = _extract_meta(steps)

        steps_json = json.dumps(
            [
                {
                    "agent":   getattr(s, "agent", ""),
                    "status":  getattr(s, "status", ""),
                    "summary": getattr(s, "summary", ""),
                    "detail":  getattr(s, "detail", {}),
                }
                for s in steps
            ],
            default=str,
            ensure_ascii=False,
        )

        conn = mysql.connector.connect(**_mysql_config())
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO chat_trace
                  (user_message, path, resolved_question, final_sql,
                   has_chart, has_excel, supervisor_invoked,
                   groundedness_warned, step_count, fail_count,
                   retry_count, answer_text, steps_json)
                VALUES
                  (%s, %s, %s, %s,
                   %s, %s, %s,
                   %s, %s, %s,
                   %s, %s, %s)
                """,
                (
                    user_message,
                    meta["path"],
                    meta["resolved_question"],
                    getattr(resp, "sql", None),
                    bool(getattr(resp, "chart_png", None)),
                    bool(getattr(resp, "excel_bytes", None)),
                    meta["supervisor_invoked"],
                    meta["groundedness_warned"],
                    len(steps),
                    meta["fail_count"],
                    meta["retry_count"],
                    (getattr(resp, "text", "") or "")[:65000],
                    steps_json,
                ),
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        # Logging must never break the chat. Print and swallow.
        print(f"[trace_logger] log_turn failed: {e}", file=sys.stderr)
