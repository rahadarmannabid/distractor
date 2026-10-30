"""AI assistance for study conditions 2 and 4 (inline ghost-text completion, single- and
multi-line) and 3 (coding agent).

Kept free of main.py imports: callers pass in the OpenAI client and a code runner.
"""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

import bug_injection
from llm_params import chat_args, is_reasoning_model

logger = logging.getLogger(__name__)

# Ghost text has to be fast and its bugs realistic. gpt-6-sol (reasoning off) answers in ~2.3 s with a
# valid pair 16/16 times and plants bugs a real assistant makes, each tied to an input that exposes it
# (int overflow at the constraint limit, a filter that drops digits). gpt-4.1 (~1.6 s) and
# gpt-4.1-mini (~1.5 s) are faster but often plant unrealistic or harmless bugs; gpt-6.1-sol is as
# good but takes 4-12 s, since its reasoning cannot be turned off.
COMPLETION_MODEL = os.getenv("COMPLETION_MODEL", "gpt-6-sol")
# gpt-6-luna at low effort answers in ~1.8 s where gpt-5-nano at low effort takes ~3 s.
AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-6-luna")
# The agent plans multi-step edits, so it gets a little more reasoning than the inline calls. It runs on
# the Responses API, since Chat Completions rejects function tools with reasoning for gpt-6 models.
AGENT_REASONING_EFFORT = os.getenv("AGENT_REASONING_EFFORT", "low")

AGENT_MODES = ("ask", "agent", "plan", "debug")
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "15"))
# Steps the agent gets to fix its code when the closing check finds a failing test case.
AGENT_FIX_STEPS = int(os.getenv("AGENT_FIX_STEPS", "8"))
MAX_FILES = 20
MAX_FILE_CHARS = 20000
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# Same file types the student's explorer allows (templates/workspace.js LANG_EXT / EXTRA_EXT).
_LANG_EXT = {"python": ["py"], "c": ["c", "h"], "cpp": ["cpp", "hpp", "h", "cc"], "java": ["java"], "javascript": ["js"]}
_EXTRA_EXT = ["txt", "md", "csv", "json"]


def allowed_extensions(language: str) -> List[str]:
    return _LANG_EXT.get(language, []) + _EXTRA_EXT


# ─── Inline completion (conditions 2 and 4) ──────────────────────────────────
# A suggestion is the next piece of code, written like Copilot writes it, and the buggy options are
# written from it:
# 1. The model writes the next lines at the cursor (up to LOOKAHEAD_LINES, a "look-ahead"), correct for
#    the problem and continuing the student's code as it is.
# 2. The correct suggestion is the next line (condition 2) or the next step (condition 4, at most
#    MULTILINE max lines) cut from the start of the look-ahead (see _copilot_portion).
# 3. One short call per buggy option writes it from the correct suggestion (bug_injection).
# The rest of the look-ahead forecasts the next steps: while the student's code still matches it (they
# accept or type along), later suggestions are cut from it, and their pairs are written ahead (see
# "Pairs ahead of time"), as is the next look-ahead before this one runs out.

PROBLEM_MAX_CHARS = 6000
LOOKAHEAD_LINES = int(os.getenv("LOOKAHEAD_LINES", "12"))
# The look-ahead is what the student waits for on a cache miss: no reasoning.
LOOKAHEAD_REASONING_EFFORT = os.getenv("LOOKAHEAD_REASONING_EFFORT", "minimal")
SOLUTION_MAX_TESTS = 5
_SOLUTION_CACHE_SIZE = 500

_SOLUTION_SYSTEM = (
    "You are a fast code completion engine like GitHub Copilot. You receive the student's {language} "
    "file with a <CURSOR> marker. Write the next lines of code to insert at <CURSOR>: up to {lines} "
    "lines, stopping at a natural boundary (the end of a statement or block), or fewer when the program "
    "is finished. Write them the way a developer writes code line by line: one statement per line, "
    "never several statements joined with ';' and never a compressed one-liner. The code before and "
    "after the cursor stays exactly as the student wrote it: continue in their style, with their "
    "names, structure and way of reading input, and never repeat any of it. If the cursor is in the "
    "middle of a line, start by finishing that line. If a <problem> is given, the code must be "
    "correct for it exactly as stated: read the input and print the output in exactly the format of "
    "its test cases (no prompts or extra text), and handle its constraints and edge cases. Reply with "
    "only the code to insert: no explanations, no markdown fences. If the file is already a complete, "
    "correct program, reply with nothing."
)

_TESTS_SYSTEM = (
    "Read the programming problem in <problem> (and its images, if any) and list its test cases as the "
    "exact standard input and expected standard output a judge would use. Reply with a JSON object "
    '{"tests": [{"input": "...", "output": "..."}]}' f", at most {SOLUTION_MAX_TESTS} tests, copied "
    "from the problem (never invent new ones). If the problem gives no test case with exact input and "
    "output, or its tests are not about standard input and output (e.g. they call a function), reply "
    '{"tests": []}.'
)

def _problem_block(problem: str) -> str:
    problem = (problem or "").strip()
    return f"<problem>\n{problem[:PROBLEM_MAX_CHARS]}\n</problem>\n\n" if problem else ""


_IMAGES_NOTE = ("The images below are part of the problem, in order: [Image 1], [Image 2], … in <problem>. "
                "The correct code must follow what they show as well as the text.")


def _strip_fences(text: str) -> str:
    m = re.match(r"^\s*```[\w+-]*\n(.*?)\n?```\s*$", text, re.S)
    return m.group(1) if m else text


_TRAILING_COMMENT_RE = re.compile(r"\s+(#|//)\s.*$")


def _drop_new_comments(buggy: str, correct: str) -> str:
    """Remove trailing comments the buggy version adds (e.g. "# empty list -> division by zero"):
    they would point the student at the bug."""
    out = []
    for line in buggy.split("\n"):
        m = _TRAILING_COMMENT_RE.search(line)
        if m and m.group(0).strip() not in correct:
            line = line[: m.start()]
        out.append(line)
    return "\n".join(out)


