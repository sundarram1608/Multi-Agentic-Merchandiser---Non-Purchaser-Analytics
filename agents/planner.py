"""
agents/planner.py
-----------------
Classifies the user turn into sql / direct / clarify AND emits a
self-contained `resolved_question` that bakes in chat-history context.
Downstream agents (Coder, Output Reviewer, Writer, Viz Coder) read the
resolved_question, never the raw user message.

Two distinct flavors of follow-up the prompt teaches the Planner to
recognize (see `agents/prompts/planner.txt`):

  - **Vocabulary resolution** — colloquial "stock more / what's missing"
    becomes an `attribute_value`-aggregation question, NOT a literal
    filter on inferred_topic='Stock Unavailable'.
  - **Hierarchical-analysis follow-up** (Case H vs Case V) — when prior
    turns built a "discover then drill down" chain and the user changes
    scope ("same for X1 and X2"), distinguishes whether to re-run the
    whole pattern (values were discovered) or carry forward specific
    values (user-named them directly).

Sees the LAST 8 messages of chat history. The history slice
`history[-8:]` in `_format_history` is the single knob that governs
the system's memory; see the long comment block above that function
for the rationale.

Model: Haiku 4.5 (routing is a simpler task than SQL generation; Haiku
handles it reliably).
"""

from __future__ import annotations

from agents.llm import call_llm_json, load_prompt, MODEL_HAIKU
from agents.schemas import PlannerOutput


ALLOWED_PATHS = {"sql", "clarify", "direct"}


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("planner")
    return _SYSTEM_PROMPT


# -----------------------------------------------------------------------------
# ## Why an 8-message look-back?
#
# The slice `history[-8:]` below caps how much prior chat gets fed into
# the Planner's prompt. The Planner is the ONLY agent that sees chat
# history — every downstream agent reads `resolved_question` instead —
# so this single number governs the system's entire memory of the
# conversation.
#
# Note that `streamlit_app._submit_user_message` appends the current
# user turn to `history` BEFORE calling the orchestrator, so the slice
# already includes today's question. Effective look-back is 7 prior
# messages plus the current one (which also appears separately in the
# "## Current user turn" block of the planner prompt — the latest
# message shows up twice). Seven prior messages ≈ 3–4 turns of
# back-and-forth.
#
# ### Why 8, not 4 or 20?
#
# 1. **Reference-resolution payoff drops sharply with depth.** In real
#    chat usage, follow-ups like "now show me X4", "the same",
#    "give me a chart of that" almost always point back 1–2 turns,
#    occasionally 3. Going past 4 prior turns rarely catches a
#    reference that 8 misses, but always costs more tokens.
#
# 2. **Planner input budget needs to stay small.** `call_llm_json`
#    runs at `max_tokens=400` for the output (see `plan()` below).
#    Each chat turn's assistant message can be 200–400 tokens
#    (grounded multi-sentence answers). 8 messages keeps the Planner
#    prompt around 1–2K tokens of history plus the 107-line system
#    prompt — fast and cheap on Haiku.
#
# 3. **Per-turn cost stays roughly flat as the session grows.** The
#    Planner is called on every turn, and the history slice is sent
#    verbatim each time. Capping at 8 prevents the O(n²) total token
#    cost growth across a long workday session that an unbounded
#    slice would create.
#
# 4. **8 = 2³, four user/assistant pairs** — a generous-but-not-
#    excessive round number that's hard to argue with absent empirical
#    tuning data.
#
# If you ever want to change this, swap the literal `8` below or
# promote it to a module-level `MAX_HISTORY_MESSAGES = 8` constant
# so this rationale and the value live in one place.
# -----------------------------------------------------------------------------
def _format_history(history: list[dict] | None) -> str:
    if not history:
        return "(no prior messages)"
    lines = [f"{m['role']}: {m['content']}" for m in history[-8:]]
    return "\n".join(lines)


def plan(user_message: str, history: list[dict] | None = None) -> PlannerOutput:
    user_block = (
        f"## Chat history\n{_format_history(history)}\n\n"
        f"## Current user turn\n{user_message}\n\n"
        f"Return the JSON object with path, plan, and resolved_question."
    )

    raw = call_llm_json(
        system=_system_prompt(),
        user=user_block,
        model=MODEL_HAIKU,
        temperature=0.0,
        max_tokens=400,
    )

    path = raw.get("path")
    if path not in ALLOWED_PATHS:
        raise ValueError(f"Planner returned invalid path: {path!r}")

    resolved = str(raw.get("resolved_question", "")).strip() or user_message.strip()

    return PlannerOutput(
        path=path,
        plan=str(raw.get("plan", "")).strip(),
        resolved_question=resolved,
    )
