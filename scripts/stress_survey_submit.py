#!/usr/bin/env python3
"""
Stress / timing helper for POST /submit-survey with large open-ended responses + AI history.

Usage (requires a running server):
  python scripts/stress_survey_submit.py --base-url http://127.0.0.1:8000

Uses env or defaults for a minimal valid session; override with flags if needed.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import uuid


def _post_json(url: str, payload: dict, timeout: float = 120.0) -> tuple[int, float]:
    body = json.dumps(payload).encode("utf-8")
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        dt = time.perf_counter() - t0
        return resp.status, dt


def main() -> None:
    p = argparse.ArgumentParser(description="Stress test /submit-survey payload size and latency")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="API origin")
    p.add_argument("--user-id", default="stress_user")
    p.add_argument("--session-id", default="")
    p.add_argument("--assignment-key", default="quiz_stress_test")
    p.add_argument("--response-chars", type=int, default=8000, help="Chars per open-ended answer")
    p.add_argument("--ai-history-entries", type=int, default=80, help="Synthetic ai_guess_history rows")
    args = p.parse_args()

    sid = args.session_id or f"stress_{uuid.uuid4().hex[:12]}"
    chunk = "Lorem ipsum dolor sit amet. " * 50
    big_text = (chunk * ((args.response_chars // len(chunk)) + 1))[: args.response_chars]

    ai_hist = []
    for i in range(args.ai_history_entries):
        ai_hist.append(
            {
                "timestamp": f"2026-01-01T10:{i:02d}:00",
                "problem_analysis": "analysis " * 200,
                "next_line_suggestion": "suggestion " * 200,
            }
        )

    payload = {
        "user_id": args.user_id,
        "session_id": sid,
        "assignment_key": args.assignment_key,
        "survey_type": "periodic",
        "analysis_count": 12,
        "timestamp": "2026-04-01T12:00:00",
        "responses": [
            {"question": "Q1 open", "response": big_text},
            {"question": "Q2 open", "response": big_text},
            {"question": "Q3 open", "response": big_text},
        ],
        "ai_history_context": {
            "condition": 2,
            "ai_guess_history": ai_hist,
        },
    }

    raw_len = len(json.dumps(payload))
    url = args.base_url.rstrip("/") + "/submit-survey"
    print(f"POST {url}")
    print(f"Approx JSON body size: {raw_len // 1024} KiB (client; server compacts AI history)")

    try:
        status, elapsed = _post_json(url, payload)
        print(f"OK status={status} time={elapsed:.3f}s")
    except Exception as e:
        print(f"FAILED: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
