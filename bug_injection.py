"""Bug generation for the study: every AI suggestion and agent change comes as a pair,
one correct version and one with exactly one bug, shown to the student in random order.

The bug types come from the quiz's selected bug skills (bug_skills/*.md, editable per quiz by
the instructor); for each pair the model picks the selected type that best fits the code. A
separate classifier call (in the background, never seen by the student) then labels the planted
bug with one of the same bug types. The built-in types follow the taxonomy of bugs in LLM-generated
code from Tambon et al., "Bugs in Large Language Models Generated Code: An Empirical Study" (EMSE 2025).

Kept free of main.py imports: callers pass in the OpenAI client and persist the records.
"""

import asyncio
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from llm_params import DEFAULT_MODEL, chat_args

logger = logging.getLogger(__name__)


# Writes the buggy copy of an agent turn's changes (inline suggestions get both versions in one call).
# The same model as the inline pairs (see ai_assist.COMPLETION_MODEL): it plants the most realistic bugs.
BUG_MODEL = os.getenv("BUG_MODEL", "gpt-6-sol")
CLASSIFIER_MODEL = os.getenv("BUG_CLASSIFIER_MODEL", DEFAULT_MODEL)

_classify_semaphore = asyncio.Semaphore(int(os.getenv("BUG_CLASSIFIER_CONCURRENCY", "4")))


# ─── Bug skills (bug_skills/*.md) ───────────────────────────────────────────
# Each bug type is a Markdown file: a small front matter (name, category, description)
# followed by the instructions the AI follows to plant that bug. The built-in files seed
# every quiz; instructors then select, edit, remove, add or upload their own per quiz
# (stored on the quiz as bug_skills). For each pair the model picks the best-fitting selected skill.

BUG_SKILLS_DIR = Path(__file__).parent / "bug_skills"
MAX_BUG_SKILLS = 30
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_")[:60]


def parse_skill_md(text: str, fallback_name: str = "") -> Dict[str, str]:
    """Parse a bug skill file: optional `---` front matter of `key: value` lines, then the instructions."""
    text = text.replace("\r\n", "\n").strip()
    meta: Dict[str, str] = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).split("\n"):
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip().strip('"').strip("'")
        body = m.group(2)
    name = meta.get("name") or fallback_name
    return {
        "name": name,
        "category": meta.get("category") or _slug(name) or "custom",
        "description": meta.get("description", ""),
        "instructions": body.strip(),
    }


def default_bug_skills() -> List[Dict[str, Any]]:
    """The built-in bug skills, all selected."""
    skills = []
    for path in sorted(BUG_SKILLS_DIR.glob("*.md")):
        try:
            skill = parse_skill_md(path.read_text(encoding="utf-8"), fallback_name=path.stem)
        except OSError as e:
            logger.warning("Could not read bug skill %s: %s", path, e)
            continue
        skills.append({"id": path.stem, **skill, "enabled": True})
    return skills


def clean_bug_skills(raw: Any) -> List[Dict[str, Any]]:
    """Validate the instructor's bug skills for a quiz."""
    skills: List[Dict[str, Any]] = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:80]
        instructions = str(item.get("instructions") or "").strip()[:4000]
        if not name or not instructions:
            continue
        skill_id = _slug(str(item.get("id") or "")) or _slug(name) or "bug"
        base, n = skill_id, 2
        while skill_id in seen:
            skill_id, n = f"{base}_{n}", n + 1
        seen.add(skill_id)
        skills.append({
            "id": skill_id,
            "name": name,
            "category": _slug(str(item.get("category") or "")) or _slug(name),
            "description": str(item.get("description") or "").strip()[:300],
            "instructions": instructions,
            "enabled": bool(item.get("enabled", True)),
        })
    return skills[:MAX_BUG_SKILLS]


