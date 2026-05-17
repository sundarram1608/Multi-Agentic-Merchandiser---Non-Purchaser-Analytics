"""
streamlit_app.py
----------------
Front-end for the Brand X Merchandising AI.

Two interaction modes, switched from the sidebar:

  1. Chat        — free-form Q&A. Hits agent_backend.chat_with_agent (stub).
  2. Recommendations — full report computed from MySQL (real numbers today).

Run
---
    pip install -r requirements.txt
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import streamlit as st

import agent_backend as backend


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(
                    page_title="Merchandise Co-pilot | Non Purchaser Agent",
                    page_icon=None,
                    layout="wide",
                    initial_sidebar_state="expanded",
                )

# Light cosmetic polish — fits within Streamlit's allowed CSS surface.
# st.markdown(
#     """
#     <style>
#       .block-container { padding-top: 2rem; padding-bottom: 2rem; }
#       .stMetricValue   { font-size: 1.4rem; }
#       .focus-card {
#           border: 1px solid #e5e7eb; border-radius: 10px;
#           padding: 0.9rem 1rem; margin-bottom: 0.5rem;
#           background: #fafafa;
#       }
#       .focus-card .tag {
#           display: inline-block; padding: 2px 8px; border-radius: 999px;
#           background: #eef2ff; color: #3730a3; font-size: 12px;
#           margin-right: 6px;
#       }
#       .stub-banner {
#           background: #fffbeb; color: #92400e;
#           border-left: 4px solid #f59e0b;
#           padding: 0.6rem 0.9rem; border-radius: 6px; margin-bottom: 1rem;
#       }
#     </style>
#     """,
#     unsafe_allow_html=True,
# )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

DEFAULT_STORES = ["X1", "X2", "X3", "X4", "X5", "X6"]

if "view" not in st.session_state:
    st.session_state.view = "chat"        # "chat" | "report"
if "messages" not in st.session_state:
    st.session_state.messages = []        # [{"role": "user"|"assistant", "content": str}]
if "filters" not in st.session_state:
    st.session_state.filters = {
                                    "stores":    list(DEFAULT_STORES),
                                    "date_from": None,
                                    "date_to":   None,
                                }


def _switch_view(target: str) -> None:
    st.session_state.view = target


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    # st.markdown("## Brand X")
    # st.caption("Merchandising AI · Non-Purchase Insights")

    # Filters apply ONLY to the Recommendations view. The chat path
    # ignores them by design (agent_backend.chat_with_agent accepts the
    # filters dict for forward-compat but does not wire it into SQL),
    # so we hide the widgets entirely when the user is on the chat
    # view to avoid the misleading "I set filters, why didn't the chat
    # respect them?" failure mode.
    if st.session_state.view == "report":
        st.markdown("**Filters**")

        selected_stores = st.multiselect(
                                        "Stores",
                                        options=DEFAULT_STORES,
                                        default=st.session_state.filters["stores"],
                                        help="Limit the analysis to specific stores.",
                                    )
        st.session_state.filters["stores"] = selected_stores or DEFAULT_STORES

        date_range = st.date_input(
                                    "Visit date range",
                                    value=(),
                                    help="Optional — leave empty for all dates.",
                                )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            st.session_state.filters["date_from"] = date_range[0].isoformat()
            st.session_state.filters["date_to"]   = date_range[1].isoformat()
        else:
            st.session_state.filters["date_from"] = None
            st.session_state.filters["date_to"]   = None

        st.divider()


    # Primary actions
    if st.session_state.view == "chat":
        st.button(
                    "Show Recommendations",
                    type="primary",
                    use_container_width=True,
                    on_click=_switch_view,
                    args=("report",),
                )
    else:
        st.button(
                    "Back to Chat",
                    type="primary",
                    use_container_width=True,
                    on_click=_switch_view,
                    args=("chat",),
                )
   
    st.divider()

    # Data freshness
    summary = backend.get_data_summary()
    st.markdown("**Data**")
    if summary.get("source") == "mysql":
        # st.caption(f"Latest visit: **{summary['latest_date']}**")
        st.caption(f"Total feedbacks: **{summary['total_rows']:,}**")
    else:
        st.warning("Could not reach MySQL. Showing placeholder values.")
        if summary.get("error"):
            st.caption(f"`{summary['error']}`")

    st.divider()

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()


