"""
agents/schemas.py
-----------------
Shared dataclasses across all agents.

Updated in Chunks 3+4:
  - PlannerOutput now carries `resolved_question` so chat history context
    is baked in once and reused by every downstream agent.
  - AgentResponse now carries `chart_png` (bytes) for the Streamlit frontend.
  - New reviewer / coder outputs for the viz sub-pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# -----------------------------------------------------------------------------
# ## PlannerOutput
#
# `resolved_question` is the user's question with all chat-history references
# resolved. e.g. "now show me X4" + history mentioning X1 becomes
# "What's the top reason for non-purchase at store X4?". Every downstream
# agent (Coder, Output Reviewer, Writer, Viz Coder) uses this — not the
# raw user message.
# -----------------------------------------------------------------------------
@dataclass
class PlannerOutput:
    path: Literal["sql", "clarify", "direct"]
    plan: str
    resolved_question: str


# -----------------------------------------------------------------------------
# ## OutputReviewerResult
#
# Emitted after sql_executor succeeds. Two verdicts:
#   - verdict='ok'    : data answers the question, continue
#   - verdict='retry' : data doesn't answer the question; bounce back to Coder
# Plus a separate boolean `viz_applies` that drives the visualization sub-tree.
# -----------------------------------------------------------------------------
@dataclass
class OutputReviewerResult:
    verdict: Literal["ok", "retry"]
    feedback: str
    viz_applies: bool


# -----------------------------------------------------------------------------
# ## VizCodeReviewerResult and VizReviewerResult
# -----------------------------------------------------------------------------
@dataclass
class VizCodeReviewerResult:
    verdict: Literal["ok", "retry"]
    feedback: str


@dataclass
class VizReviewerResult:
    verdict: Literal["ok", "revise", "drop"]
    feedback: str


# -----------------------------------------------------------------------------
# ## StepEvent / AgentResponse
# -----------------------------------------------------------------------------
@dataclass
class StepEvent:
    agent: str
    status: Literal["start", "ok", "retry", "fail"]
    summary: str
    detail: dict = field(default_factory=dict)


@dataclass
class AgentResponse:
    text: str
    sql: Optional[str] = None
    chart_png: Optional[bytes] = None     # raw PNG bytes; Streamlit uses st.image()
    viz_code: Optional[str] = None        # the matplotlib code used (for transparency)
    excel_bytes: Optional[bytes] = None   # .xlsx of the SQL result; Streamlit offers as download
    steps: list[StepEvent] = field(default_factory=list)
