"""
tools/excel_tools.py
--------------------
Deterministic tool that turns SQL result rows into an Excel workbook (bytes).

Used by the Orchestrator to attach a downloadable .xlsx to every SQL response.
The Streamlit frontend wires this to st.download_button so the user can save
the answer's underlying data.

Public exports (also re-exported from tools/__init__.py):
    to_excel              — convert rows + columns to .xlsx bytes
    LAST_TO_EXCEL_ERROR   — module-level diagnostic string; cleared on each
                            successful call, populated with the exception
                            type + message when a call fails. The
                            Orchestrator reads this to emit a meaningful
                            `to_excel · fail` step event so the failure
                            surfaces in the chat trace rather than being
                            silently swallowed.
"""

from __future__ import annotations

import sys


# Module-level diagnostic — last to_excel failure reason, or empty string.
# Set by `to_excel` and read by the Orchestrator's `_attach_excel` helper.
LAST_TO_EXCEL_ERROR: str = ""


# -----------------------------------------------------------------------------
# ## to_excel — turn SQL result rows into an Excel workbook (bytes)
# -----------------------------------------------------------------------------
def to_excel(rows: list[dict], columns: list[str], sheet_name: str = "Result") -> bytes:
    """Build .xlsx bytes from the given rows + columns.

    Returns the workbook bytes on success. On failure (e.g. openpyxl
    missing or a pandas/openpyxl incompatibility) returns `b""` AND
    populates `LAST_TO_EXCEL_ERROR` with a human-readable description
    of what went wrong. Failures also print to stderr so the issue is
    visible in the Streamlit / shell terminal where the app was launched.
    The caller (`_attach_excel` in the orchestrator) checks both the
    bytes and `LAST_TO_EXCEL_ERROR` to decide which step event to emit.
    """
    global LAST_TO_EXCEL_ERROR
    LAST_TO_EXCEL_ERROR = ""

    import io as _io
    import pandas as _pd

    if not rows:
        df = _pd.DataFrame(columns=columns)
    else:
        df = _pd.DataFrame(rows, columns=columns)

    buf = _io.BytesIO()
    try:
        with _pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    except ImportError as e:
        # openpyxl missing — the most common failure. Surface a clear hint.
        LAST_TO_EXCEL_ERROR = (
            f"openpyxl not installed: {e}. "
            "Run `pip install openpyxl` (or `pip install -r requirements.txt`)."
        )
        print(f"[to_excel] {LAST_TO_EXCEL_ERROR}", file=sys.stderr)
        return b""
    except Exception as e:
        # Any other write-time failure — log type + message so the user
        # can pip-resolve incompatible versions.
        LAST_TO_EXCEL_ERROR = f"{type(e).__name__}: {e}"
        print(f"[to_excel] write failed — {LAST_TO_EXCEL_ERROR}", file=sys.stderr)
        return b""

    return buf.getvalue()
