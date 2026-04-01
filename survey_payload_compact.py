"""Shrink AI history embedded in survey POST/Firebase payloads (keeps last N analyses, truncates long strings)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _trunc(s: Any, max_chars: int) -> Any:
    if s is None:
        return None
    if not isinstance(s, str):
        return s
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def _compact_entry(row: Any, max_chars: int) -> Any:
    if not isinstance(row, dict):
        return row
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k in ("problem_analysis", "next_line_suggestion", "full_code_suggestion", "user_code_snapshot"):
            out[k] = _trunc(v, max_chars)
        else:
            out[k] = v
    return out


def _compact_list(rows: Any, max_entries: int, max_chars: int) -> List[Any]:
    if not isinstance(rows, list) or not rows:
        return []
    tail = rows[-max_entries:] if len(rows) > max_entries else rows
    return [_compact_entry(x, max_chars) for x in tail]


def compact_survey_ai_history(
    ctx: Optional[Dict[str, Any]],
    *,
    max_entries: int = 24,
    max_chars: int = 6000,
) -> Optional[Dict[str, Any]]:
    """Return a smaller copy safe for RTDB and fast JSON; preserves counts on the root dict."""
    if ctx is None or not isinstance(ctx, dict):
        return ctx
    out = dict(ctx)
    for key in ("ai_guess_history", "correct_description_history", "suggestions_only_history"):
        if key in out and out[key] is not None:
            out[key] = _compact_list(out[key], max_entries, max_chars)
    if "timestamp_range" in out and isinstance(out["timestamp_range"], dict):
        tr = dict(out["timestamp_range"])
        for tk in ("start", "survey_time", "end"):
            if tk in tr:
                tr[tk] = _trunc(tr[tk], 80)
        out["timestamp_range"] = tr
    out["ai_history_compact"] = True
    out["ai_history_max_entries"] = max_entries
    out["ai_history_max_chars"] = max_chars
    return out
