"""
tools — deterministic helpers the Orchestrator calls.

Organized by domain:
    sql_tools     — schema + SQL parser + safety guard + executor
    viz_tools     — matplotlib code parser + sandboxed generator + base64
    excel_tools   — turn SQL result rows into an .xlsx workbook
    groundedness  — verify Writer numbers actually appear in result rows
    trace_logger  — persist every chat turn to MySQL for offline analysis

Importing from `tools` directly works for the common helpers:
    from tools import sql_executor, viz_generator, to_excel

For everything else use the submodule explicitly:
    from tools.sql_tools import SCHEMA_TEXT, ParseResult
"""

from tools.sql_tools import (
    SCHEMA_TEXT,
    get_schema_for_prompt,
    sql_parser,
    sql_safety_guard,
    sql_executor,
    ParseResult,
    SafetyResult,
)

from tools.viz_tools import (
    viz_code_parser,
    viz_generator,
    png_to_base64,
)

from tools.excel_tools import (
    to_excel,
)

from tools.groundedness import (
    groundedness_check,
    GroundednessResult,
    safe_derivations_summary,
)

from tools.trace_logger import (
    log_turn,
    ensure_table,
)

__all__ = [
    # sql
    "SCHEMA_TEXT", "get_schema_for_prompt", "sql_parser",
    "sql_safety_guard", "sql_executor", "ParseResult", "SafetyResult",
    # viz
    "viz_code_parser", "viz_generator", "png_to_base64",
    # excel
    "to_excel",
    # eval / persistence
    "groundedness_check", "GroundednessResult", "safe_derivations_summary",
    "log_turn", "ensure_table",
]