# ---------------------------------------------------------------------------
# Chat view
# ---------------------------------------------------------------------------

def render_chat_view() -> None:
    st.markdown("## Non Purchaser Agent")
    st.caption("Ask anything about non-purchase feedback across the stores.")

    # Both surfaces are live: the chat backend dispatches into the full
    # 9-agent orchestrator (`agent_backend.chat_with_agent` → `agents.
    # orchestrator.run`), and the Recommendations view computes its
    # report deterministically against MySQL via
    # `agent_backend.generate_full_recommendations` — no agents on that
    # path.

    # Suggested starters when the chat is empty
    if not st.session_state.messages:
        st.markdown("**Try one of these to get started:**")
        suggestions = [
            "What is the top reason for non-purchase across all stores?",
            "Which store has the most design-related complaints?",
            "Compare size issues between X1 and X4.",
        ]
        cols = st.columns(3)
        for col, sug in zip(cols, suggestions):
            with col:
                if st.button(sug, use_container_width=True, key=f"sug_{hash(sug)}"):
                    _submit_user_message(sug)
                    st.rerun()

    # History
    for idx, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            chart_png = msg.get("chart_png")
            if chart_png:
                st.image(chart_png, use_container_width=True)
            excel_bytes = msg.get("excel_bytes")
            if excel_bytes:
                st.download_button(
                    label="Download data as Excel",
                    data=excel_bytes,
                    file_name=f"merchandising_result_{idx}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"xl_dl_{idx}",
                )
            sql = msg.get("sql")
            if sql:
                with st.expander("Show SQL", expanded=False):
                    st.code(sql, language="sql")
            viz_code = msg.get("viz_code")
            if viz_code:
                with st.expander("Show chart code", expanded=False):
                    st.code(viz_code, language="python")
            steps = msg.get("steps")
            if steps:
                with st.expander("How I got this answer (agent trace)", expanded=False):
                    for s in steps:
                        st.markdown(f"- **{s['agent']}** · _{s['status']}_ · {s['summary']}")

    # Input
    if prompt := st.chat_input("Ask about non-purchase feedback..."):
        _submit_user_message(prompt)
        st.rerun()


def _submit_user_message(prompt: str) -> None:
    """Append the user message, call the agentic backend, append the response."""
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.spinner("Working through the agent pipeline..."):
        resp = backend.chat_with_agent(
            prompt,
            history=st.session_state.messages,
            filters=st.session_state.filters,
        )
    st.session_state.messages.append({
        "role": "assistant",
        "content": resp.text,
        "steps": getattr(resp, "tool_calls", []) or [],
        "chart_png": getattr(resp, "chart_png", None),
        "sql": getattr(resp, "sql", None),
        "viz_code": getattr(resp, "viz_code", None),
        "excel_bytes": getattr(resp, "excel_bytes", None),
    })


# ---------------------------------------------------------------------------
# Recommendations view
# ---------------------------------------------------------------------------

