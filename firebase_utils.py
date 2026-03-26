"""Small helpers for Firebase Realtime Database typing (Pyright-safe)."""

from typing import Any, Dict, Optional


def fb_dict(val: object) -> Optional[Dict[str, Any]]:
    """Coerce RTDB .get() result to dict; None or non-dict snapshots become None."""
    if isinstance(val, dict):
        return val
    return None
