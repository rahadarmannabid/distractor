#!/usr/bin/env python3
"""
Concurrent stress: N students POST /quiz/join then POST /submit-survey (confirmation JSON).

Usage:
  python scripts/stress_quiz_survey_100.py --base-url http://127.0.0.1:8000 --access-code fmbk2Xuj -n 100
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import List

import httpx


@dataclass
class OneResult:
    ok: bool
    join_ms: float
    submit_ms: float
    total_ms: float
    error: str = ""


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


async def one_student(
    client: httpx.AsyncClient,
    base: str,
    access_code: str,
    idx: int,
) -> OneResult:
    user_id = f"stress_{idx}_{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    join_ms = 0.0
    submit_ms = 0.0
    try:
        t_j0 = time.perf_counter()
        jr = await client.post(
            f"{base}/quiz/join",
            json={
                "access_code": access_code,
                "user_id": user_id,
                "programming_language": "python",
            },
            timeout=120.0,
        )
        join_ms = (time.perf_counter() - t_j0) * 1000.0
        if jr.status_code != 200:
            return OneResult(
                False,
                join_ms,
                0.0,
                (time.perf_counter() - t0) * 1000.0,
                error=f"join HTTP {jr.status_code}: {jr.text[:200]}",
            )
        data = jr.json()
        if data.get("status") == "already_completed":
            return OneResult(
                False,
                join_ms,
                0.0,
                (time.perf_counter() - t0) * 1000.0,
                error="join: already_completed",
            )
        if data.get("status") != "success":
            return OneResult(
                False,
                join_ms,
                0.0,
                (time.perf_counter() - t0) * 1000.0,
                error=f"join: status={data.get('status')!r}",
            )

        session_id = data.get("session_id")
        quiz_id = data.get("quiz_id")
        if not session_id or not quiz_id:
            return OneResult(
                False,
                join_ms,
                0.0,
                (time.perf_counter() - t0) * 1000.0,
                error="join: missing session_id or quiz_id",
            )

        assignment_key = f"quiz_{str(quiz_id)[:8].lower()}"

        payload = {
            "user_id": user_id,
            "session_id": session_id,
            "assignment_key": assignment_key,
            "survey_type": "periodic",
            "responses": [
                {"question": "Stress Q1", "response": 3},
                {"question": "Stress Q2", "response": 4},
                {"question": "Stress Q3", "response": 3},
            ],
            "analysis_count": 1,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "ai_history_context": {},
        }

        t_s0 = time.perf_counter()
        sr = await client.post(
            f"{base}/submit-survey",
            json=payload,
            timeout=120.0,
        )
        submit_ms = (time.perf_counter() - t_s0) * 1000.0
        total_ms = (time.perf_counter() - t0) * 1000.0

        if sr.status_code != 200:
            return OneResult(
                False,
                join_ms,
                submit_ms,
                total_ms,
                error=f"submit HTTP {sr.status_code}: {sr.text[:200]}",
            )
        sj = sr.json()
        if sj.get("status") != "success":
            return OneResult(
                False,
                join_ms,
                submit_ms,
                total_ms,
                error=f"submit: status={sj.get('status')!r}",
            )
        return OneResult(True, join_ms, submit_ms, total_ms, "")
    except Exception as e:
        return OneResult(
            False,
            join_ms,
            submit_ms,
            (time.perf_counter() - t0) * 1000.0,
            error=f"{type(e).__name__}: {e}",
        )


async def run_all(n: int, base: str, access_code: str) -> List[OneResult]:
    limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n + 10)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [one_student(client, base, access_code, i) for i in range(n)]
        return await asyncio.gather(*tasks)


def print_stats(name: str, vals: List[float]) -> None:
    if not vals:
        print(f"  {name}: (no samples)")
        return
    s = sorted(vals)
    print(
        f"  {name}: n={len(s)} min={s[0]:.1f}ms max={s[-1]:.1f}ms "
        f"mean={statistics.mean(s):.1f}ms p50={_percentile(s, 50):.1f}ms p95={_percentile(s, 95):.1f}ms"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Stress test quiz join + submit-survey")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base (no trailing slash)")
    ap.add_argument("--access-code", default="fmbk2Xuj", help="Quiz access code from URL /quiz/{code}")
    ap.add_argument("-n", "--students", type=int, default=100, help="Concurrent students")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    t_wall0 = time.perf_counter()
    results = asyncio.run(run_all(args.students, base, args.access_code))
    wall_s = time.perf_counter() - t_wall0

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]

    print(f"\nStress: {args.students} students → POST /quiz/join + POST /submit-survey")
    print(f"Base: {base}  access_code={args.access_code}")
    print(f"Wall-clock (gather): {wall_s:.2f}s")
    print(f"Success: {len(ok)}/{len(results)}  Failed: {len(bad)}")

    if ok:
        print_stats("Join (confirmation)", [r.join_ms for r in ok])
        print_stats("Submit-survey (confirmation JSON)", [r.submit_ms for r in ok])
        print_stats("Total per student (join+submit)", [r.total_ms for r in ok])

    if bad:
        print("\nFirst errors:")
        for r in bad[:12]:
            print(f"  - {r.error}")
        if len(bad) > 12:
            print(f"  ... and {len(bad) - 12} more")

    if len(ok) < len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