def quiz_bug_skills(quiz: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """All bug skills configured for a quiz. Quizzes saved before bug skills existed get the built-ins.
    `bug_skills_configured` marks a quiz whose list was saved empty (Firebase drops empty lists)."""
    quiz = quiz or {}
    if "bug_skills" in quiz:
        return clean_bug_skills(quiz.get("bug_skills"))
    return [] if quiz.get("bug_skills_configured") else default_bug_skills()


# How the model chooses which bug to plant (inline suggestions and agent changes). The study teaches
# students to check AI-written code, so the bug should be one a real AI assistant would make here and
# one an attentive student can find, not random sabotage or an obvious typo.
BUG_CHOICE_RULES = """How to choose the bug (decide this before writing any buggy code):
- Realistic: pick the mistake a real AI coding assistant (Copilot, ChatGPT) most plausibly makes \
in THIS code, e.g. misreading the task, an output format or a constraint, an off-by-one, a missed \
edge case, a wrong operator or comparison, a wrong but real library call or argument. Never a \
random change that no assistant would produce.
- Tied to the task: when a <problem> is given, prefer a bug that violates its statement, test \
cases or constraints, e.g. code that still passes the first test case but fails another test case, \
an edge case or the limit given in the constraints.
- Subtle but findable: it must look right at a glance, yet an attentive student can find it by \
reading the code carefully or by running the problem's test cases. Avoid misspelled names and \
other typo-like or syntax bugs unless the chosen bug type requires them.
- It must really change behaviour: on the input named in bug_plan the buggy code gives a different \
result (or crashes) where the correct code does not. A harmless change, an unused extra statement or \
a guard the constraints make unnecessary is not a bug. Never divide by a literal 0 or write similar \
nonsense no assistant would write.
- bug_plan names the change and the input or situation that exposes it, and the buggy code makes \
exactly that change.
- No forced bugs: if none of the bug types fits this code as a mistake an assistant would really \
make here (for example the code is too trivial for that type, or the only possible change would \
be harmless or nonsense), choose "none" instead of forcing one. Then no buggy version is shown."""


def selected_bug_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in skills if s.get("enabled")]


def bug_guides(skills: List[Dict[str, Any]]) -> str:
    """The selected bug skills as a prompt block the model chooses from. A random rotation of the
    list puts every type first equally often (so the first-listed type is not favoured), while
    keeping only len(skills) distinct prompts, which OpenAI's prompt cache can reuse."""
    k = random.randrange(len(skills)) if skills else 0
    return "\n\n".join(
        f'<bug_type id="{s["id"]}" name="{s["name"]}">\n{s.get("description", "")}\n\n{s["instructions"]}\n</bug_type>'
        for s in skills[k:] + skills[:k]
    )


def skill_by_id(skills: List[Dict[str, Any]], skill_id: Any) -> Optional[Dict[str, Any]]:
    skill_id = str(skill_id or "").strip()
    return next((s for s in skills if s["id"] == skill_id), None)


# ─── Buggy version of an agent turn ─────────────────────────────────────────

_AGENT_BUG_SYSTEM = """You help run a study of how students judge AI-written code. An AI coding agent \
just made the correct changes below. Make a second version of those changes that contains exactly \
one bug, so the student can be shown both versions.

1. Pick the ONE bug type from <bug_types> that fits the changed code best, following the rules \
on how to choose the bug below, or "none" if no bug type fits.
2. Plant it by replacing one exact snippet of the changed code with a buggy version. The snippet \
must lie inside the lines the agent added or changed (the "+" lines of the diff).

Rules: exactly one bug of the chosen type; everything else unchanged; the bug must make the code \
actually wrong (wrong result, crash, or failure to compile), not just slower or longer; the buggy \
code must look like a normal, correct change at a glance; no comments, hints or markers about the bug.

Reply with a JSON object, keys in this order:
  "bug_type": the id of the chosen bug type, or "none" (then leave the other keys empty),
  "bug_plan": one sentence naming the exact change you will make, why it is that bug type, and the \
input or situation that exposes it,
  "filename": the file you change,
  "old": a short exact snippet (1 to 3 whole lines, never the whole file) copied from that \
file's new content; it must occur exactly once,
  "new": the replacement containing the bug."""

