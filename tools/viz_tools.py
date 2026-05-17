"""
tools/viz_tools.py
------------------
Deterministic visualization tools used by the agentic Orchestrator.

Public exports (also re-exported from tools/__init__.py):
    viz_code_parser   — extract Python code from <viz_code>...</viz_code> tags
    viz_generator     — sandboxed matplotlib executor → PNG bytes
    png_to_base64     — encode PNG bytes for the multimodal LLM
"""

from __future__ import annotations

import re

# Reuse the ParseResult dataclass defined in sql_tools so all parser tools
# share one return shape.
from tools.sql_tools import ParseResult


# -----------------------------------------------------------------------------
# ## viz_code_parser — extract Python viz code from <viz_code>...</viz_code> tags
# -----------------------------------------------------------------------------
_VIZ_TAG_RE = re.compile(r"<viz_code>\s*(.*?)\s*</viz_code>", re.DOTALL | re.IGNORECASE)

def viz_code_parser(text: str) -> ParseResult:
    if not text or not text.strip():
        return ParseResult(ok=False, reason="empty input")
    m = _VIZ_TAG_RE.search(text)
    if m:
        code = m.group(1).strip()
        if not code:
            return ParseResult(ok=False, reason="empty <viz_code> tags")
        # Strip a stray ```python fence if the model included one inside
        if code.startswith("```"):
            code = code.strip("`")
            if code.lower().startswith("python"):
                code = code[6:].lstrip()
        # Reusing the `sql` field on ParseResult as a generic "code" payload.
        return ParseResult(ok=True, sql=code)
    return ParseResult(ok=False, reason="no <viz_code> tags found")


# -----------------------------------------------------------------------------
# ## viz_generator — sandboxed execution of matplotlib code
#
# Runs the viz code in a restricted exec() environment. The code receives:
#   df       : pandas DataFrame of the SQL result
#   rows     : list[dict] of the result rows
#   columns  : list[str] of column names
#   plt, pd, np : pre-imported as shortcuts
# and must produce a matplotlib figure (either by setting `fig = plt.figure(...)`
# or by using plt.something() — we grab plt.gcf() as fallback).
#
# Security: __builtins__ is restricted to a safe whitelist. open/exec/eval/
# __import__ are NOT available. This is reasonable for a single-user dev
# environment but NOT production-grade. For real multi-tenant use, run viz
# code in a separate subprocess with timeout + resource limits.
# -----------------------------------------------------------------------------
def viz_generator(viz_code: str, rows: list[dict], columns: list[str]) -> dict:
    import io as _io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    import numpy as np

    plt.close("all")

    safe_builtins = {
        "len": len, "range": range, "enumerate": enumerate, "zip": zip,
        "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
        "sorted": sorted, "reversed": reversed,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "str": str, "int": int, "float": float, "bool": bool,
        "isinstance": isinstance, "print": print,
        "True": True, "False": False, "None": None,
    }

    try:
        df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    except Exception as e:
        return {"ok": False, "error": f"could not build DataFrame: {e}"}

    g = {
        "__builtins__": safe_builtins,
        "plt": plt, "pd": pd, "np": np,
        "df": df, "rows": rows, "columns": columns,
    }
    l: dict = {}

    try:
        exec(viz_code, g, l)
    except Exception as e:
        plt.close("all")
        return {"ok": False, "error": f"viz code raised: {type(e).__name__}: {e}"}

    fig = l.get("fig") or g.get("fig")
    if fig is None:
        try:
            fig = plt.gcf()
        except Exception:
            fig = None

    if fig is None or not fig.axes:
        plt.close("all")
        return {"ok": False, "error": "no figure produced — set `fig = ...` or use plt.something()"}

    buf = _io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    except Exception as e:
        plt.close("all")
        return {"ok": False, "error": f"savefig failed: {e}"}
    finally:
        plt.close("all")

    return {"ok": True, "png": buf.getvalue()}


# -----------------------------------------------------------------------------
# ## png_to_base64 — encode PNG bytes for the multimodal LLM
# call_llm_vision handles base64 internally too; this helper is kept for
# clarity and audit-logging where we want the base64 explicitly available.
# -----------------------------------------------------------------------------
def png_to_base64(png_bytes: bytes) -> str:
    import base64
    return base64.b64encode(png_bytes).decode("ascii")
