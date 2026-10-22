#!/usr/bin/env python3
"""
Stress test: many students using the coding agent at once (condition 3). Each simulated student logs
in and has three agent turns, like a student working through a problem:

  1. Agent mode: "solve the problem" (the agent writes the code, usually runs it)
  2. Debug mode: "test it on the example and another input, fix what is wrong"
  3. Agent mode: "move the logic into a helper file" (the agent creates a new file)

After each turn that changed files the student looks at the versions offered (correct + buggy, each
with its description), applies one at random like a student who cannot always tell, and runs the
program in the sandbox. Then the student submits.

Point it at a server whose assignment key maps to condition 3 (CONDITION_3_PREFIXES). Run it
against a test server, not production: it creates sessions and calls OpenAI for real.

Usage:
  STRESS_BASE_URL=http://127.0.0.1:8099 STRESS_KEY=stresscond3 python scripts/stress_agent_users.py

Env:
  STRESS_BASE_URL   Default http://127.0.0.1:8000
  STRESS_KEY        Assignment key in condition 3 (required)
  STRESS_USERS      Default 50
  STRESS_RAMP_SEC   Students log in spread over this many seconds. Default 10
  STRESS_THINK_SEC  Mean pause between steps (seconds). Default 5

Exit code 1 if any request failed, any agent turn reported an error, or any run was rejected.
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
from typing import Any, Dict, List, Optional

import httpx
from websockets.asyncio.client import connect as ws_connect

BASE = os.environ.get("STRESS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ.get("STRESS_KEY", "").strip()
USERS = int(os.environ.get("STRESS_USERS", "50"))
RAMP = float(os.environ.get("STRESS_RAMP_SEC", "10"))
THINK = float(os.environ.get("STRESS_THINK_SEC", "5"))

PROBLEM = ("Problem: read n, then n integers (on the next line). Print the sum of the even ones.\n"
           "Example input:\n5\n1 2 3 4 5\nExample output:\n6")
STDIN = "5\n1 2 3 4 5\n"
EXPECTED = "6"

# language -> (entry file, starter content, helper file the third turn asks for)
STARTERS = {
    "python": ("main.py", "# Write your solution here\n", "utils.py"),
    "cpp": ("main.cpp", "#include <iostream>\nusing namespace std;\n\nint main() {\n    // Write your solution here\n    return 0;\n}\n", "utils.h"),
    "java": ("Main.java", "import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        // Write your solution here\n    }\n}\n", "EvenSum.java"),
    "javascript": ("main.js", "// Write your solution here\n", "utils.js"),
}
LANGUAGES = ["python"] * 5 + ["cpp"] * 2 + ["java"] * 2 + ["javascript"]

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


async def agent_turn(client: httpx.AsyncClient, sid: str, name: str, mode: str, message: str, language: str,
                     ws: Dict[str, str], active: str, history: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """One /agent/chat turn, read as the browser does. Returns the versions event (or None)."""
    body = {"session_id": sid, "mode": mode, "message": message, "language": language,
            "files": [{"filename": n, "content": c} for n, c in ws.items()], "active_file": active,
            "history": history[-20:], "skills": [], "terminal_output": ""}
    t = time.monotonic()
    first, text, versions, failed = None, "", None, False
    try:
        async with client.stream("POST", "/agent/chat", json=body) as resp:
            if resp.status_code >= 400:
                errors[name].append(f"HTTP {resp.status_code}: {(await resp.aread())[:150]!r}")
                return None
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                if first is None:
                    first = time.monotonic() - t
                kind = ev["type"]
                if kind == "text":
                    text += ev["delta"]
                elif kind == "file":
                    counts[f"agent file events: {ev['action']}"] += 1
                elif kind == "run":
                    counts[f"agent ran code: {'ok' if ev['success'] else 'failed'}"] += 1
                    if not ev["success"] and "runner" in (ev.get("error") or "").lower():
                        errors[name + " (agent run)"].append(ev["error"][:150])
                elif kind == "versions":
                    versions = ev
                elif kind == "error":
                    failed = True
                    errors[name].append(f"agent error event: {ev.get('message')}")
    except Exception as e:
        errors[name].append(f"{type(e).__name__}: {e}"[:200])
        return None
    lat[name].append(time.monotonic() - t)
    if first is not None:
        lat[name + " (first event)"].append(first)
    if failed:
        return None
    history += [{"role": "user", "content": message}]
    if versions is None:
        counts[f"{name}: no file changes"] += 1
        if text:
            history.append({"role": "assistant", "content": text})
        return None
    vs = versions["versions"]
    counts[f"{name}: {len(vs)} version(s)"] += 1
    if len(vs) == 2:
        if vs[0]["description"].strip() != vs[1]["description"].strip():
            counts[f"{name}: descriptions differ (wrong claim)"] += 1
        elif not vs[0]["description"].strip():
            counts[f"{name}: no description"] += 1
    for v in vs:
        await timed("pair_event", client.post("/pair-event", json={
            "session_id": sid, "pair_id": versions["pair_id"], "oid": v["oid"], "event": "shown"}))
    chosen = random.choice(vs)
    await timed("pair_event", client.post("/pair-event", json={
        "session_id": sid, "pair_id": versions["pair_id"], "oid": chosen["oid"], "event": "accepted"}))
    for f in chosen["files"]:
        if f["action"] == "delete":
            ws.pop(f["filename"], None)
        else:
            if f["filename"] not in ws:
                counts["files created by the agent (applied)"] += 1
            ws[f["filename"]] = f["content"]
    history.append({"role": "assistant", "content": (text + "\n\n" + chosen["description"]).strip()})
    return versions


async def run_code(sid: str, language: str, ws: Dict[str, str], entry: str) -> None:
    url = BASE.replace("http", "ws", 1) + "/ws/run"
    t = time.monotonic()
    out, exit_ev = "", None
    try:
        async with ws_connect(url, open_timeout=20, max_size=2 ** 20) as conn:
            await conn.send(json.dumps({"session_id": sid, "language": language,
                                        "files": [{"filename": n, "content": c} for n, c in ws.items()], "entry": entry}))
            await conn.send(json.dumps({"type": "stdin", "data": STDIN}))
            await conn.send(json.dumps({"type": "eof"}))
            async with asyncio.timeout(150):
                async for raw in conn:
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
    if not exit_ev or exit_ev.get("type") == "rejected":
        errors["run_code"].append(f"{language}: {exit_ev}")
    elif exit_ev.get("exit_code") == 0 and out.strip() == EXPECTED:
        counts[f"student run correct ({language})"] += 1
    else:
        # Expected now and then: the student may have applied the buggy version.
        counts[f"student run wrong/failed ({language})"] += 1


async def student(i: int, client: httpx.AsyncClient) -> None:
    await asyncio.sleep(random.uniform(0, RAMP))
    language = LANGUAGES[i % len(LANGUAGES)]
    entry, starter, helper = STARTERS[language]
    user_id = f"stressagent_{uuid.uuid4().hex[:6]}_{i}"
    r = await timed("authenticate", client.post("/authenticate", json={
        "user_id": user_id, "assignment_key": KEY, "programming_language": language}))
    if r is None:
        return
    sid = r.json()["session_id"]
    if r.json().get("condition") != 3:
        errors["authenticate"].append(f"condition {r.json().get('condition')}, expected 3")
        return
    ws = {entry: starter}
    history: List[Dict[str, str]] = []
    pause = lambda: asyncio.sleep(random.expovariate(1 / THINK))  # noqa: E731

    await pause()
    await agent_turn(client, sid, "agent: solve", "agent", f"Solve this in {entry}.\n\n{PROBLEM}",
                     language, ws, entry, history)
    await run_code(sid, language, ws, entry)
    await pause()
    await agent_turn(client, sid, "debug: test & fix", "debug",
                     "Test it on the example input, and on 4 / 2 4 6 8 (should print 20). Fix anything that is wrong.",
                     language, ws, entry, history)
    await run_code(sid, language, ws, entry)
    await pause()
    await agent_turn(client, sid, "agent: create file", "agent",
                     f"Move the even-sum logic into a new file {helper} and use it from {entry}.",
                     language, ws, entry, history)
    await run_code(sid, language, ws, entry)
    await pause()
    code = ws.get(entry, "")
    r = await timed("submit", client.post("/submit", json={
        "session_id": sid, "user_id": user_id, "assignment_key": KEY, "code": code,
        "programming_language": language, "timestamp": now_iso(),
        "files": [{"filename": n, "content": c} for n, c in ws.items()]}))
    if r is not None:
        counts["submit ok"] += 1


def pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def main() -> int:
    if not KEY:
        print("Set STRESS_KEY to an assignment key in condition 3.")
        return 2
    print(f"{USERS} students → {BASE} (key {KEY}), logins spread over {RAMP:.0f}s, ~{THINK:.0f}s between steps")
    # Drop idle connections before uvicorn's 5s keep-alive does (see stress_ide_users.py).
    limits = httpx.Limits(max_connections=USERS * 2, max_keepalive_connections=USERS, keepalive_expiry=3)
    t0 = time.monotonic()
    async with httpx.AsyncClient(base_url=BASE, timeout=300, limits=limits) as client:
        await asyncio.gather(*[student(i, client) for i in range(USERS)])
    wall = time.monotonic() - t0

    print(f"\nFinished in {wall:.0f}s\n")
    print(f"{'request':34} {'count':>6} {'median':>8} {'p90':>8} {'max':>8} {'errors':>7}")
    for name in sorted(set(lat) | set(errors)):
        xs = lat.get(name, [])
        if xs:
            print(f"{name:34} {len(xs):6} {statistics.median(xs):7.2f}s {pct(xs, .9):7.2f}s {max(xs):7.2f}s {len(errors.get(name, [])):7}")
        else:
            print(f"{name:34} {0:6} {'-':>8} {'-':>8} {'-':>8} {len(errors.get(name, [])):7}")
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