# Added when this turn may also get a wrong description (DESCRIPTION_ERROR_RATE).
_AGENT_DESCRIPTION_RULES = """The student also sees the agent's description of its changes (in <description>). \
In this turn the buggy version's description also contains exactly one wrong claim, besides the code bug:
- Make it the way a real AI agent misdescribes its own work: for example claiming an edge case or \
constraint is handled when it is not, claiming it tested an input it did not, a wrong explanation of \
the cause of the problem it fixed, or a wrong complexity. Prefer a claim that covers up or misexplains \
the planted bug, e.g. "negative numbers are handled" when the bug drops them.
- The wrong claim is short, confident and subtle: a careful student could check it against the code \
or by running it. Never an obvious contradiction, a hint that something is wrong, or a typo.
- Change or add only that one claim; keep everything else in the description word for word.
- Only if the description is too short to carry any claim, keep it unchanged.
Add two keys to the JSON:
  "description": the buggy version's full description (same tone, length and Markdown),
  "description_error": one sentence naming the wrong claim, or "" if the description is unchanged."""

# Share of agent turns whose buggy version also gets a wrong description (the rest keep the agent's
# own description, so most buggy versions differ only in the code).
DESCRIPTION_ERROR_RATE = float(os.getenv("DESCRIPTION_ERROR_RATE", "0.5"))


_COMMENT_RE = re.compile(r"\s*(#|//).*$", re.M)


def _code_only(snippet: str) -> str:
    """The snippet without line comments and whitespace (approximate: ignores comment markers in strings)."""
    return re.sub(r"\s+", "", _COMMENT_RE.sub("", snippet))