def clean_completion(completion: str, prefix: str, suffix: str, max_lines: int) -> str:
    text = _strip_fences(completion.replace("\r\n", "\n")).replace("<CURSOR>", "")
    if not text.strip():
        return ""

    # Smaller models sometimes restate the code before the cursor; drop the restated lines
    # (compared without indentation or blank lines).
    before = [ln.strip() for ln in prefix.split("\n")[:-1] if ln.strip()]
    lines = text.split("\n")
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    for n in range(min(len(before), len(filled) - 1), 0, -1):
        if [lines[i].strip() for i in filled[:n]] == before[-n:]:
            text = "\n".join(lines[filled[n - 1] + 1:])
            break

    # Never repeat a statement right after itself (the line above the cursor, or the suggestion's
    # own previous line): e.g. the same print line twice. Short lines like "}" may repeat.
    line_head = prefix.rsplit("\n", 1)[-1]
    prev = before[-1] if before and not line_head.strip() else None
    kept = []
    for i, ln in enumerate(text.split("\n")):
        st = ln.strip()
        if len(st) >= 8 and st == prev:
            continue
        kept.append(ln)
        if st:
            # A first line that continues the current line is a fragment, not a statement.
            prev = st if i > 0 or not line_head.strip() else None
    text = "\n".join(kept)
    if not text.strip():
        return ""

    # Model sometimes repeats the partial current line; drop the duplicate.
    if not line_head.strip():
        # Cursor sits in the line's indentation: drop indentation the editor already has,
        # otherwise the ghost text is over-indented and Tab indents instead of accepting.
        first, _, rest = text.partition("\n")
        stripped = first.lstrip(" \t")
        indent = first[: len(first) - len(stripped)]
        first = indent[len(line_head):] if indent.startswith(line_head) else ""
        text = first + stripped + ("\n" + rest if _ else "")
    elif text.startswith(line_head):
        text = text[len(line_head):]
    elif text.lstrip().startswith(line_head.strip()):
        text = text.lstrip()[len(line_head.strip()):]

    # Mid-line cursor (code to the right): single-line suggestion only, like Copilot.
    line_tail = suffix.split("\n", 1)[0]
    if line_tail.strip():
        text = text.split("\n", 1)[0]
        if text.endswith(line_tail.strip()):
            text = text[: -len(line_tail.strip())]

    # Drop any trailing overlap with the code after the cursor. When that code starts on a new line,
    # the overlap must too: a deeper "        }" ends with the outer loop's "    }" but is not it.
    tail = suffix.lstrip("\n")
    whole_lines = not line_tail.strip()
    for k in range(min(len(text), len(tail)), 3, -1):
        if text.endswith(tail[:k]) and (not whole_lines or not text[:-k] or text[:-k].endswith("\n")):
            text = text[:-k]
            break

    # A suggestion that starts on the next line keeps one newline (only after a non-blank line head).
    body = text.lstrip("\n")
    lead = "\n" if body != text and line_head.strip() else ""
    lines = body.rstrip().split("\n")[:max_lines]
    return (lead + "\n".join(lines)).rstrip()


def _completion_user_content(problem: str, images: List[str], code: str) -> Any:
    """Plain text, or with problem images: text parts around image parts (problem first, so the
    problem and its images stay the same prefix from one request to the next)."""
    if not images:
        return problem + code
    parts: List[Dict[str, Any]] = [{"type": "text", "text": problem + _IMAGES_NOTE}]
    parts += [{"type": "image_url", "image_url": {"url": u, "detail": "auto"}} for u in images]
    if code:
        parts.append({"type": "text", "text": code})
    return parts


def _problem_key(problem: str, images: List[str]) -> str:
    h = hashlib.sha1(problem.encode("utf-8", "replace"))
    for u in images:
        h.update(u.encode("ascii", "replace"))
    return h.hexdigest()


_tests_cache: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()


async def problem_tests(client: AsyncOpenAI, problem: str, images: List[str]) -> List[Dict[str, str]]:
    """The problem's test cases as [{"input", "output"}] (stdin/stdout), [] if it has none we can run."""
    if not problem.strip():
        return []
    key = _problem_key(problem, images)
    if key in _tests_cache:
        return _tests_cache[key]
    try:
        resp = await client.chat.completions.create(
            **chat_args(COMPLETION_MODEL, temperature=0, max_tokens=2000),
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _TESTS_SYSTEM},
                      {"role": "user", "content": _completion_user_content(_problem_block(problem), images, "")}],
        )
        raw = json.loads(resp.choices[0].message.content or "{}").get("tests")
    except Exception as e:
        logger.warning("Problem tests not extracted: %s", e)
        return []  # not cached: the next request tries again
    tests = [{"input": str(t.get("input") or ""), "output": str(t.get("output"))}
             for t in (raw if isinstance(raw, list) else [])
             if isinstance(t, dict) and str(t.get("output") or "").strip()][:SOLUTION_MAX_TESTS]
    _tests_cache[key] = tests
    if len(_tests_cache) > _SOLUTION_CACHE_SIZE:
        _tests_cache.popitem(last=False)
    return tests


def _same_output(actual: str, expected: str) -> bool:
    def norm(t: str) -> str:
        return "\n".join(ln.rstrip() for ln in t.replace("\r\n", "\n").strip("\n").split("\n")).strip()
    return norm(actual) == norm(expected) or actual.split() == expected.split()


# (slot, code, language, stdin, files or None) -> the runner's result. Slots let the test cases run in
# parallel; files are the whole workspace when the program may import its other files (agent).
CheckRun = Callable[[int, str, str, str, Optional[List[Dict[str, str]]]], Awaitable[Dict[str, Any]]]


