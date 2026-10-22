#!/usr/bin/env python3
"""
Stress test: many students using the IDE at once in the ghost-text condition. Each simulated
student logs in, writes a short program line by line (asking for a code suggestion before every
line, like a real student pausing on a new line), logs editing activity, saves the editor draft,
runs the program in the sandbox (stdin in, output checked) and submits it.

Point it at a server whose assignment key maps to condition 2 (CONDITION_2_PREFIXES). Run it
against a test server, not production: it creates sessions and calls OpenAI for real.

Usage:
  STRESS_BASE_URL=http://127.0.0.1:8099 STRESS_KEY=stresscond2 python scripts/stress_ide_users.py

Env:
  STRESS_BASE_URL   Default http://127.0.0.1:8000
  STRESS_KEY        Assignment key in condition 2 (required)
  STRESS_USERS      Default 50
  STRESS_RAMP_SEC   Students log in spread over this many seconds. Default 10
  STRESS_THINK_SEC  Mean pause between lines (seconds). Default 3

Exit code 1 if any request failed, any 5xx, or any run gave the wrong output.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

import httpx
from websockets.asyncio.client import connect as ws_connect

BASE = os.environ.get("STRESS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ.get("STRESS_KEY", "").strip()
USERS = int(os.environ.get("STRESS_USERS", "50"))
RAMP = float(os.environ.get("STRESS_RAMP_SEC", "10"))
THINK = float(os.environ.get("STRESS_THINK_SEC", "3"))

STDIN = "5\n1 2 3 4 5\n"
EXPECTED = "6"

# The program each student types, one line per step: read n numbers, print the sum of the even ones.
PROGRAMS = {
    "python": ("main.py", [
        "n = int(input())",
        "nums = list(map(int, input().split()))",
        "total = 0",
        "for x in nums:",
        "    if x % 2 == 0:",
        "        total += x",
        "print(total)",
    ]),
    "cpp": ("main.cpp", [
        "#include <iostream>",
        "using namespace std;",
        "int main() {",
        "    int n;",
        "    cin >> n;",
        "    long long total = 0;",
        "    for (int i = 0; i < n; i++) {",
        "        int x;",
        "        cin >> x;",
        "        if (x % 2 == 0) total += x;",
        "    }",
        "    cout << total << endl;",
        "    return 0;",
        "}",
    ]),
    "java": ("Main.java", [
        "import java.util.*;",
        "public class Main {",
        "    public static void main(String[] args) {",
        "        Scanner sc = new Scanner(System.in);",
        "        int n = sc.nextInt();",
        "        long total = 0;",
        "        for (int i = 0; i < n; i++) {",
        "            int x = sc.nextInt();",
        "            if (x % 2 == 0) total += x;",
        "        }",
        "        System.out.println(total);",
        "    }",
        "}",
    ]),
}
# Roughly the mix of a class: mostly Python, some C++ and Java (the compiled ones load the sandbox most).
LANGUAGES = ["python"] * 6 + ["cpp"] * 2 + ["java"] * 2

lat: Dict[str, List[float]] = defaultdict(list)
errors: Dict[str, List[str]] = defaultdict(list)
counts: Dict[str, int] = defaultdict(int)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def timed(name: str, coro):
    t = time.monotonic()
    try:
        r = await coro
    except Exception as e:  # network error, timeout
        errors[name].append(f"{type(e).__name__}: {e}"[:200])
        return None
    lat[name].append(time.monotonic() - t)
    if isinstance(r, httpx.Response) and r.status_code >= 400:
        errors[name].append(f"HTTP {r.status_code}: {r.text[:150]}")
        return None
    return r


async def run_code(sid: str, language: str, filename: str, code: str) -> None:
    url = BASE.replace("http", "ws", 1) + "/ws/run"
    t = time.monotonic()
    out, exit_ev = "", None
    try:
        async with ws_connect(url, open_timeout=20, max_size=2 ** 20) as ws:
            await ws.send(json.dumps({"session_id": sid, "language": language,
                                      "files": [{"filename": filename, "content": code}], "entry": filename}))
            await ws.send(json.dumps({"type": "stdin", "data": STDIN}))
            await ws.send(json.dumps({"type": "eof"}))
            async with asyncio.timeout(150):
                async for raw in ws:
                    ev = json.loads(raw)
                    if ev.get("type") == "output":
                        out += ev.get("data", "")
                    elif ev.get("type") in ("exit", "rejected"):
                        exit_ev = ev
                        break
    except Exception as e:
        errors["run_code"].append(f"{type(e).__name__}: {e}"[:200])
        return
    lat["run_code"].append(time.monotonic() - t)
    if not exit_ev or exit_ev.get("type") == "rejected" or exit_ev.get("exit_code") != 0:
        errors["run_code"].append(f"{language}: {exit_ev} output={out[-150:]!r}")
    elif out.strip() != EXPECTED:
        errors["run_code (wrong output)"].append(f"{language}: {out[-150:]!r}")
    else:
        counts[f"run ok ({language})"] += 1


async def student(i: int, client: httpx.AsyncClient) -> None:
    await asyncio.sleep(random.uniform(0, RAMP))
    language = LANGUAGES[i % len(LANGUAGES)]
    filename, lines = PROGRAMS[language]
    user_id = f"stress_{uuid.uuid4().hex[:6]}_{i}"
    r = await timed("authenticate", client.post("/authenticate", json={
        "user_id": user_id, "assignment_key": KEY, "programming_language": language}))
    if r is None:
        return
    sid = r.json()["session_id"]
    if r.json().get("condition") != 2:
        errors["authenticate"].append(f"condition {r.json().get('condition')}, expected 2")
        return

    for step, line in enumerate(lines):
        # The student pauses on a new, indented line: the IDE asks for a suggestion.
        indent = line[: len(line) - len(line.lstrip())]
        prefix = "\n".join(lines[:step]) + ("\n" if step else "") + indent
        if prefix.strip():
            r = await timed("complete", client.post("/complete", json={
                "session_id": sid, "language": language, "filename": filename, "prefix": prefix, "suffix": ""}))
            if r is not None:
                data = r.json()
                n = len(data.get("options") or [])
                counts[f"suggestions with {n} option(s)"] += 1
                if n == 1 and data.get("no_bug"):
                    counts[f"  1 option because: {data['no_bug']}"] += 1
                for o in data.get("options") or []:
                    await timed("pair_event", client.post("/pair-event", json={
                        "session_id": sid, "pair_id": data["id"], "oid": o["oid"], "event": "shown"}))
        await asyncio.sleep(random.expovariate(1 / THINK))
        code = "\n".join(lines[: step + 1]) + "\n"
        await timed("log_activities", client.post("/log-activities", json={
            "session_id": sid, "user_id": user_id, "assignment_key": KEY,
            "activities": [{"timestamp": now_iso(), "activity_type": "code_editing_insert",
                            "activity_details": {"line": step + 1, "length": len(line)},
                            "session_context": {"total_lines": step + 1}}]}))
        if step % 3 == 2:
            await timed("checkpoint_editor", client.post("/checkpoint-editor", json={
                "session_id": sid, "user_id": user_id, "assignment_key": KEY, "code": code,
                "programming_language": language, "files": [{"filename": filename, "content": code}],
                "active_file": filename}))
        if step == len(lines) // 2:  # an early run of the half-written program (may not compile)
            await run_code_partial(sid, language, filename, code)

    code = "\n".join(lines) + "\n"
    await run_code(sid, language, filename, code)
    await asyncio.sleep(random.uniform(1, 3))
    await run_code(sid, language, filename, code)
    await asyncio.sleep(random.uniform(1, 3))
    r = await timed("submit", client.post("/submit", json={
        "session_id": sid, "user_id": user_id, "assignment_key": KEY, "code": code,
        "programming_language": language, "timestamp": now_iso(),
        "files": [{"filename": filename, "content": code}]}))
    if r is not None:
        counts["submit ok"] += 1


async def run_code_partial(sid: str, language: str, filename: str, code: str) -> None:
    url = BASE.replace("http", "ws", 1) + "/ws/run"
    t = time.monotonic()
    try:
        async with ws_connect(url, open_timeout=20, max_size=2 ** 20) as ws:
            await ws.send(json.dumps({"session_id": sid, "language": language,
                                      "files": [{"filename": filename, "content": code}], "entry": filename}))
            await ws.send(json.dumps({"type": "stdin", "data": STDIN}))
            await ws.send(json.dumps({"type": "eof"}))
            async with asyncio.timeout(150):
                async for raw in ws:
                    if json.loads(raw).get("type") in ("exit", "rejected"):
                        break
    except Exception as e:
        errors["run_code (partial)"].append(f"{type(e).__name__}: {e}"[:200])
        return
    lat["run_code (partial)"].append(time.monotonic() - t)


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def main() -> int:
    if not KEY:
        print("Set STRESS_KEY to an assignment key in condition 2.")
        return 2
    print(f"{USERS} students → {BASE} (key {KEY}), logins spread over {RAMP:.0f}s, ~{THINK:.0f}s per line")
    # Drop idle connections before uvicorn's 5s keep-alive does, or a request can go out on a
    # connection the server is closing ("Server disconnected"; browsers retry these silently).
    limits = httpx.Limits(max_connections=USERS * 2, max_keepalive_connections=USERS, keepalive_expiry=3)
    t0 = time.monotonic()
    async with httpx.AsyncClient(base_url=BASE, timeout=120, limits=limits) as client:
        await asyncio.gather(*[student(i, client) for i in range(USERS)])
    wall = time.monotonic() - t0

    print(f"\nFinished in {wall:.0f}s\n")
    print(f"{'request':26} {'count':>6} {'median':>8} {'p90':>8} {'max':>8} {'errors':>7}")
    for name in sorted(set(lat) | set(errors)):
        xs = lat.get(name, [])
        if xs:
            print(f"{name:26} {len(xs):6} {statistics.median(xs):7.2f}s {pct(xs, .9):7.2f}s {max(xs):7.2f}s {len(errors.get(name, [])):7}")
        else:
            print(f"{name:26} {0:6} {'-':>8} {'-':>8} {'-':>8} {len(errors.get(name, [])):7}")
    print()
    for k in sorted(counts):
        print(f"{k}: {counts[k]}")
    failed = {k: v for k, v in errors.items() if v}
    if failed:
        print("\nErrors (first 3 of each):")
        for k, v in failed.items():
            for e in v[:3]:
                print(f"  {k}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
