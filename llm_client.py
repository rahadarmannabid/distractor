"""OpenAI + OpenRouter + Groq clients and the per-quiz model catalog.

Instructors pick one model per quiz (`llm_model`). Generation uses that model for
completions, agent turns, and bug planting. Models are routed by `provider`:
OpenAI, OpenRouter (`OPENROUTER_BASE_URL`), or Groq (`GROQ_BASE_URL`).
"""

from __future__ import annotations

import contextvars
import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI, OpenAI

# Shown in the instructor quiz form. `id` is what we send to the provider.
# Prices are USD per 1M tokens; update when rates change.
AVAILABLE_MODELS: List[Dict[str, Any]] = [
    {
        "id": "nvidia/nemotron-3.5-lightning:free",
        "label": "NVIDIA: Nemotron 3.5 Lightning (free)",
        "provider": "openrouter",
        "input_per_mtok": 0.0,
        "output_per_mtok": 0.0,
    },
    {
        "id": "openai/gpt-oss-20b",
        "label": "Groq: GPT OSS 20B",
        "provider": "groq",
        "input_per_mtok": 0.075,
        "output_per_mtok": 0.30,
    },
    {
        "id": "openai/gpt-oss-120b",
        "label": "Groq: GPT OSS 120B",
        "provider": "groq",
        "input_per_mtok": 0.15,
        "output_per_mtok": 0.60,
    },
    {
        "id": "qwen/qwen3.8-27b",
        "label": "Groq: Qwen3.8 27B",
        "provider": "groq",
        "input_per_mtok": 0.29,
        "output_per_mtok": 0.59,
    },
    {
        "id": "gpt-5-nano",
        "label": "OpenAI: gpt-5-nano",
        "provider": "openai",
        "input_per_mtok": 0.05,
        "output_per_mtok": 0.40,
    },
    {
        "id": "gpt-6-luna",
        "label": "GPT-6 Luna",
        "provider": "openai",
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.50,
    },
    {
        "id": "gpt-4.1-mini",
        "label": "OpenAI: gpt-4.1-mini",
        "provider": "openai",
        "input_per_mtok": 0.40,
        "output_per_mtok": 1.60,
    },
]


def format_model_price(model: Dict[str, Any]) -> str:
    """Short price string for dropdowns, e.g. 'in $0.05 / out $0.40 per 1M'."""
    inp = float(model.get("input_per_mtok", 0) or 0)
    out = float(model.get("output_per_mtok", 0) or 0)
    if inp == 0 and out == 0:
        return "free (in $0 / out $0 per 1M tokens)"

    def _fmt(v: float) -> str:
        return f"${v:.2f}" if v >= 0.01 else f"${v:.3f}".rstrip("0").rstrip(".")

    return f"in {_fmt(inp)} / out {_fmt(out)} per 1M tokens"


def models_for_instructor() -> List[Dict[str, Any]]:
    """Catalog with display fields for the quiz form (sorted cheapest → pricier)."""
    rows = []
    for m in AVAILABLE_MODELS:
        row = dict(m)
        row["price_label"] = format_model_price(m)
        row["option_label"] = f"{m['label']} — {row['price_label']}"
        rows.append(row)
    rows.sort(
        key=lambda r: (
            float(r.get("input_per_mtok") or 0) + float(r.get("output_per_mtok") or 0),
            float(r.get("input_per_mtok") or 0),
        )
    )
    return rows


_MODEL_IDS = {m["id"] for m in AVAILABLE_MODELS}
_MODEL_PROVIDER = {m["id"]: m["provider"] for m in AVAILABLE_MODELS}

_active_model: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "active_llm_model", default=None
)

_openai_sync: Optional[OpenAI] = None
_openai_async: Optional[AsyncOpenAI] = None
_openrouter_sync: Optional[OpenAI] = None
_openrouter_async: Optional[AsyncOpenAI] = None
_groq_sync: Optional[OpenAI] = None
_groq_async: Optional[AsyncOpenAI] = None

# Populated by init_clients() after dotenv is loaded.
OPENROUTER_API_KEY = ""
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_HTTP_REFERER = ""
OPENROUTER_APP_NAME = "CodeOracle"
GROQ_API_KEY = ""
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENAI_API_KEY = ""
DEFAULT_QUIZ_LLM_MODEL = "gpt-6-luna"


def model_provider(model: str) -> str:
    """Return 'openai' | 'openrouter' | 'groq' for a model id."""
    m = (model or "").strip()
    if m in _MODEL_PROVIDER:
        return _MODEL_PROVIDER[m]
    if m.endswith(":free") or m.startswith(
        ("nvidia/", "meta-llama/", "google/", "deepseek/", "anthropic/")
    ):
        return "openrouter"
    if m.startswith(("llama-", "mixtral-", "gemma", "whisper-", "openai/gpt-oss", "qwen/")):
        return "groq"
    return "openai"