async def _run_tests(
    program: str, language: str, tests: List[Dict[str, str]], run: CheckRun,
    files: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """{"passed", "total", "failures"} for the program on the tests, or None if the runner refused or
    was unavailable (then nothing is known about the program)."""
    results = await asyncio.gather(*(run(i, program, language, t["input"], files) for i, t in enumerate(tests)),
                                   return_exceptions=True)
    failures = []
    for t, r in zip(tests, results):
        # A refused run (busy, rate limited) or an unreachable runner has no execution time.
        if isinstance(r, BaseException) or not r.get("execution_time"):
            logger.info("Solution check skipped: %s", r if isinstance(r, BaseException) else r.get("error"))
            return None
        if not _same_output(r.get("output") or "", t["output"]):
            failures.append({"input": t["input"][:1000], "expected": t["output"][:1000],
                             "got": (r.get("output") or "")[-1000:], "error": (r.get("error") or "")[-1000:]})
    return {"passed": len(tests) - len(failures), "total": len(tests), "failures": failures}


async def _write_insert(
    client: AsyncOpenAI, *, language: str, filename: str, prefix: str, suffix: str,
    problem: str, images: List[str],
) -> str:
    """The look-ahead: the next lines of code at the cursor."""
    code = f"File: {filename}\n{prefix[-8000:]}<CURSOR>{suffix[:3000]}"
    resp = await client.chat.completions.create(
        **chat_args(COMPLETION_MODEL, temperature=0, max_tokens=60 * LOOKAHEAD_LINES,
                    reasoning_effort=LOOKAHEAD_REASONING_EFFORT),
        messages=[
            {"role": "system", "content": _SOLUTION_SYSTEM.format(language=language, lines=LOOKAHEAD_LINES)},
            {"role": "user", "content": _completion_user_content(_problem_block(problem), images, code)},
        ],
    )
    return _strip_fences((resp.choices[0].message.content or "").replace("\r\n", "\n")).replace("<CURSOR>", "")


def _program(sol: Dict[str, Any]) -> str:
    return sol["prefix"] + sol["insert"] + sol["suffix"]


def _insert_for(sol: Dict[str, Any], prefix: str, suffix: str) -> Optional[str]:
    """What the program still adds between this prefix and suffix, or None when the student's code no
    longer matches it. Brackets the editor closed on its own (e.g. the ")" after a typed "(") are allowed."""
    program = _program(sol)
    if not program.startswith(prefix):
        return None
    tail = suffix
    if not program.endswith(tail):
        first, nl, rest = suffix.partition("\n")
        if not first.strip() or first.strip(")]}\"\'; ") or not program.endswith(nl + rest):
            return None
        tail = nl + rest
    if len(prefix) + len(tail) > len(program):
        return None
    return program[len(prefix):len(program) - len(tail)]


# Per session, its programs, most recently used first: the one the student follows and, once a buggy
# option is shown, the ones written ahead for "the student accepts the bug" (see _prefetch_branch).
_solutions: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
_SOLUTIONS_PER_SESSION = 6
_inflight: Dict[str, "asyncio.Task[Dict[str, Any]]"] = {}


def _remember(cache_key: str, sol: Dict[str, Any], front: bool = True) -> None:
    """Keeps `sol` among the session's programs: first, or second when written ahead (`front` False)."""
    if not cache_key:
        return
    sols = _solutions.setdefault(cache_key, [])
    if sol in sols:
        sols.remove(sol)
    sols.insert(0 if front or not sols else 1, sol)
    del sols[_SOLUTIONS_PER_SESSION:]
    _solutions.move_to_end(cache_key)
    if len(_solutions) > _SOLUTION_CACHE_SIZE:
        _solutions.popitem(last=False)


async def _new_solution(
    client: AsyncOpenAI, *, key: Tuple[str, str, str], cache_key: str, language: str, filename: str,
    prefix: str, suffix: str, problem: str, images: List[str], front: bool = True,
) -> Dict[str, Any]:
    insert = await _write_insert(client, language=language, filename=filename, prefix=prefix, suffix=suffix,
                                 problem=problem, images=images)
    # "steps": the pair (correct and buggy options) of each step, by _step_key; "continued": the next
    # look-ahead has been started (see _prefetch_steps).
    sol: Dict[str, Any] = {"key": key, "language": language, "prefix": prefix, "suffix": suffix,
                           "insert": insert, "steps": {}, "continued": False}
    _remember(cache_key, sol, front)
    return sol


async def _solution_for(
    client: AsyncOpenAI, *, cache_key: str, language: str, filename: str, prefix: str, suffix: str,
    problem: str, images: List[str],
) -> Tuple[Dict[str, Any], str, bool]:
    """(program, what it inserts at this cursor, whether it was reused). Reuses the session's program
    while the student's code matches it, or the one being written for an earlier keystroke."""
    key = (language, filename, _problem_key(problem, images))
    for sol in list(_solutions.get(cache_key, [])) if cache_key else []:
        insert = _insert_for(sol, prefix, suffix) if sol["key"] == key else None
        if insert is not None:
            _remember(cache_key, sol)
            return sol, insert, True
    task = _inflight.get(cache_key) if cache_key else None
    if task is not None:
        try:
            sol = await asyncio.shield(task)
            insert = _insert_for(sol, prefix, suffix) if sol["key"] == key else None
            if insert is not None:
                return sol, insert, True
        except Exception:
            pass
    # Shielded: when the request times out or the student types on, the program is still finished and cached.
    task = asyncio.create_task(_new_solution(
        client, key=key, cache_key=cache_key, language=language, filename=filename, prefix=prefix,
        suffix=suffix, problem=problem, images=images))
    if cache_key:
        _inflight[cache_key] = task
        task.add_done_callback(lambda t: _inflight.pop(cache_key, None) if _inflight.get(cache_key) is t else None)
    sol = await asyncio.shield(task)
    return sol, sol["insert"], False


_CLOSERS = ("}", "};", "})", "});", "end")
# Clauses that continue the statement above them at the same level (if/else, try/except); a closing
# brace and the while of a do/while are handled where they are matched.
_CONTINUATIONS = ("elif", "else", "except", "finally", "catch")
# Most lines a run of simple statements may span; a block (function, loop, if) is shown whole.
MULTILINE_SIMPLE_MAX_LINES = int(os.getenv("MULTILINE_SIMPLE_MAX_LINES", "5"))
_LINE_COMMENT_RE = re.compile(r"\s+(#|//).*$")


def _copilot_portion(insert: str, prefix: str, max_lines: int) -> str:
    """The part of the insert Copilot would show, so the student goes step by step: up to the first
    line with code (single-line), or one step (multi-line). A step is the first statement's whole
    block when it opens one (a function, loop or if, with its else/closing brace), otherwise the
    simple statements from there up to a blank line, a block or MULTILINE_SIMPLE_MAX_LINES lines."""
    lines = insert.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return ""
    if max_lines <= 1:
        return "\n".join(lines[:first + 1])

    def indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" \t"))

    def opens_block(ln: str) -> bool:
        return _LINE_COMMENT_RE.sub("", ln).rstrip().endswith((":", "{"))

    line_head = prefix.rsplit("\n", 1)[-1]
    on_line = first == 0  # the first code line continues the line the cursor is on
    base = indent(line_head + lines[0]) if on_line else indent(lines[first])

    # Split the insert into statements at the base level: each is its first line plus the deeper
    # lines under it, and the closers and continuation clauses that belong to it.
    stmts: List[List[str]] = []
    for i in range(first, len(lines)):
        ln = lines[i]
        if not ln.strip():
            nxt = next((x for x in lines[i + 1:] if x.strip()), None)
            if nxt is None or indent(nxt) <= base:
                break  # a blank line before code at this level ends the step
            stmts[-1].append(ln)
            continue
        if i > first and indent(ln) < base:
            if ln.strip() in _CLOSERS:
                stmts[-1].append(ln)  # closes the block the cursor is in
            break
        if i > first and indent(ln) == base:
            text = ln.strip()
            word = text.split("(")[0].split()[0].rstrip(":{")
            header = (line_head + stmts[0][0]) if len(stmts) == 1 and on_line else stmts[-1][0]
            compound = len(stmts[-1]) > 1 or opens_block(header)
            continues = text.startswith("}") or word in _CONTINUATIONS or (
                word == "while" and header.strip().startswith("do"))
            if text == "{" or (compound and continues):
                stmts[-1].append(ln)
            else:
                stmts.append([ln])
            continue
        if stmts:
            stmts[-1].append(ln)
        else:
            stmts.append([ln])

    def is_block(k: int) -> bool:
        head = line_head + stmts[k][0] if k == 0 and on_line else stmts[k][0]
        return opens_block(head) or (len(stmts[k]) > 1 and stmts[k][1].strip() == "{")

    if is_block(0):
        out = stmts[0]
    else:
        out = []
        for k, stmt in enumerate(stmts):
            if k and (is_block(k) or sum(1 for x in out + stmt if x.strip()) > MULTILINE_SIMPLE_MAX_LINES):
                break
            out += stmt
    # Keep any blank lines before the first code line (the insert may start with a newline).
    out = lines[:first] + out
    kept, n = [], 0
    for ln in out:
        n += bool(ln.strip())
        if n > max_lines:
            break
        kept.append(ln)
    return "\n".join(kept).rstrip()


