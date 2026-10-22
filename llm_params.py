"""Chat Completions arguments that work for both model families.

gpt-5 and o-series models are reasoning models: they reject `temperature` and `max_tokens`
(they take `max_completion_tokens`, which also covers the hidden reasoning tokens) and accept
`reasoning_effort`. gpt-4.x models take the classic arguments.
"""

import os
from typing import Any, Dict, Optional

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")
# "minimal" keeps gpt-5 latency close to gpt-4.1 for short, interactive calls.
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "minimal")


def is_reasoning_model(model: str) -> bool:
    m = model.lower()
    return m.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def chat_args(
    model: str,
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Keyword arguments for client.chat.completions.create(model=..., **chat_args(...))."""
    args: Dict[str, Any] = {"model": model}
    if is_reasoning_model(model):
        # Sent as extra_body: the pinned openai SDK (1.35) predates these parameters.
        effort = reasoning_effort or REASONING_EFFORT
        if effort == "minimal" and model.lower().startswith("gpt-6"):
            # gpt-6 models have no "minimal"; "none" is their lowest setting.
            effort = "none"
        extra: Dict[str, Any] = {"reasoning_effort": effort}
        if max_tokens:
            # Room for the reasoning tokens on top of the visible answer.
            extra["max_completion_tokens"] = max_tokens + 2000
        args["extra_body"] = extra
    else:
        if temperature is not None:
            args["temperature"] = temperature
        if max_tokens:
            args["max_tokens"] = max_tokens
    return args
