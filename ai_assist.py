"""AI assistance for study conditions 2 and 4 (inline ghost-text completion, single- and
multi-line) and 3 (coding agent).

Kept free of main.py imports: callers pass in the OpenAI client and a code runner.
"""

import difflib
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from openai import NOT_GIVEN, AsyncOpenAI

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
MAX_FILES = 20
MAX_FILE_CHARS = 20000
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# Same file types the student's explorer allows (templates/workspace.js LANG_EXT / EXTRA_EXT).
_LANG_EXT = {"python": ["py"], "c": ["c", "h"], "cpp": ["cpp", "hpp", "h", "cc"], "java": ["java"], "javascript": ["js"]}
_EXTRA_EXT = ["txt", "md", "csv", "json"]


def allowed_extensions(language: str) -> List[str]:
    return _LANG_EXT.get(language, []) + _EXTRA_EXT


# ─── Inline completion (conditions 2 and 4) ──────────────────────────────────

PROBLEM_MAX_CHARS = 6000

_COMPLETION_BASE = (
    "You are a code completion engine like GitHub Copilot. You receive a {language} file "
    "with a <CURSOR> marker. {reply_rule} Write only the code to insert at <CURSOR>: no "
    "explanations, no markdown fences, never repeat code that is already before or after "
    "the cursor (in particular, never repeat the line above the cursor). {length_rule} Match the file's indentation. "
    "If a <problem> is given, the student is solving it: the correct code must solve that problem "
    "exactly as stated, pass its test cases (same input and output format) and respect its constraints."
)
_PLAIN_REPLY = "Reply with ONLY the text to insert; if nothing useful can be added, reply with nothing."
# Every suggestion is a pair (see bug_injection.py): one call writes both, so both arrive together.
_PAIR_REPLY = (
    "Write two versions of the suggestion and reply with a JSON object:\n"
    '  "correct": the suggestion, fully correct (it handles edge cases such as empty input, zero '
    'or None/null where they matter) and following every rule here;\n'
    '  "bug_type": the id of the ONE bug type in <bug_types> that fits this spot best, chosen by the '
    'rules on how to choose the bug below, or "none" if no bug type fits here (then use "" for '
    '"buggy");\n'
    '  "bug_plan": one sentence naming the exact change you will make, why it is that bug type, and '
    "the input or situation that exposes it;\n"
    '  "buggy": the correct suggestion changed to contain exactly one bug of that type (it must '
    'differ from "correct"), starting '
    "at the same place, with the same shape and length, and looking like a normal, correct "
    "suggestion at a glance (no comments, hints or markers). The bug must make the code actually "
    "wrong (wrong result, crash, or failure to compile), not just slower or longer.\n"
    'If nothing useful can be added, use "" for both.'
)
_SINGLE_LINE_RULE = (
    "Suggest a single line only: finish the current line, or, if the cursor is on an empty "
    "line, write the one next line."
)
# Condition 4: Copilot's multi-line mode.
_MULTI_LINE_RULE = (
    "If code follows the cursor on the same line, only finish that line. Otherwise write the "
    "next logical chunk, up to {max_lines} lines: e.g. a whole function body, loop or if-block "
    "after its header, or the next few related statements. Stop at a natural boundary rather "
    "than writing the rest of the program."
)


def _problem_block(problem: str) -> str:
    problem = (problem or "").strip()
    return f"<problem>\n{problem[:PROBLEM_MAX_CHARS]}\n</problem>\n\n" if problem else ""


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

    # Drop any trailing overlap with the code after the cursor.
    tail = suffix.lstrip("\n")
    for k in range(min(len(text), len(tail)), 3, -1):
        if text.endswith(tail[:k]):
            text = text[:-k]
            break

    # A suggestion that starts on the next line keeps one newline (only after a non-blank line head).
    body = text.lstrip("\n")
    lead = "\n" if body != text and line_head.strip() else ""
    lines = body.rstrip().split("\n")[:max_lines]
    return (lead + "\n".join(lines)).rstrip()