def _suggestion(insert: str, prefix: str, suffix: str, max_lines: int) -> str:
    return clean_completion(_copilot_portion(insert, prefix, max_lines), prefix, suffix, max_lines)


# ─── Pairs ahead of time ─────────────────────────────────────────────────────
# A look-ahead is cut into steps, one suggestion after another, so the next suggestions are known
# before the student asks for them. Each step's pair (correct and buggy options) is written once and
# stored on the look-ahead by where the step starts, and after a pair is served the next PREFETCH_STEPS
# are written in the background: a student who accepts a suggestion or types along finds the next one
# ready. When fewer steps than that are left, the next look-ahead is written from where the student
# will be at its end.
PREFETCH_STEPS = int(os.getenv("PREFETCH_STEPS", "2"))
_background: "set[asyncio.Task[None]]" = set()  # holds work written ahead until it is done


def _keep(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


def _step_key(max_lines: int, prefix: str, insert: str) -> Tuple[int, int]:
    """Where the step's code starts in the program, so the cursor right after the previous step and
    the cursor after Enter and the editor's indentation find the same pair."""
    return max_lines, len(prefix) + len(insert) - len(insert.lstrip())


async def _make_pair(
    client: AsyncOpenAI, sol: Dict[str, Any], ctx: Dict[str, Any], prefix: str, suffix: str, insert: str,
    max_lines: int,
) -> Dict[str, Any]:
    correct = _suggestion(insert, prefix, suffix, max_lines)
    pair: Dict[str, Any] = {"correct": correct, "bug": None}
    if not ctx["skills"] or not correct:
        return pair
    try:
        pair["bug"] = await bug_injection.make_buggy_completions(
            client, language=sol["language"], prefix=prefix, suffix=suffix, correct=correct,
            skills=ctx["skills"], count=ctx["bug_count"], recent=ctx["recent"], problem=ctx["problem"],
            clean=lambda t: _drop_new_comments(clean_completion(t, prefix, suffix, max_lines), correct),
        )
    except Exception as e:
        logger.warning("Buggy suggestion failed: %s", e)
        pair["bug"] = {"no_bug": "error"}
    return pair


def _pair_task(
    client: AsyncOpenAI, sol: Dict[str, Any], ctx: Dict[str, Any], prefix: str, suffix: str, insert: str,
    max_lines: int,
) -> "asyncio.Task[Dict[str, Any]]":
    """The step's pair, being written or done; started here if it is neither."""
    key = _step_key(max_lines, prefix, insert)
    task = sol["steps"].get(key)
    if task is None or task.cancelled():
        task = asyncio.create_task(_make_pair(client, sol, ctx, prefix, suffix, insert, max_lines))
        sol["steps"][key] = task
    return task


async def _write_ahead(
    client: AsyncOpenAI, sol: Dict[str, Any], ctx: Dict[str, Any], *, cache_key: str, prefix: str,
    suffix: str, max_lines: int, images: List[str],
) -> None:
    """Writes the look-ahead and first pair for code the student does not have yet: the end of the
    current look-ahead, or a buggy option they may accept."""
    if any(_insert_for(s, prefix, suffix) is not None for s in _solutions.get(cache_key, [])):
        return
    try:
        nsol = await _new_solution(
            client, key=sol["key"], cache_key=cache_key, language=sol["language"], filename=ctx["filename"],
            prefix=prefix, suffix=suffix, problem=ctx["problem"], images=images, front=False)
        if nsol["insert"].strip():
            await _pair_task(client, nsol, ctx, prefix, suffix, nsol["insert"], max_lines)
    except Exception as e:
        logger.info("Look-ahead not written ahead: %s", e)


def _prefetch_steps(
    client: AsyncOpenAI, sol: Dict[str, Any], ctx: Dict[str, Any], prefix: str, insert: str, max_lines: int,
    *, cache_key: str, images: List[str],
) -> None:
    """Starts the pairs of the PREFETCH_STEPS steps after the one at this cursor (each step's cursor
    is right after the step before it), and the next look-ahead when fewer steps are left."""
    program = _program(sol)
    pos, end = len(prefix), len(prefix) + len(insert)
    for k in range(PREFETCH_STEPS + 1):
        rest = program[pos:end]
        if not rest.strip():
            break
        if k:
            _pair_task(client, sol, ctx, program[:pos], program[end:], rest, max_lines)
        cut = _copilot_portion(rest, program[:pos], max_lines)
        if not cut:
            return
        pos += len(cut)
    else:
        return
    if cache_key and not sol["continued"] and insert.strip():
        sol["continued"] = True
        _keep(_write_ahead(client, sol, ctx, cache_key=cache_key, prefix=program[:end], suffix=program[end:],
                           max_lines=max_lines, images=images))


def _adapt(cached_correct: str, text: str, correct: str) -> str:
    """An option of a pair stored for another cursor on the same step (e.g. before or after Enter),
    with this cursor's leading newline and indentation."""
    if cached_correct == correct:
        return text
    return correct[:len(correct) - len(correct.lstrip())] + text.lstrip()


async def inline_completion(
    client: AsyncOpenAI,
    *,
    language: str,
    filename: str,
    prefix: str,
    suffix: str,
    max_lines: int = 1,
    bug_skills: Optional[List[Dict[str, Any]]] = None,
    bug_count: int = 1,
    recent_bugs: Optional[List[str]] = None,
    problem: str = "",
    problem_images: Optional[List[str]] = None,
    cache_key: str = "",
    session_skills: Optional[List[Dict[str, Any]]] = None,
    branch_prefetch: bool = False,
) -> Dict[str, Any]:
    """{"correct", "bugs": [{"buggy", "bug_skill", "bug_plan"}], "check", ...}. The correct suggestion is
    cut from a look-ahead (see above); with selected bug skills up to `bug_count` buggy versions are
    written from it (each a different bug type), or taken from the pairs written ahead (see "Pairs
    ahead of time"). `problem` is the task as plain text and `problem_images` its images (data URLs for
    its "[Image N]" markers). `cache_key` (the session) lets later keystrokes reuse the look-ahead.
    `recent_bugs` are the session's latest bug type ids (newest first), so the bug types vary; the
    result's "choice" records how they were chosen. `session_skills` are the bug skills pairs written
    ahead use (default `bug_skills`; they differ for single-option suggestions, which draw per request
    whether to be buggy). `branch_prefetch` also writes ahead for a student who accepts a buggy option
    and keeps it (no correcting feedback)."""
    images = problem_images or []
    ctx = {"skills": session_skills if session_skills is not None else (bug_skills or []),
           "bug_count": bug_count, "recent": list(recent_bugs or []), "problem": problem, "filename": filename}
    sol, insert, reused = await _solution_for(
        client, cache_key=cache_key, language=language, filename=filename, prefix=prefix, suffix=suffix,
        problem=problem, images=images)
    task = _pair_task(client, sol, ctx, prefix, suffix, insert, max_lines)
    _prefetch_steps(client, sol, ctx, prefix, insert, max_lines, cache_key=cache_key, images=images)
    pair = await asyncio.shield(task)
    if ctx["skills"] and len((pair["bug"] or {}).get("bugs", [])) < min(ctx["bug_count"], len(ctx["skills"])):
        # Fewer buggy options than the problem asks for: not kept, so the next request here writes them again.
        key = _step_key(max_lines, prefix, insert)
        if sol["steps"].get(key) is task:
            del sol["steps"][key]
    correct = _suggestion(insert, prefix, suffix, max_lines)
    if pair["correct"].strip() != correct.strip():
        # Stored for a cursor where the step is cut differently: write this cursor's own pair.
        pair = await _make_pair(client, sol, ctx, prefix, suffix, insert, max_lines)
        correct = pair["correct"]
    result: Dict[str, Any] = {"correct": correct, "bugs": [], "check": {"reused": reused},
                              "program_insert": insert[:6000]}
    bug = pair["bug"] if bug_skills and correct else None
    if not bug:
        return result
    if bug.get("no_bug"):
        if bug["no_bug"] != "none_fits":
            logger.info("Completion without a buggy option (%s)", bug["no_bug"])
        return {**result, "no_bug": bug["no_bug"], "choice": bug.get("choice")}
    bugs = [{**b, "buggy": _adapt(pair["correct"], b["buggy"], correct)} for b in bug["bugs"]]
    if branch_prefetch and cache_key:
        for b in bugs:
            _keep(_write_ahead(client, sol, ctx, cache_key=cache_key, prefix=prefix + b["buggy"], suffix=suffix,
                               max_lines=max_lines, images=images))
    return {**result, "bugs": bugs, "choice": bug.get("choice")}


async def warm_up(client: AsyncOpenAI, *, cache_key: str, **kwargs: Any) -> None:
    """Writes the look-ahead and first pairs for where the student's cursor is, before they ask (the
    editor calls it when it opens and when the student clicks somewhere new)."""
    if not cache_key or cache_key in _inflight:
        return
    try:
        await inline_completion(client, cache_key=cache_key, **kwargs)
    except Exception as e:
        logger.info("Warm-up failed: %s", e)

# ─── Agent (condition 3) ─────────────────────────────────────────────────────

_TOOL_DEFS: Dict[str, Dict[str, Any]] = {
    "list_files": {
        "description": "List the files in the student's workspace with their sizes.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "read_file": {
        "description": "Read a file from the workspace (returns it with line numbers).",
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": ["filename"],
        },
    },
    "edit_file": {
        "description": (
            "Replace one exact occurrence of old_string with new_string in a file. old_string must "
            "match the file exactly (including indentation) and be unique; include surrounding lines "
            "to make it unique. Use this for targeted changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["filename", "old_string", "new_string"],
        },
    },
    "write_file": {
        "description": "Create a new file, or overwrite an existing file with the full new content.",
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": "string"}, "content": {"type": "string"}},
            "required": ["filename", "content"],
        },
    },
    "delete_file": {
        "description": "Delete a file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": ["filename"],
        },
    },
    "run_code": {
        "description": (
            "Run the program and return stdout/stderr. Runs `filename` as the entry point "
            "(null = the active file). If the program reads input (input(), scanf), pass the "
            "input lines in `stdin`, each ending with a newline; otherwise pass null."
        ),
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": ["string", "null"]}, "stdin": {"type": ["string", "null"]}},
            "required": ["filename", "stdin"],
        },
    },
}