async def make_buggy_agent_version(
    client: AsyncOpenAI,
    *,
    language: str,
    message: str,
    diffs: Dict[str, str],
    files_after: Dict[str, str],
    skills: List[Dict[str, Any]],
    problem: str = "",
    description: str = "",
) -> Optional[Dict[str, Any]]:
    """A buggy copy of the agent's final files, or None when no valid bug could be planted.
    `problem` is the task the student is solving (statement, test cases, constraints). `description`
    is the agent's summary of its changes; the result's "description" is the buggy version's one,
    which on some turns (DESCRIPTION_ERROR_RATE) contains a wrong claim too."""
    if not skills or not files_after:
        return None
    vary_description = bool(description.strip()) and random.random() < DESCRIPTION_ERROR_RATE
    parts = [f"<problem>\n{problem.strip()[:6000]}\n</problem>"] if problem.strip() else []
    parts += [f"Language: {language}.", f"Student's request:\n{message[:3000]}",
             "The agent's changes (unified diffs):\n" + "\n".join(diffs.values())[:12000]]
    for name, content in files_after.items():
        parts.append(f"New content of {name}:\n```\n{content[:8000]}\n```")
    system = _AGENT_BUG_SYSTEM
    if vary_description:
        parts.append(f"<description>\n{description.strip()[:4000]}\n</description>")
        system += "\n\n" + _AGENT_DESCRIPTION_RULES
    messages = [
        {"role": "system", "content": system + "\n\n" + BUG_CHOICE_RULES + "\n\n<bug_types>\n" + bug_guides(skills) + "\n</bug_types>"},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    for _attempt in range(2):
        resp = await client.chat.completions.create(
            **chat_args(BUG_MODEL, temperature=0.4), response_format={"type": "json_object"}, messages=messages,
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            out = {}
        if str(out.get("bug_type") or "").strip().lower() == "none":
            logger.info("No bug type fits this agent turn: %s", str(out.get("bug_plan") or "")[:200])
            return None
        skill = skill_by_id(skills, out.get("bug_type"))
        fn, old, new = out.get("filename"), out.get("old"), out.get("new")
        problem = None
        if not skill:
            problem = "bug_type must be one of the ids in <bug_types>."
        elif fn not in files_after:
            problem = f"filename must be one of: {', '.join(files_after)}."
        elif not isinstance(old, str) or not isinstance(new, str) or not old or old == new:
            problem = "old and new must be non-empty and different."
        elif _code_only(new) == _code_only(old):
            problem = "new only changes comments or spacing; change the code itself so it gives a wrong result."
        elif abs(new.count("\n") - old.count("\n")) > 2 or len(new) > 2 * len(old) + 80:
            problem = "new must be old with one small change, about the same length; do not add or repeat code."
        elif files_after[fn].count(old) != 1:
            problem = f"old occurs {files_after[fn].count(old)} times in {fn}; copy a snippet that occurs exactly once."
        new_description, description_error = description, ""
        if problem is None and vary_description:
            new_description = str(out.get("description") or "").strip() or description
            description_error = str(out.get("description_error") or "").strip()[:500]
            if not description_error:
                new_description = description  # unchanged by the model's own account
            elif new_description.strip() == description.strip():
                description_error = ""
            elif len(new_description) > 2 * len(description) + 200:
                problem = "description must be the original with one changed claim, about the same length."
        if problem is None:
            buggy = dict(files_after)
            buggy[fn] = files_after[fn].replace(old, new, 1)
            return {"bug_skill": skill, "filename": fn, "old": old, "new": new,
                    "explanation": str(out.get("bug_plan") or "")[:500], "files": buggy,
                    "description": new_description, "description_error": description_error,
                    "description_offered": vary_description}
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": f"That is not valid: {problem} Reply with the corrected JSON."}]
    return None


# ─── Classification ─────────────────────────────────────────────────────────
# The classifier labels each planted bug with one of the quiz's selected bug types (the same list
# the generator chose from), so its label can be compared with the generator's choice.

_CLASSIFIER_SYSTEM = (
    "You audit code written by an AI coding assistant. You get a correct version and a version with "
    "one planted bug. Find the bug in the buggy version and say which ONE of these bug types it is:\n"
    "{types}\n\n"
    "Reply with a JSON object with these keys:\n"
    '  "has_bug": true or false (false if the buggy version behaves the same as the correct one),\n'
    '  "bug_type": one id from the list above, "other" if none fits, or "none" when has_bug is false,\n'
    '  "description": one or two sentences explaining the bug,\n'
    '  "buggy_code": the exact buggy line or fragment ("" when none),\n'
    '  "fix": the corrected line or fragment ("" when none),\n'
    '  "manifestation": how the bug shows up, one of "compile_error", "runtime_error", "wrong_output", '
    '"edge_case_only", "none".'
)

_MANIFESTATIONS = ("compile_error", "runtime_error", "wrong_output", "edge_case_only", "none")


def _normalize(raw: Dict[str, Any], ids: List[str]) -> Dict[str, Any]:
    bug_type = str(raw.get("bug_type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if bug_type not in ids:
        bug_type = "none" if bug_type == "none" or raw.get("has_bug") is False else "other"
    manifestation = str(raw.get("manifestation") or "").strip().lower()
    return {
        "has_bug": bool(raw.get("has_bug")) and bug_type != "none",
        "category": bug_type,
        "description": str(raw.get("description") or "")[:1000],
        "buggy_code": str(raw.get("buggy_code") or "")[:1000],
        "fix": str(raw.get("fix") or "")[:1000],
        "manifestation": manifestation if manifestation in _MANIFESTATIONS else "unknown",
        "model": CLASSIFIER_MODEL,
    }


async def classify_pair(
    client: AsyncOpenAI, *, language: str, filename: str, context: str, correct: str, buggy: str,
    skills: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Label the bug that separates the buggy version from the correct one with a bug type id."""
    types = "\n".join(f"- {s['id']}: {s['name']}. {s.get('description', '')}" for s in skills)
    async with _classify_semaphore:
        resp = await client.chat.completions.create(
            **chat_args(CLASSIFIER_MODEL, temperature=0),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM.format(types=types)},
                {"role": "user", "content": (
                    f"Language: {language}. File: {filename}.\n\n"
                    f"Surrounding code (<CURSOR> marks where the AI's code goes, if shown):\n{context[-5000:]}\n\n"
                    f"Correct version:\n```\n{correct[:6000]}\n```\n\nBuggy version:\n```\n{buggy[:6000]}\n```"
                )},
            ],
        )
    return _normalize(json.loads(resp.choices[0].message.content or "{}"), [s["id"] for s in skills])