def is_openrouter_model(model: str) -> bool:
    return model_provider(model) == "openrouter"


def is_groq_model(model: str) -> bool:
    return model_provider(model) == "groq"


def uses_responses_api(model: str) -> bool:
    """OpenAI Responses API (reasoning + tools) — OpenAI gpt-5/6/o-series only."""
    if model_provider(model) != "openai":
        return False
    m = (model or "").lower()
    return m.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def clean_llm_model(raw: Any) -> str:
    """Validate an instructor-selected model id; fall back to the quiz default."""
    mid = str(raw or "").strip()
    if mid in _MODEL_IDS:
        return mid
    # Allow env defaults that are not in the short catalog (forward-compatible).
    if mid and len(mid) < 120 and all(c.isalnum() or c in "-_./:" for c in mid):
        return mid
    return DEFAULT_QUIZ_LLM_MODEL if DEFAULT_QUIZ_LLM_MODEL in _MODEL_IDS else (
        AVAILABLE_MODELS[0]["id"] if AVAILABLE_MODELS else "gpt-5-nano"
    )


def set_active_model(model: Optional[str]) -> contextvars.Token:
    return _active_model.set((model or "").strip() or None)


def reset_active_model(token: contextvars.Token) -> None:
    _active_model.reset(token)


def get_active_model(fallback: str) -> str:
    return _active_model.get() or fallback


def _openrouter_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME
    return headers


def init_clients() -> None:
    """Create shared clients once at startup. Needs at least one provider API key."""
    global _openai_sync, _openai_async, _openrouter_sync, _openrouter_async, _groq_sync, _groq_async
    global OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_HTTP_REFERER, OPENROUTER_APP_NAME
    global GROQ_API_KEY, GROQ_BASE_URL, OPENAI_API_KEY, DEFAULT_QUIZ_LLM_MODEL

    OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
    OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    OPENROUTER_BASE_URL = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    OPENROUTER_HTTP_REFERER = (
        os.getenv("OPENROUTER_HTTP_REFERER") or os.getenv("APP_PUBLIC_URL") or ""
    ).strip()
    OPENROUTER_APP_NAME = (
        os.getenv("OPENROUTER_APP_NAME") or os.getenv("APP_TITLE") or "CodeOracle"
    ).strip()
    GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
    GROQ_BASE_URL = (os.getenv("GROQ_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
    DEFAULT_QUIZ_LLM_MODEL = (
        os.getenv("DEFAULT_QUIZ_LLM_MODEL")
        or (
            "nvidia/nemotron-3.5-lightning:free"
            if OPENROUTER_API_KEY
            else ("openai/gpt-oss-20b" if GROQ_API_KEY else "gpt-6-luna")
        )
    ).strip()

    if not OPENAI_API_KEY and not OPENROUTER_API_KEY and not GROQ_API_KEY:
        raise ValueError(
            "Set OPENAI_API_KEY, OPENROUTER_API_KEY, and/or GROQ_API_KEY in the environment (.env)."
        )
    if OPENAI_API_KEY:
        _openai_sync = OpenAI(api_key=OPENAI_API_KEY)
        _openai_async = AsyncOpenAI(api_key=OPENAI_API_KEY)
    if OPENROUTER_API_KEY:
        _openrouter_sync = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            default_headers=_openrouter_headers() or None,
        )
        _openrouter_async = AsyncOpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url=OPENROUTER_BASE_URL,
            default_headers=_openrouter_headers() or None,
        )
    if GROQ_API_KEY:
        _groq_sync = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        _groq_async = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)


def get_sync_client(model: Optional[str] = None) -> OpenAI:
    m = model or get_active_model(DEFAULT_QUIZ_LLM_MODEL)
    provider = model_provider(m)
    if provider == "openrouter":
        if _openrouter_sync is None:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        return _openrouter_sync
    if provider == "groq":
        if _groq_sync is None:
            raise RuntimeError("GROQ_API_KEY is not configured")
        return _groq_sync
    if _openai_sync is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return _openai_sync


def get_async_client(model: Optional[str] = None) -> AsyncOpenAI:
    m = model or get_active_model(DEFAULT_QUIZ_LLM_MODEL)
    provider = model_provider(m)
    if provider == "openrouter":
        if _openrouter_async is None:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        return _openrouter_async
    if provider == "groq":
        if _groq_async is None:
            raise RuntimeError("GROQ_API_KEY is not configured")
        return _groq_async
    if _openai_async is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return _openai_async


def openai_configured() -> bool:
    return bool(OPENAI_API_KEY)


def openrouter_configured() -> bool:
    return bool(OPENROUTER_API_KEY)


def groq_configured() -> bool:
    return bool(GROQ_API_KEY)