_READ_TOOLS = ["list_files", "read_file"]
_WRITE_TOOLS = _READ_TOOLS + ["edit_file", "write_file", "delete_file", "run_code"]
_MODE_TOOLS = {"ask": _READ_TOOLS, "plan": _READ_TOOLS, "agent": _WRITE_TOOLS, "debug": _WRITE_TOOLS}

_BASE_PROMPT = """You are an AI coding agent built into a browser IDE, working with a student the way Cursor's agent does.
Workspace language: {language}. Active file: {active_file}.
In Agent and Debug modes you can create new files with write_file (flat names, no folders; allowed types: {extensions}).
Keep replies concise and use Markdown. Refer to files by name. Never claim you ran or changed something you did not."""

_MODE_PROMPTS = {
    "ask": """Mode: ASK (chat only). Answer questions and explain code. You cannot modify files in this mode;
if the student wants changes, show short snippets and suggest switching to Agent mode.""",
    "plan": """Mode: PLAN. Investigate the code, then reply with a short numbered implementation plan
(what to change, in which file, and why). Do not write the full solution and do not modify files.
End by telling the student they can click "Build this plan" to have the agent carry it out.""",
    "agent": """Mode: AGENT. Make the changes the student asks for directly in their files using the tools.
Keep working until the request is fully done: read the code you need, make the edits, then run the
code (with the problem's test cases as stdin when there is a problem) and fix what fails. Do not stop
to ask permission for steps you can take yourself; ask only when the request is truly ambiguous.
Prefer edit_file for targeted changes and write_file for new files or full rewrites. Finish with a
brief summary of what you changed and how you checked it. If the program reads input, end the summary
with the exact input to type when they press Run (e.g. an example test case), since it waits for it.""",
    "debug": """Mode: DEBUG. Find and fix the bug systematically. Always use run_code yourself; never ask
the student whether to run it.
1. Reproduce: call run_code first and read the actual error/output.
2. Form hypotheses about the root cause; add temporary print statements if you need more evidence.
3. Fix the root cause (not the symptom) with the smallest change.
4. Call run_code again to verify the fix, then remove any temporary prints.
Finish with: the cause, the fix, and the output that shows it works.""",
}


