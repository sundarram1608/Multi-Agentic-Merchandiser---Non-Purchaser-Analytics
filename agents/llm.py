"""
agents/llm.py
-------------
Thin wrapper around aisuite so every agent calls the LLM the same way.
Centralizing this means:

  - We swap models / providers in one place.
  - JSON-output handling and defensive markdown-fence stripping happen once.
  - Prompt files are loaded from a single canonical location.

No agent should `import aisuite` directly — they all go through here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


# -----------------------------------------------------------------------------
# ## Bootstrap — load .env once at import time
#
# Every agent ends up importing this module, so loading the project's .env
# here means MYSQL_*, ANTHROPIC_API_KEY, etc. are always available without
# the caller having to remember to `export` them.
# -----------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


import aisuite as ai  # noqa: E402  (after dotenv load on purpose)


# -----------------------------------------------------------------------------
# ## Model registry
#
# Keep model identifiers in one place so we can swap (e.g. upgrade the
# Chart-Reviewer to Sonnet) without hunting through every agent.
# -----------------------------------------------------------------------------
MODEL_HAIKU = "anthropic:claude-haiku-4-5-20251001"   # default for most agents
MODEL_SONNET = "anthropic:claude-sonnet-4-6"          # Coder + Code Reviewer — the SQL-quality bottleneck


# -----------------------------------------------------------------------------
# ## Lazy aisuite client
#
# aisuite reads ANTHROPIC_API_KEY from env on first use. We construct one
# Client at first call and reuse it, which avoids re-creating HTTP pools
# on every agent invocation.
# -----------------------------------------------------------------------------
_client: ai.Client | None = None

def get_client() -> ai.Client:
    global _client
    if _client is None:
        _client = ai.Client()
    return _client


# -----------------------------------------------------------------------------
# ## call_llm — plain text completion
#
# Used when the agent expects free-form text back (e.g. the Writer). For
# JSON output (which most agents need), use `call_llm_json` instead.
# -----------------------------------------------------------------------------
def call_llm(
    *,
    system: str,
    user: str,
    model: str = MODEL_HAIKU,
    temperature: float = 0.0,
    max_tokens: int = 800,
) -> str:
    resp = get_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


# -----------------------------------------------------------------------------
# ## call_llm_json — completion + JSON parsing in one shot
#
# Strips an optional ```json ... ``` markdown fence defensively even though
# our prompts forbid fences. If the model returns malformed JSON, this
# raises json.JSONDecodeError, which the agent catches and treats as a
# transient failure (retry or bubble up).
# -----------------------------------------------------------------------------
def call_llm_json(
    *,
    system: str,
    user: str,
    model: str = MODEL_HAIKU,
    temperature: float = 0.0,
    max_tokens: int = 800,
) -> dict:
    text = call_llm(
        system=system, user=user,
        model=model, temperature=temperature, max_tokens=max_tokens,
    )
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# -----------------------------------------------------------------------------
# ## call_llm_vision — multimodal (text + image) call
#
# aisuite's multimodal handling for Anthropic is patchy, so this path uses
# the native `anthropic` SDK (already a transitive dep of aisuite[anthropic]).
# Used by the Viz Reviewer to inspect the rendered chart.
# -----------------------------------------------------------------------------
import base64 as _b64

def call_llm_vision(
    *,
    system: str,
    user_text: str,
    image_png: bytes,
    model: str = "claude-haiku-4-5-20251001",   # native model name (no "anthropic:" prefix)
    temperature: float = 0.0,
    max_tokens: int = 600,
) -> str:
    import anthropic  # native SDK
    client = anthropic.Anthropic()
    img_b64 = _b64.b64encode(image_png).decode("ascii")
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": user_text},
            ],
        }],
    )
    return resp.content[0].text.strip()


def call_llm_vision_json(
    *,
    system: str,
    user_text: str,
    image_png: bytes,
    model: str = "claude-haiku-4-5-20251001",
    temperature: float = 0.0,
    max_tokens: int = 600,
) -> dict:
    text = call_llm_vision(
        system=system, user_text=user_text, image_png=image_png,
        model=model, temperature=temperature, max_tokens=max_tokens,
    )
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# -----------------------------------------------------------------------------
# ## load_prompt — pull a system prompt from agents/prompts/<name>.txt
#
# Keeping prompts as plain text files (not as Python strings) means:
#   1. They're editable without touching code.
#   2. Diffs are clean — you see the prompt change, not escape-character noise.
#   3. We can later add a prompt-versioning / A-B testing layer here.
# -----------------------------------------------------------------------------
_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

def load_prompt(name: str) -> str:
    path = _PROMPT_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")
