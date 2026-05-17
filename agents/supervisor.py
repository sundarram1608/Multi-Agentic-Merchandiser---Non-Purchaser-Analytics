"""
agents/supervisor.py
--------------------
The Supervisor only activates when a retry loop in the Orchestrator has
been exhausted. It sees the full trace and decides what to do next.

Returns one of four actions:
  abort_gracefully  — tell the user we can't answer; ship the message
  retry_with_strategy — propose a fundamentally different SQL approach;
                        Orchestrator runs the Coder once more with hint
  ship_partial      — ship the best result with a caveat
  ask_user          — return a clarifying question
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agents.llm import call_llm_json, load_prompt, MODEL_HAIKU


_SYSTEM_PROMPT: str | None = None

def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = load_prompt("supervisor")
    return _SYSTEM_PROMPT


ALLOWED_ACTIONS = {"abort_gracefully", "retry_with_strategy", "ship_partial", "ask_user"}


@dataclass
class SupervisorDecision:
    action: str
    message: str
    strategy_hint: str = ""


def decide(user_question: str, trace: list[dict]) -> SupervisorDecision:
    """
    `trace` is a list of step dicts (the Orchestrator's step log). Each
    dict has at minimum {"agent": str, "summary": str} and optionally
    {"sql": ..., "error": ..., "feedback": ..., "rows": ...}.
    """
    user_block = (
        f"## User question\n{user_question.strip()}\n\n"
        f"## Trace so far\n"
        + json.dumps(trace, default=str, indent=2)
        + "\n\nReturn the JSON decision."
    )

    raw = call_llm_json(
        system=_system_prompt(),
        user=user_block,
        model=MODEL_HAIKU,
        temperature=0.0,
        max_tokens=400,
    )

    action = raw.get("action")
    if action not in ALLOWED_ACTIONS:
        # Fail safe: ship partial with generic caveat
        return SupervisorDecision(
            action="ship_partial",
            message="I tried but couldn't perfectly answer this. Here's the closest I got.",
        )

    return SupervisorDecision(
        action=action,
        message=str(raw.get("message", "")).strip(),
        strategy_hint=str(raw.get("strategy_hint", "")).strip(),
    )