def _number_lines(content: str) -> str:
    lines = content.split("\n")
    if len(lines) > 1 and lines[-1] == "":  # trailing newline is not an extra line
        lines.pop()
    return "\n".join(f"{i:4}| {ln}" for i, ln in enumerate(lines, 1))


def _short_diff(before: str, after: str, filename: str, limit: int = 6000) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{filename}", tofile=f"b/{filename}",
        )
    )
    return diff if len(diff) <= limit else diff[:limit] + "\n… (diff truncated)"


def build_system_prompt(
    mode: str, language: str, active_file: str, skills: List[Dict[str, str]], problem: str = ""
) -> str:
    parts = [_BASE_PROMPT.format(
        language=language, active_file=active_file or "(none)",
        extensions=", ".join("." + e for e in allowed_extensions(language)),
    ), _MODE_PROMPTS[mode]]
    if problem.strip():
        parts.append(
            "## The student's problem\nThe student is solving this problem. Code you write must solve it "
            "exactly as stated, pass its test cases (same input and output format) and respect its "
            "constraints.\n\n" + _problem_block(problem).strip()
        )
    if skills:
        parts.append(
            "## Skills\nFollow these skills whenever they are relevant:\n\n"
            + "\n\n".join(f"### {s.get('name', 'Skill')}\n{s.get('instructions', '').strip()}" for s in skills)
        )
    return "\n\n".join(parts)


def _workspace_context(ws: Dict[str, str], terminal_output: Optional[str]) -> str:
    total = sum(len(c) for c in ws.values())
    if total <= 24000:
        body = "\n\n".join(f"### {name}\n```\n{_number_lines(c)}\n```" for name, c in ws.items())
    else:
        body = "Files: " + ", ".join(ws) + " (use read_file to see contents)"
    ctx = "<workspace>\n" + body + "\n</workspace>"
    if terminal_output and terminal_output.strip():
        ctx += "\n<terminal_output>\n" + terminal_output[-3000:] + "\n</terminal_output>"
    return ctx


RunCode = Callable[[str, List[Dict[str, str]], str, Optional[str]], Awaitable[Dict[str, Any]]]


