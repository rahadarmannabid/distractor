#!/usr/bin/env python3
"""
Load pre/post survey definitions from GET /survey-config, then stress-test:
  POST /quiz/join → POST /submit-pre-post-survey (pre) → POST /submit-pre-post-survey (post)

Example (production):
  python scripts/stress_pre_post_survey.py --base-url https://www.guide.codes --access-code fmbk2Xuj -n 10
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List

import httpx


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _synthetic_answer(q: Dict[str, Any]) -> Any:
    """Match templates/survey.html semantics for a valid answer."""
    t = (q.get("type") or "").strip()
    if t == "info_block":
        return None
    if t == "likert":
        scale = q.get("scale") or {}
        lo = int(scale.get("min", 1))
        hi = int(scale.get("max", 5))
        return int((lo + hi) / 2)
    if t == "multiple_choice":
        opts = q.get("options") or []
        return 0 if opts else None
    if t == "slider":
        sc = q.get("scale") or {}
        return int(sc.get("default", (int(sc.get("min", 0)) + int(sc.get("max", 100))) // 2))
    if t == "open_ended":
        ml = int(q.get("min_length") or 0)
        base = "Stress-test answer for load testing. "
        need = max(ml, 10)
        s = (base * ((need // len(base)) + 1))[:need]
        return s if len(s) >= ml else s + "x" * (ml - len(s))
    return "ok"


def _build_pre_post_payload(
    *,
    survey_block: Dict[str, Any],
    survey_type: str,
    user_id: str,
    session_id: str,
    assignment_key: str,
) -> Dict[str, Any]:
    raw_questions = [q for q in (survey_block.get("questions") or []) if isinstance(q, dict)]
    questions = [q for q in raw_questions if q.get("type") != "info_block"]
    if not questions:
        raise ValueError(f"No {survey_type} questions in config")

    q_payload = []
    r_payload = []
    for q in questions:
        q_payload.append(
            {
                "id": q.get("id") or "q",
                "type": q.get("type") or "open_ended",
                "question": q.get("question") or "",
                "required": q.get("required", True) is not False,
                "scale": q.get("scale"),
                "options": q.get("options"),
                "placeholder": q.get("placeholder"),
                "max_length": q.get("max_length"),
                "min_length": q.get("min_length"),
            }
        )
        ans = _synthetic_answer(q)
        r_payload.append(
            {
                "question_id": q.get("id") or "q",
                "question_text": q.get("question") or "",
                "question_type": q.get("type") or "open_ended",
                "question_config": {
                    "scale": q.get("scale"),
                    "options": q.get("options"),
                    "placeholder": q.get("placeholder"),
                    "max_length": q.get("max_length"),
                    "min_length": q.get("min_length"),
                    "help_text": q.get("help_text"),
                },
                "answer": ans,
                "required": q.get("required", True) is not False,
            }
        )

    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    return {
        "user_id": user_id,
        "session_id": session_id,
        "assignment_key": assignment_key,
        "survey_type": survey_type,
        "survey_title": survey_block.get("title") or ("Pre-Study Survey" if survey_type == "pre" else "Post-Study Survey"),
        "survey_description": survey_block.get("description") or "",
        "questions": q_payload,
        "responses": r_payload,
        "timestamp": ts,
        "total_questions": len(questions),
        "required_questions": sum(1 for q in questions if q.get("required", True) is not False),
        "completion_time_seconds": 12.5,
    }


@dataclass
class OneResult:
    ok: bool
    join_ms: float
    pre_ms: float
    post_ms: float
    total_ms: float
    error: str = ""


async def fetch_survey_config(client: httpx.AsyncClient, base: str, assignment_key: str) -> Dict[str, Any]:
    url = f"{base}/survey-config/{assignment_key}"
    r = await client.get(url, params={"problem_index": 0}, timeout=60.0)
    r.raise_for_status()
    return r.json()


async def one_student(
    client: httpx.AsyncClient,
    base: str,
    access_code: str,
    assignment_key: str,
    pre_block: Dict[str, Any],
    post_block: Dict[str, Any],
    idx: int,
) -> OneResult:
    user_id = f"stress_{idx}_{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    join_ms = pre_ms = post_ms = 0.0
    try:
        t_j = time.perf_counter()
        jr = await client.post(
            f"{base}/quiz/join",
            json={
                "access_code": access_code,
                "user_id": user_id,
                "programming_language": "python",
            },
            timeout=120.0,
        )
        join_ms = (time.perf_counter() - t_j) * 1000.0
        if jr.status_code != 200:
            return OneResult(
                False,
                join_ms,
                0,
                0,
                (time.perf_counter() - t0) * 1000.0,
                f"join HTTP {jr.status_code}: {jr.text[:180]}",
            )
        data = jr.json()
        if data.get("status") != "success":
            return OneResult(
                False,
                join_ms,
                0,
                0,
                (time.perf_counter() - t0) * 1000.0,
                f"join: {data.get('status')!r}",
            )
        session_id = data.get("session_id")
        quiz_id = data.get("quiz_id")
        if not session_id or not quiz_id:
            return OneResult(
                False,
                join_ms,
                0,
                0,
                (time.perf_counter() - t0) * 1000.0,
                "join: missing session_id or quiz_id",
            )

        ak = f"quiz_{str(quiz_id)[:8].lower()}"

        try:
            pre_body = _build_pre_post_payload(
                survey_block=pre_block,
                survey_type="pre",
                user_id=user_id,
                session_id=session_id,
                assignment_key=ak,
            )
        except ValueError as e:
            return OneResult(
                False,
                join_ms,
                0,
                0,
                (time.perf_counter() - t0) * 1000.0,
                str(e),
            )

        t_p = time.perf_counter()
        pr = await client.post(
            f"{base}/submit-pre-post-survey",
            json=pre_body,
            timeout=120.0,
        )
        pre_ms = (time.perf_counter() - t_p) * 1000.0
        if pr.status_code != 200:
            return OneResult(
                False,
                join_ms,
                pre_ms,
                0,
                (time.perf_counter() - t0) * 1000.0,
                f"pre HTTP {pr.status_code}: {pr.text[:180]}",
            )
        pj = pr.json()
        if pj.get("status") != "success":
            return OneResult(
                False,
                join_ms,
                pre_ms,
                0,
                (time.perf_counter() - t0) * 1000.0,
                f"pre: {pj}",
            )

        try:
            post_body = _build_pre_post_payload(
                survey_block=post_block,
                survey_type="post",
                user_id=user_id,
                session_id=session_id,
                assignment_key=ak,
            )
        except ValueError as e:
            return OneResult(
                False,
                join_ms,
                pre_ms,
                0,
                (time.perf_counter() - t0) * 1000.0,
                str(e),
            )

        t_o = time.perf_counter()
        por = await client.post(
            f"{base}/submit-pre-post-survey",
            json=post_body,
            timeout=120.0,
        )
        post_ms = (time.perf_counter() - t_o) * 1000.0
        if por.status_code != 200:
            return OneResult(
                False,
                join_ms,
                pre_ms,
                post_ms,
                (time.perf_counter() - t0) * 1000.0,
                f"post HTTP {por.status_code}: {por.text[:180]}",
            )
        poj = por.json()
        if poj.get("status") != "success":
            return OneResult(
                False,
                join_ms,
                pre_ms,
                post_ms,
                (time.perf_counter() - t0) * 1000.0,
                f"post: {poj}",
            )

        return OneResult(
            True,
            join_ms,
            pre_ms,
            post_ms,
            (time.perf_counter() - t0) * 1000.0,
            "",
        )
    except Exception as e:
        return OneResult(
            False,
            join_ms,
            pre_ms,
            post_ms,
            (time.perf_counter() - t0) * 1000.0,
            f"{type(e).__name__}: {e}",
        )


def _stats(name: str, vals: List[float]) -> None:
    if not vals:
        print(f"  {name}: (no samples)")
        return
    s = sorted(vals)
    print(
        f"  {name}: n={len(s)} min={s[0]:.1f}ms max={s[-1]:.1f}ms "
        f"mean={statistics.mean(s):.1f}ms p50={_percentile(s, 50):.1f}ms p95={_percentile(s, 95):.1f}ms"
    )


async def run_all(
    n: int,
    base: str,
    access_code: str,
    pre_block: Dict[str, Any],
    post_block: Dict[str, Any],
    assignment_key: str,
) -> List[OneResult]:
    limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n + 10)
    async with httpx.AsyncClient(limits=limits, verify=True) as client:
        tasks = [
            one_student(client, base, access_code, assignment_key, pre_block, post_block, i)
            for i in range(n)
        ]
        return await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stress pre + post survey on quiz (join → pre → post)")
    ap.add_argument("--base-url", default="https://www.guide.codes", help="API origin")
    ap.add_argument("--access-code", default="fmbk2Xuj", help="Quiz access code (from /quiz/{code})")
    ap.add_argument("-n", "--students", type=int, default=10, help="Concurrent students (use small values on prod)")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    assignment_key = f"quiz_{args.access_code}"

    with httpx.Client(timeout=60.0, verify=True) as client:
        cfg = client.get(f"{base}/survey-config/{assignment_key}", params={"problem_index": 0})
        if cfg.status_code != 200:
            print(f"ERROR: survey-config HTTP {cfg.status_code}: {cfg.text[:300]}")
            raise SystemExit(1)
        data = cfg.json()

    pre_block = data.get("pre_survey") or {}
    post_block = data.get("post_survey") or {}
    pre_q = [q for q in (pre_block.get("questions") or []) if isinstance(q, dict) and q.get("type") != "info_block"]
    post_q = [q for q in (post_block.get("questions") or []) if isinstance(q, dict) and q.get("type") != "info_block"]

    print(f"Survey config: assignment_key={assignment_key}")
    print(f"  pre_enabled={pre_block.get('enabled')} pre_questions={len(pre_q)}")
    print(f"  post_enabled={post_block.get('enabled')} post_questions={len(post_q)}")

    if not pre_q and not post_q:
        print("ERROR: No pre/post questions — enable surveys on the quiz problem or use a different code.")
        raise SystemExit(1)

    if not pre_q:
        print("WARNING: No pre-survey questions; pre POST will fail. Aborting.")
        raise SystemExit(1)
    if not post_q:
        print("WARNING: No post-survey questions; post POST will fail. Aborting.")
        raise SystemExit(1)

    wall0 = time.perf_counter()
    results = asyncio.run(run_all(args.students, base, args.access_code, pre_block, post_block, assignment_key))
    wall = time.perf_counter() - wall0

    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]

    print(f"\nConcurrent students: {args.students}  wall={wall:.2f}s")
    print(f"Success: {len(ok)}/{len(results)}  Failed: {len(bad)}")
    if ok:
        _stats("POST /quiz/join", [r.join_ms for r in ok])
        _stats("POST /submit-pre-post-survey (pre)", [r.pre_ms for r in ok])
        _stats("POST /submit-pre-post-survey (post)", [r.post_ms for r in ok])
        _stats("Total per student", [r.total_ms for r in ok])

    if bad:
        print("\nFirst errors:")
        for r in bad[:15]:
            print(f"  - {r.error}")
        if len(bad) > 15:
            print(f"  ... and {len(bad) - 15} more")

    if len(ok) < len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