async def inline_completion(
    client: AsyncOpenAI,
    *,
    language: str,
    filename: str,
    prefix: str,
    suffix: str,
    max_lines: int = 1,
    bug_skills: Optional[List[Dict[str, Any]]] = None,
    problem: str = "",
) -> Dict[str, Any]:
    """{"correct": text, "buggy": text or None, "bug_skill": skill or None}. With selected bug
    skills, one call writes both versions; without any, only the correct one. `problem` is the
    task the student is solving (statement, test cases, constraints) as plain text."""
    prefix = prefix[-4000:]
    suffix = suffix[:1500]
    length_rule = _SINGLE_LINE_RULE if max_lines <= 1 else _MULTI_LINE_RULE.format(max_lines=max_lines)
    paired = bool(bug_skills)
    system = _COMPLETION_BASE.format(
        language=language, length_rule=length_rule, reply_rule=_PAIR_REPLY if paired else _PLAIN_REPLY,
    )
    if paired:
        system += "\n\n" + bug_injection.BUG_CHOICE_RULES + "\n\n<bug_types>\n" + bug_injection.bug_guides(bug_skills or []) + "\n</bug_types>"
    max_tokens = 200 if max_lines <= 1 else 60 * max_lines
    resp = await client.chat.completions.create(
        **chat_args(COMPLETION_MODEL, temperature=0, max_tokens=max_tokens * 2 + 60 if paired else max_tokens),
        messages=[
            {"role": "system", "content": system},
            # The problem goes in the user message, so the long system prompt stays a cacheable prefix.
            {"role": "user", "content": _problem_block(problem) + f"File: {filename}\n{prefix}<CURSOR>{suffix}"},
        ],
        response_format={"type": "json_object"} if paired else NOT_GIVEN,
    )
    raw = resp.choices[0].message.content or ""
    if not paired:
        return {"correct": clean_completion(raw, prefix, suffix, max_lines), "buggy": None, "bug_skill": None}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        out = {}
    correct = clean_completion(str(out.get("correct") or ""), prefix, suffix, max_lines)
    buggy = _drop_new_comments(clean_completion(str(out.get("buggy") or ""), prefix, suffix, max_lines), correct)
    bug_type = str(out.get("bug_type") or "").strip()
    skill = bug_injection.skill_by_id(bug_skills or [], bug_type)
    if bug_type.lower() == "none":
        # The model judged that no selected bug type fits this line: show the correct one only.
        return {"correct": correct, "buggy": None, "bug_skill": None, "no_bug": "none_fits"}
    if not correct or not buggy or buggy == correct or not skill:
        reason = "same_as_correct" if buggy and buggy == correct else ("unknown_type" if not skill else "empty")
        logger.info("Completion without a buggy option (%s, bug_type=%r)", reason, bug_type)
        return {"correct": correct, "buggy": None, "bug_skill": None, "no_bug": reason}
    return {"correct": correct, "buggy": buggy, "bug_skill": skill, "bug_plan": str(out.get("bug_plan") or "")[:500]}


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
brief summary of what you changed and how you checked it.""",
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

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
        for _step in range(AGENT_MAX_STEPS):
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

            calls = [it for it in output if it.type == "function_call"]
            if not calls:
                if hold:
                    self.final_text = text
                break
            if hold and text:
                yield {"type": "text", "delta": text}
            items += [it.model_dump(exclude_none=True) for it in output]
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
        else:
            note = "\n\n_(Stopped after the maximum number of steps.)_"
            self.record["response"] += note
            yield {"type": "text", "delta": note}

        self.record["usage"] = usage
        self.record["files_changed"] = sorted(
            n for n in set(self.initial) | set(self.ws) if self.initial.get(n) != self.ws.get(n)
        )
        yield {"type": "done", "turn_id": self.record["turn_id"]}