class AgentTurn:
    """One student message → streamed events, plus a log record for research analysis."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        mode: str,
        message: str,
        history: List[Dict[str, str]],
        files: List[Dict[str, str]],
        active_file: str,
        language: str,
        skills: List[Dict[str, str]],
        terminal_output: Optional[str],
        run_code: RunCode,
        problem: str = "",
        problem_images: Optional[List[str]] = None,
        run_check: Optional[CheckRun] = None,
    ):
        self.client = client
        self.mode = mode if mode in AGENT_MODES else "ask"
        self.message = message
        self.history = history
        self.ws: Dict[str, str] = {f["filename"]: f["content"] for f in files}
        self.initial = dict(self.ws)
        self.active_file = active_file if active_file in self.ws else next(iter(self.ws), "")
        self.language = language
        self.skills = skills
        self.terminal_output = terminal_output
        self.run_code = run_code
        self.problem = problem
        self.problem_images = problem_images or []
        self.run_check = run_check
        self.last_run_entry = ""  # the file the agent last ran: the program the closing check runs
        # The closing summary of a turn that changed files. It is not streamed: it belongs to the
        # versions the student chooses between (the buggy one may get its own, wrong, description).
        self.final_text = ""
        self.record: Dict[str, Any] = {
            "turn_id": uuid.uuid4().hex[:12],
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "model": AGENT_MODEL,
            "message": message,
            "skills": [s.get("name", "") for s in skills],
            "response": "",
            "tool_calls": [],
            "edits": [],
            "usage": {},
        }

    # Tool implementations -------------------------------------------------

    def _check_name(self, name: str) -> Optional[str]:
        if not _FILENAME_RE.match(name or ""):
            return "Invalid filename: use a simple name like utils.py (no folders)."
        allowed = allowed_extensions(self.language)
        if name not in self.ws and name.rsplit(".", 1)[-1].lower() not in allowed:
            return "Invalid file type. Allowed: " + ", ".join("." + e for e in allowed)
        return None

    async def _tool(self, name: str, args: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Return (result text for the model, optional event for the client)."""
        fn = args.get("filename") or ""
        if name == "list_files":
            return "\n".join(f"{n} ({len(c.splitlines())} lines)" for n, c in self.ws.items()) or "(no files)", None
        if name == "read_file":
            if fn not in self.ws:
                return f"File not found: {fn}. Files: {', '.join(self.ws)}", None
            return _number_lines(self.ws[fn]), None
        if name == "edit_file":
            if fn not in self.ws:
                return f"File not found: {fn}", None
            if not isinstance(args.get("old_string"), str) or not isinstance(args.get("new_string"), str):
                return "edit_file needs both old_string and new_string. Nothing was changed.", None
            old, new = args["old_string"], args["new_string"]
            if self.ws[fn].count(old) == 0 and old.strip() and self.ws[fn].count(old.rstrip()) == 1:
                # Model often adds stray trailing whitespace; match without it.
                old, new = old.rstrip(), new.rstrip() if new.endswith(old[len(old.rstrip()):]) else new
            if self.ws[fn].strip() and not self.ws[fn].replace(old, new, 1).strip():
                return ("That edit would erase the whole file, so it was not applied. Use write_file "
                        "with the full new content, or delete_file to remove the file."), None
            count = self.ws[fn].count(old) if old else 0
            if count != 1:
                return (f"old_string matched {count} times in {fn}; it must match exactly once. "
                        "Re-read the file and include more surrounding lines."), None
            return self._apply(fn, self.ws[fn].replace(old, new, 1), "edit")
        if name == "write_file":
            err = self._check_name(fn)
            if err:
                return err, None
            if fn not in self.ws and len(self.ws) >= MAX_FILES:
                return f"Workspace already has {MAX_FILES} files.", None
            return self._apply(fn, args.get("content", ""), "create" if fn not in self.ws else "write")
        if name == "delete_file":
            if fn not in self.ws:
                return f"File not found: {fn}", None
            if len(self.ws) == 1:
                return "Cannot delete the only file in the workspace.", None
            before = self.ws.pop(fn)
            self.record["edits"].append({"filename": fn, "action": "delete", "diff": _short_diff(before, "", fn)})
            return f"Deleted {fn}.", {"type": "file", "action": "delete", "filename": fn}
        if name == "run_code":
            entry = fn or self.active_file
            if entry not in self.ws:
                return f"File not found: {entry}", None
            self.last_run_entry = entry
            files = [{"filename": n, "content": c} for n, c in self.ws.items()]
            stdin = args.get("stdin") if isinstance(args.get("stdin"), str) else None
            res = await self.run_code(self.ws[entry], files, self.language, stdin)
            out = (res.get("output") or "")[-4000:]
            errout = (res.get("error") or "")[-4000:]
            text = f"success={res.get('success')}\nstdout:\n{out or '(empty)'}\nstderr:\n{errout or '(empty)'}"
            return text, {"type": "run", "filename": entry, "success": bool(res.get("success")),
                          "output": out, "error": errout}
        return f"Unknown tool: {name}", None

    def _apply(self, fn: str, content: str, action: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        if len(content) > MAX_FILE_CHARS:
            return f"Content too long (max {MAX_FILE_CHARS} characters).", None
        before = self.ws.get(fn, "")
        self.ws[fn] = content
        diff = _short_diff(before, content, fn)
        self.record["edits"].append({"filename": fn, "action": action, "diff": diff})
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        return (f"{'Created' if action == 'create' else 'Updated'} {fn} (+{added} -{removed}).",
                {"type": "file", "action": action, "filename": fn, "content": content,
                 "added": added, "removed": removed})

    def changes(self) -> List[Dict[str, Any]]:
        """The turn's net file changes (after run()): [{filename, action, content, diff}], content None for deletes."""
        out = []
        for name in self.record.get("files_changed", []):
            before, after = self.initial.get(name), self.ws.get(name)
            action = "delete" if after is None else "create" if before is None else "write"
            out.append({"filename": name, "action": action, "content": after,
                        "diff": _short_diff(before or "", after or "", name)})
        return out

    # Main loop ------------------------------------------------------------

    async def _steps(
        self, items: List[Dict[str, Any]], tools: List[Dict[str, Any]], instructions: str,
        model_args: Dict[str, Any], usage: Dict[str, int], max_steps: int,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Model steps (with their tool calls) until the model stops calling tools or max_steps."""
        for _step in range(max_steps):
            stream = await self.client.responses.create(
                model=AGENT_MODEL, instructions=instructions, input=items,  # pyright: ignore[reportArgumentType]
                tools=tools, store=False, stream=True, **model_args,  # pyright: ignore[reportArgumentType]
            )
            text, output = "", []
            # Once files have changed, this step's text may be the closing summary: hold it until we
            # know (a step that also calls tools is only a preamble, sent when the step ends).
            hold = bool(self.record["edits"])
            async for ev in stream:
                if ev.type == "response.output_text.delta":
                    text += ev.delta
                    if not hold:
                        yield {"type": "text", "delta": ev.delta}
                elif ev.type in ("response.completed", "response.incomplete"):
                    output = ev.response.output
                    if ev.response.usage:
                        usage["prompt_tokens"] += ev.response.usage.input_tokens
                        usage["completion_tokens"] += ev.response.usage.output_tokens
                        details = ev.response.usage.output_tokens_details
                        usage["reasoning_tokens"] += getattr(details, "reasoning_tokens", 0) or 0
                elif ev.type in ("response.failed", "error"):
                    err = getattr(getattr(ev, "response", None), "error", None) or ev
                    raise RuntimeError(getattr(err, "message", None) or str(err))
            if text:
                self.record["response"] += ("\n\n" if self.record["response"] else "") + text

            # Kept in the input, so a turn sent back after the closing check sees its own summary.
            items += [it.model_dump(exclude_none=True) for it in output]
            calls = [it for it in output if it.type == "function_call"]
            if not calls:
                if hold:
                    self.final_text = text
                return
            if hold and text:
                yield {"type": "text", "delta": text}
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                label = args.get("filename") or ""
                yield {"type": "tool", "name": c.name, "filename": label}
                result, event = await self._tool(c.name, args)
                self.record["tool_calls"].append({"name": c.name, "filename": label, "result": result[:500]})
                if event:
                    yield event
                items.append({"type": "function_call_output", "call_id": c.call_id, "output": result})
        note = "\n\n_(Stopped after the maximum number of steps.)_"
        self.record["response"] += note
        yield {"type": "text", "delta": note}

    def _changed(self) -> List[str]:
        return sorted(n for n in set(self.initial) | set(self.ws) if self.initial.get(n) != self.ws.get(n))

    def _program_entry(self) -> str:
        """The file the closing check runs: the one the agent last ran, else the active file."""
        for fn in (self.last_run_entry, self.active_file):
            if fn in self.ws:
                return fn
        return ""

    async def _check(self, tests: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        entry = self._program_entry()
        if not entry or self.run_check is None:
            return None
        files = [{"filename": n, "content": c} for n, c in self.ws.items()]
        res = await _run_tests(self.ws[entry], self.language, tests, self.run_check, files)
        if res is not None:
            res["filename"] = entry
        return res

    async def run(self) -> AsyncIterator[Dict[str, Any]]:
        # Responses API, not Chat Completions: there gpt-6 models only take function tools with
        # reasoning turned off. store=False keeps nothing at OpenAI; the encrypted reasoning items are
        # sent back each step instead, so the model keeps its chain of thought across tool calls (Codex).
        # strict: the model must send every argument (a missing new_string once erased files)
        tools = [
            {"type": "function", "name": n, "strict": True, **_TOOL_DEFS[n],
             "parameters": {**_TOOL_DEFS[n]["parameters"], "additionalProperties": False}}
            for n in _MODE_TOOLS[self.mode]
        ]
        instructions = build_system_prompt(self.mode, self.language, self.active_file, self.skills, self.problem)
        items: List[Dict[str, Any]] = []
        if self.problem_images and self.problem.strip():
            # Instructions are text only, so the problem's images go first in the input (a stable prefix).
            items.append({"role": "user", "content": [{"type": "input_text", "text": _IMAGES_NOTE}] + [
                {"type": "input_image", "image_url": u, "detail": "auto"} for u in self.problem_images]})
        for h in self.history[-20:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                items.append({"role": h["role"], "content": str(h["content"])[:8000]})
        items.append({"role": "user", "content": _workspace_context(self.ws, self.terminal_output)
                      + "\n\n" + self.message})
        if is_reasoning_model(AGENT_MODEL):
            model_args: Dict[str, Any] = {"reasoning": {"effort": AGENT_REASONING_EFFORT},
                                          "include": ["reasoning.encrypted_content"]}
        else:
            model_args = {"temperature": 0.2}

        # The problem's test cases are read out while the agent works, for the closing check.
        checking = self.mode in ("agent", "debug") and bool(self.problem.strip()) and self.run_check is not None
        tests_task = asyncio.create_task(problem_tests(self.client, self.problem, self.problem_images)) if checking else None

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
        async for ev in self._steps(items, tools, instructions, model_args, usage, AGENT_MAX_STEPS):
            yield ev

        # Closing check: the system (not the agent) runs the changed program on the problem's test
        # cases; on a failure the agent is sent back once to fix it, then the program is checked again.
        if tests_task is not None:
            tests = await tests_task if self._changed() else []
            res = await self._check(tests) if tests else None
            check: Dict[str, Any] = {"verified": None, "total": len(tests), "sent_back": False}
            if res and res["failures"]:
                f = res["failures"][0]
                check.update(sent_back=True, first_failure=f, first_passed=res["passed"])
                yield {"type": "tool", "name": "check_tests", "filename": res["filename"]}
                items.append({"role": "user", "content": (
                    f"Automatic check: {res['filename']} was run on the problem's test cases and fails "
                    f"{len(res['failures'])} of {res['total']}. For example:\nInput:\n{f['input']}\n"
                    f"Expected output:\n{f['expected']}\nActual output:\n{f['got'] or '(none)'}"
                    + (f"\nErrors:\n{f['error']}" if f["error"].strip() else "")
                    + "\nFix the code so it passes every test case, run it to confirm, then finish with "
                    "your summary of all the changes in this turn.")})
                async for ev in self._steps(items, tools, instructions, model_args, usage, AGENT_FIX_STEPS):
                    yield ev
                res = await self._check(tests)
            if res:
                check.update(verified=not res["failures"], passed=res["passed"], filename=res["filename"],
                             failure=res["failures"][0] if res["failures"] else None)
            self.record["solution_check"] = check

        self.record["usage"] = usage
        self.record["files_changed"] = self._changed()
        yield {"type": "done", "turn_id": self.record["turn_id"]}
