#!/usr/bin/env python3
"""
Emulate many concurrent students hitting the same HTTP paths the IDE uses.

Usage (API must be running):
  LOADTEST_BASE_URL=http://127.0.0.1:8000 python scripts/emulate_students_loadtest.py

Env:
  LOADTEST_BASE_URL   Default http://127.0.0.1:8000
  LOADTEST_STUDENTS   Default 100
  LOADTEST_ROUNDS     Per-student request cycles (log + checkpoint). Default 4
  LOADTEST_ANALYZE    Set to 1 to also POST /analyze (uses OpenAI quota)
  LOADTEST_TIMEOUT    Per-request seconds. Default 120
  LOADTEST_QUIZ_CODE  If set (e.g. 5eA6yK63), each student GETs /quiz/{code}, POSTs /quiz/join,
                      then uses real session_id + quiz_* assignment_key for log/checkpoint.
  LOADTEST_QUIZ_LANGUAGE  Language sent to /quiz/join (default python). Must be in quiz allowed_languages.

Exit code 1 if any 5xx or request failures occur.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import List

import httpx

BASE = os.environ.get("LOADTEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
NUM_STUDENTS = int(os.environ.get("LOADTEST_STUDENTS", "100"))
ROUNDS = int(os.environ.get("LOADTEST_ROUNDS", "4"))
WITH_ANALYZE = os.environ.get("LOADTEST_ANALYZE", "0") == "1"
TIMEOUT = float(os.environ.get("LOADTEST_TIMEOUT", "120"))
QUIZ_CODE = os.environ.get("LOADTEST_QUIZ_CODE", "").strip()
# Default python — many instructor quizzes only allow python; override with LOADTEST_QUIZ_LANGUAGE.
QUIZ_LANG = os.environ.get("LOADTEST_QUIZ_LANGUAGE", "python").strip() or "python"


def _activity(ts: str, kind: str) -> dict:
    return {
        "timestamp": ts,
        "activity_type": kind,
        "activity_details": {"loadtest": True},
        "session_context": {
            "total_lines": 12,
            "current_analysis_count": 0,
            "time_since_start": 1.0,
            "session_id": "",
            "user_id": "",
            "condition": 0,
            "problem_index": 0,
        },
    }


@dataclass
class StudentResult:
    student_id: int
    errors: List[str] = field(default_factory=list)
    latencies_ms: List[float] = field(default_factory=list)
    status_counts: dict = field(default_factory=dict)


async def one_student(client: httpx.AsyncClient, student_id: int) -> StudentResult:
    out = StudentResult(student_id=student_id)
    sid = f"loadtest_s{student_id}_{uuid.uuid4().hex[:10]}"
    uid = f"loadtest_u{student_id}"
    aid = "loadtest_assignment"

    def note(resp: httpx.Response, label: str) -> None:
        out.latencies_ms.append(resp.elapsed.total_seconds() * 1000.0)
        c = resp.status_code
        out.status_counts[c] = out.status_counts.get(c, 0) + 1
        if c >= 500:
            out.errors.append(f"{label} HTTP {c}")
        elif c >= 400:
            out.errors.append(f"{label} HTTP {c} (client error)")

    if QUIZ_CODE:
        try:
            pg = await client.get(f"{BASE}/quiz/{QUIZ_CODE}")
            note(pg, "quiz-page")
        except Exception as e:
            out.errors.append(f"quiz-page: {e}")
            return out
        try:
            jr = await client.post(
                f"{BASE}/quiz/join",
                json={"access_code": QUIZ_CODE, "user_id": uid, "programming_language": QUIZ_LANG},
            )
            note(jr, "quiz-join")
        except Exception as e:
            out.errors.append(f"quiz-join: {e}")
            return out
        if jr.status_code != 200:
            return out
        try:
            data = jr.json()
        except Exception as e:
            out.errors.append(f"quiz-join json: {e}")
            return out
        if data.get("quiz_already_completed") or data.get("status") == "already_completed":
            return out
        qid = (data.get("quiz_id") or "").strip()
        new_sid = (data.get("session_id") or "").strip()
        if not new_sid or not qid:
            out.errors.append("quiz-join: missing session_id or quiz_id")
            return out
        sid = new_sid
        aid = f"quiz_{qid[:8]}"

    for r in range(ROUNDS):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for ctx in (_activity(ts, "code_editing_insert"), _activity(ts, "code_editing_modify")):
            ctx["session_context"]["session_id"] = sid
            ctx["session_context"]["user_id"] = uid

        body: dict = {
            "session_id": sid,
            "activities": [
                _activity(ts, "code_editing_insert"),
                _activity(ts, "code_editing_modify"),
                _activity(ts, "ai_analysis_trigger"),
            ],
        }
        for a in body["activities"]:
            a["session_context"]["session_id"] = sid
            a["session_context"]["user_id"] = uid
        if QUIZ_CODE:
            body["user_id"] = uid
            body["assignment_key"] = aid

        try:
            resp = await client.post(f"{BASE}/log-activities", json=body)
            note(resp, f"log-activities r{r}")
        except Exception as e:
            out.errors.append(f"log-activities r{r}: {e}")

        if QUIZ_LANG.lower() == "python":
            sample_code = f"# student {student_id} r{r}\ndef f():\n    return {r}\n"
        else:
            sample_code = f"// student {student_id} round {r}\nfunction f() {{ return {r}; }}\n"
        ck_body = {
            "session_id": sid,
            "user_id": uid,
            "assignment_key": aid,
            "code": sample_code,
            "programming_language": QUIZ_LANG if QUIZ_CODE else "javascript",
            "cursor_line": 2,
            "cursor_column": 1,
            "current_problem_index": 0,
        }
        try:
            resp = await client.post(f"{BASE}/checkpoint-editor", json=ck_body)
            note(resp, f"checkpoint-editor r{r}")
        except Exception as e:
            out.errors.append(f"checkpoint-editor r{r}: {e}")

        if WITH_ANALYZE:
            an_body = {
                "code": "def hello():\n    return 42\n",
                "user_id": uid,
                "assignment_key": aid,
                "session_id": sid,
                "programming_language": "python",
                "cursor_line": 2,
                "cursor_column": 1,
            }
            try:
                resp = await client.post(f"{BASE}/analyze", json=an_body)
                note(resp, f"analyze r{r}")
            except Exception as e:
                out.errors.append(f"analyze r{r}: {e}")

    return out


async def main() -> int:
    print(f"Target: {BASE}")
    print(f"Students: {NUM_STUDENTS}, rounds/student: {ROUNDS}, analyze: {WITH_ANALYZE}")
    if QUIZ_CODE:
        print(f"Quiz mode: GET /quiz/{QUIZ_CODE} + POST /quiz/join, language={QUIZ_LANG}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            h = await client.get(f"{BASE}/health")
            print(f"GET /health -> {h.status_code}")
            if h.status_code != 200:
                print("ERROR: /health did not return 200. Start the API first.", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"ERROR: cannot reach {BASE}: {e}", file=sys.stderr)
            return 1

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        results = await asyncio.gather(
            *[one_student(client, i) for i in range(NUM_STUDENTS)],
            return_exceptions=True,
        )
    elapsed = time.perf_counter() - t0

    all_lat: List[float] = []
    all_errors: List[str] = []
    status_totals: dict = {}
    exc_count = 0
    for res in results:
        if isinstance(res, StudentResult):
            all_lat.extend(res.latencies_ms)
            all_errors.extend(res.errors)
            for k, v in res.status_counts.items():
                status_totals[k] = status_totals.get(k, 0) + v
            continue
        exc_count += 1
        all_errors.append(str(res))

    print()
    print(f"Finished in {elapsed:.2f}s wall-clock ({NUM_STUDENTS} students in parallel)")
    print(f"HTTP status totals: {dict(sorted(status_totals.items()))}")
    if all_lat:
        sl = sorted(all_lat)
        p95 = sl[int(0.95 * (len(sl) - 1))] if len(sl) > 1 else sl[0]
        print(
            f"Latency ms: min={min(all_lat):.1f} p50={statistics.median(all_lat):.1f} "
            f"p95={p95:.1f} max={max(all_lat):.1f}"
        )
    if exc_count:
        print(f"Gather exceptions (student tasks): {exc_count}")
    if all_errors:
        print(f"Issues logged: {len(all_errors)}")
        for line in all_errors[:40]:
            print(f"  - {line}")
        if len(all_errors) > 40:
            print(f"  ... and {len(all_errors) - 40} more")
        return 1

    print("OK — no 5xx or transport errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
