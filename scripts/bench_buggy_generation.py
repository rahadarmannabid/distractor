#!/usr/bin/env python3
"""Benchmark how long buggy inline-suggestion planting takes.

Uses a fixed correct next-line fixture so timings measure bug planting only
(not correct-completion latency).

Usage (from repo root):
  python scripts/bench_buggy_generation.py
  python scripts/bench_buggy_generation.py --model gpt-6-luna --runs 5
  python scripts/bench_buggy_generation.py --model gpt-5-nano --runs 3 --count 1
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import bug_injection  # noqa: E402
import llm_client  # noqa: E402

# Fixed student cursor: correct next line is known so we only time bug planting.
FIXTURE = {
    "language": "python",
    "filename": "main.py",
    "problem": "Write a function that returns the sum of two integers.",
    "prefix": "def add(a, b):\n",
    "suffix": "\n\nprint(add(2, 3))\n",
    "correct": "    return a + b\n",
}


def _pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


async def _one_shot(client, skills: List[Dict[str, Any]], count: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    bugs, dropped = await bug_injection.write_buggy_suggestions(
        client,
        language=FIXTURE["language"],
        prefix=FIXTURE["prefix"],
        suffix=FIXTURE["suffix"],
        correct=FIXTURE["correct"],
        problem=FIXTURE["problem"],
        skills=skills,
        count=count,
        others=[],
    )
    ms = (time.perf_counter() - t0) * 1000
    return {
        "ms": ms,
        "ok": len(bugs) >= count,
        "n_bugs": len(bugs),
        "dropped": dropped,
        "types": [b["bug_skill"]["id"] for b in bugs],
        "buggy": [b["buggy"] for b in bugs],
    }


async def _full(client, skills: List[Dict[str, Any]], count: int) -> Dict[str, Any]:
    t0 = time.perf_counter()
    result = await bug_injection.make_buggy_completions(
        client,
        language=FIXTURE["language"],
        prefix=FIXTURE["prefix"],
        suffix=FIXTURE["suffix"],
        correct=FIXTURE["correct"],
        problem=FIXTURE["problem"],
        skills=skills,
        count=count,
        recent=[],
    )
    ms = (time.perf_counter() - t0) * 1000
    bugs = result.get("bugs") or []
    choice = result.get("choice") or {}
    return {
        "ms": ms,
        "ok": len(bugs) >= count,
        "n_bugs": len(bugs),
        "no_bug": result.get("no_bug", ""),
        "attempts": choice.get("attempts"),
        "dropped": choice.get("dropped") or [],
        "types": [b["bug_skill"]["id"] for b in bugs],
        "buggy": [b["buggy"] for b in bugs],
    }


def _summarize(label: str, rows: List[Dict[str, Any]]) -> None:
    times = sorted(r["ms"] for r in rows)
    ok = sum(1 for r in rows if r["ok"])
    print(f"\n=== {label} ({ok}/{len(rows)} ok) ===")
    if not times:
        return
    mean = statistics.fmean(times)
    print(
        f"ms: mean={mean:.0f}  p50={_pct(times, 0.5):.0f}  "
        f"p95={_pct(times, 0.95):.0f}  min={times[0]:.0f}  max={times[-1]:.0f}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6-luna", help="Quiz/LLM model id (default: gpt-6-luna)")
    parser.add_argument("--runs", type=int, default=5, help="How many timed trials")
    parser.add_argument("--count", type=int, default=1, help="How many buggy options to request")
    parser.add_argument(
        "--mode",
        choices=("one_shot", "full", "both"),
        default="both",
        help="one_shot=write_buggy_suggestions once; full=make_buggy_completions (retries)",
    )
    args = parser.parse_args()

    llm_client.init_clients()
    model = llm_client.clean_llm_model(args.model)
    token = llm_client.set_active_model(model)
    client = llm_client.get_async_client(model)
    skills = [s for s in bug_injection.default_bug_skills() if s.get("enabled", True)]
    if not skills:
        print("No bug skills found in bug_skills/", file=sys.stderr)
        return 1

    print(f"model={model}  provider={llm_client.model_provider(model)}")
    print(f"skills={len(skills)}  count={args.count}  runs={args.runs}")
    print(f"correct={FIXTURE['correct']!r}")
    print(f"BUG_ATTEMPTS={bug_injection.BUG_ATTEMPTS}")

    one_rows: List[Dict[str, Any]] = []
    full_rows: List[Dict[str, Any]] = []

    try:
        for i in range(1, args.runs + 1):
            if args.mode in ("one_shot", "both"):
                row = await _one_shot(client, skills, args.count)
                one_rows.append(row)
                preview = (row["buggy"][0].replace("\n", "\\n") if row["buggy"] else "")[:60]
                print(
                    f"[{i}] one_shot  {row['ms']:7.0f} ms  ok={row['ok']}  "
                    f"dropped={row['dropped'] or '-'}  types={row['types'] or '-'}  "
                    f"buggy={preview!r}"
                )
            if args.mode in ("full", "both"):
                row = await _full(client, skills, args.count)
                full_rows.append(row)
                preview = (row["buggy"][0].replace("\n", "\\n") if row["buggy"] else "")[:60]
                print(
                    f"[{i}] full      {row['ms']:7.0f} ms  ok={row['ok']}  "
                    f"attempts={row.get('attempts')}  dropped={row['dropped'] or '-'}  "
                    f"types={row['types'] or '-'}  buggy={preview!r}"
                )
    finally:
        llm_client.reset_active_model(token)

    if one_rows:
        _summarize("one_shot (write_buggy_suggestions ×1)", one_rows)
    if full_rows:
        _summarize(f"full (make_buggy_completions, up to {bug_injection.BUG_ATTEMPTS} attempts)", full_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