def render_report_view() -> None:
    st.markdown("## Full Recommendations Report")
    st.caption(
        "Top focus areas for the next two months, computed across the selected "
        "stores. Use the sidebar filters to narrow scope."
    )

    with st.spinner("Crunching feedback data..."):
        try:
            report = backend.generate_full_recommendations(st.session_state.filters)
        except Exception as e:
            st.error(f"Could not generate the report: {e}")
            st.info(
                "Make sure MySQL is running, the MYSQL_PASSWORD env var is set, "
                "and the `non_purchasers_feedback` table is populated."
            )
            return

    if report.get("empty"):
        st.warning("No feedback rows matched the current filters.")
        return

    # ---- Headline metrics ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total feedbacks", f"{report['total_rows']:,}")
    c2.metric("Stores analyzed", len(report["by_store"]))
    c3.metric("Top issue",   report["top_issue"])
    c4.metric("Top product", report["top_product"])

    st.divider()

    # ---- Top 10 focus areas ----
    st.markdown("### Top 10 focus areas")
    st.caption("Ranked by evidence count. Click any item to expand.")
    for i, item in enumerate(report["top_10"], start=1):
        with st.expander(
            f"**{i}. {item['title']}**  ·  {item['evidence_count']} feedbacks",
            expanded=(i <= 3),
        ):
            st.markdown(
                f'<span class="tag">Store {item["store"]}</span>'
                f'<span class="tag">{item["product"]}</span>'
                f'<span class="tag">{item["topic"]}</span>',
                unsafe_allow_html=True,
            )
            st.markdown(f"**Why it matters.** {item['why']}")
            st.markdown(f"**Recommended action.** {item['action']}")

    st.divider()

    # ---- Per-store deep dive ----
    st.markdown("### Per-store deep dive")
    stores = list(report["by_store"].keys())
    if stores:
        tabs = st.tabs(stores)
        for tab, store in zip(tabs, stores):
            data = report["by_store"][store]
            with tab:
                left, middle, right = st.columns([2, 1, 2])

                with left:
                    st.markdown(f"**Top 3 issues at {store}**")
                    for issue in data["top_issues"]:
                        st.markdown(
                            f"- **{issue['topic']}** — {issue['count']} feedbacks "
                            f"({issue['percent']:.0f}% of {store}'s non-purchases)"
                        )

                with middle:
                    st.markdown("**Most-asked product**")
                    st.markdown(f"### {data['top_product']}")
                    st.caption(
                        f"{data['top_product_count']} feedbacks · "
                        f"{data['top_product_count']/data['total']*100:.0f}% of "
                        f"{store}'s volume"
                    )

                with right:
                    coverage = data.get("coverage_pct", 0)
                    st.markdown(
                        f"**What customers wanted "
                        f"(top reasons, ~{coverage:.0f}% coverage)**"
                    )
                    st.caption(
                        "Click each reason for product + attribute drill-down."
                    )
                    for r in data["top_reasons"]:
                        label = (
                            f"{r['topic']} · {r['count']} ({r['percent']:.0f}%)"
                        )
                        with st.popover(label, use_container_width=True):
                            st.markdown(f"#### {r['topic']} at {store}")
                            st.caption(
                                f"{r['count']} feedbacks · "
                                f"{r['percent']:.0f}% of {store}'s non-purchases"
                            )

                            st.markdown("**Affected products**")
                            for p in r["products"]:
                                st.markdown(
                                    f"- **{p['product']}** — {p['count']} feedbacks "
                                    f"({p['percent']:.0f}% of this reason)"
                                )

                            if r["attributes"]:
                                st.markdown(
                                    "**Specific attributes customers asked for**"
                                )
                                for a in r["attributes"]:
                                    st.markdown(
                                        f"- **{a['product']}** · "
                                        f"_{a['attribute_value']}_ — "
                                        f"{a['count']} requests"
                                    )
                            else:
                                st.caption(
                                    "_This reason doesn't carry specific "
                                    "attribute values (e.g. Stock Unavailable "
                                    "or Sales Service rows are not tied to a "
                                    "particular design / size / color)._"
                                )

    st.divider()

    # ---- Distribution charts ----
    st.markdown("### Distributions")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Reasons for non-purchase**")
        topic_series = pd.Series(report["topic_distribution"]).sort_values(ascending=True)
        st.bar_chart(topic_series, horizontal=True, height=320)
    with c2:
        st.markdown("**Most looked-for products**")
        product_series = pd.Series(report["product_distribution"]).sort_values(ascending=True)
        st.bar_chart(product_series, horizontal=True, height=320)

    st.divider()

    # ---- Downloads (stubs for now) ----
    st.markdown("### Downloads")
    c1, c2 = st.columns(2)
    c1.download_button(
        "Download PPT  (coming soon)",
        data=b"",
        file_name="merchandising_report.pptx",
        disabled=True,
        use_container_width=True,
    )
    c2.download_button(
        "Download Excel  (coming soon)",
        data=b"",
        file_name="merchandising_report.xlsx",
        disabled=True,
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if st.session_state.view == "chat":
    render_chat_view()
else:
    render_report_view()
