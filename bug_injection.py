"""Bug generation for the study: every AI suggestion and agent change comes as one correct version
and one or more buggy versions (problem.bug_options, default 1), each with exactly one bug of a
different bug type, shown to the student in random order.

The bug types come from the quiz's selected bug skills (bug_skills/*.md, editable per quiz by
the instructor); for each buggy version the model picks a selected type that fits the code. A
separate classifier call (in the background, never seen by the student) then labels each planted
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
from typing import Any, Callable, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from llm_params import DEFAULT_MODEL, chat_args

logger = logging.getLogger(__name__)


# Writes the buggy version of an inline suggestion (from the correct one) and of an agent turn's changes.
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


# ─── Buggy versions of an inline suggestion ─────────────────────────────────
# Each problem sets how many buggy options go with each correct one (problem.bug_options). Each has a
# different bug type, so the student compares different kinds of mistakes (see make_buggy_completions).

MAX_BUG_OPTIONS = 3  # plus the correct option: at most four options (A to D)


def clean_problem_bug_settings(problem: Dict[str, Any], fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A problem's "Correct wrong picks" (bug_feedback), "Show correct answer" (bug_reveal) and
    "Buggy options per suggestion" (bug_options: 1 to MAX_BUG_OPTIONS buggy next to the correct one,
    or 0 for a single option that is randomly the correct or a buggy one). A key the problem lacks
    comes from `fallback`: quizzes saved before these were per problem kept them on the quiz."""
    fallback = fallback or {}
    get = lambda k, d: problem[k] if k in problem else fallback.get(k, d)
    try:
        n = int(get("bug_options", 1))
    except (TypeError, ValueError):
        n = 1
    return {
        "bug_feedback": bool(get("bug_feedback", False)),
        "bug_reveal": bool(get("bug_reveal", False)),
        "bug_options": max(0, min(n, MAX_BUG_OPTIONS)),
    }


# ─── Choosing the bug types ─────────────────────────────────────────────────
# Before any buggy code is written, one call rates how well every selected bug type fits the correct
# code as a mistake a real LLM coding assistant makes there. The types are then drawn at random,
# weighted by that fit and by how recently this student was shown each type (the session's bug
# history), so the most fitting types come up most often without the same one every time. Each
# writer call then gets exactly one of the chosen types.

SELECTOR_MODEL = os.getenv("BUG_SELECTOR_MODEL", BUG_MODEL)
_FIT_WEIGHT = {3: 3.0, 2: 1.0}  # fit 0 and 1 are never chosen
# A type's weight is multiplied by this for each time it is among the session's recent bugs.
_RECENT_PENALTY = float(os.getenv("BUG_RECENT_PENALTY", "0.35"))
RECENT_BUG_WINDOW = 6

_SELECTOR_SYSTEM = """You help run a study of how students judge AI code suggestions. Students are \
shown correct code from an AI coding assistant next to versions with one planted bug. Before the \
bugs are written, rate how well each bug type in <bug_types> fits the correct code in <code>.

Rate each bug type as a mistake that LLM coding assistants (Copilot, ChatGPT, Claude) really make \
in code like this, judged from what is in the code: its operations, conditions, calls, types, edge \
cases, and the task in <problem> if given.
  3: a mistake LLM assistants commonly make in exactly this kind of code; it can be planted in the \
code and an attentive student could find it.
  2: plausible here; an assistant could make it in this code.
  1: possible only in an unnatural way (no assistant would write it here).
  0: does not fit (nothing in the code for it, or the change would be harmless or nonsense).
Judge each type on its own; several types may get the same rating.

Reply with a JSON object (kept short: the student is waiting):
  "fit": {"<bug type id>": 0, 1, 2 or 3, ...} with every bug type in <bug_types>,
  "where": {"<bug type id>": "3 to 6 words naming where in the code it goes", ...} only for the types rated 3 (e.g. "loop bound <= n")."""


def _bug_type_list(skills: List[Dict[str, Any]]) -> str:
    return "\n".join(f"- {s['id']}: {s['name']}. {s.get('description', '')}" for s in skills)


def _draw_bug_types(ratings: Dict[str, Dict[str, Any]], skills: List[Dict[str, Any]],
                    recent: List[str]) -> List[Dict[str, Any]]:
    """Every fitting skill, in an order drawn at random by fit weight times the recency penalty."""
    recent_counts = {t: recent[:RECENT_BUG_WINDOW].count(t) for t in set(recent)}
    pool = [(s, _FIT_WEIGHT.get(ratings.get(s["id"], {}).get("fit", 0), 0.0)
             * _RECENT_PENALTY ** recent_counts.get(s["id"], 0)) for s in skills]
    pool = [(s, w) for s, w in pool if w > 0]
    chosen: List[Dict[str, Any]] = []
    while pool:
        s, _w = random.choices(pool, weights=[w for _, w in pool])[0]
        chosen.append(s)
        pool = [(x, w) for x, w in pool if x is not s]
    return chosen


async def choose_bug_types(
    client: AsyncOpenAI, *, language: str, code: str, problem: str, skills: List[Dict[str, Any]],
    recent: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """{"order": every fitting skill in draw order (use the first ones, the rest are fallbacks),
    "choice": the record for the bug log: every type's rating and why, and the session's recent bug
    types (the caller adds "chosen", the ids it used)}. `code` is the correct code in context;
    `recent` the session's latest bug type ids, newest first."""
    recent = list(recent or [])
    parts = [f"<problem>\n{problem.strip()[:6000]}\n</problem>"] if problem.strip() else []
    parts += [f"Language: {language}.", f"<code>\n{code}\n</code>"]
    resp = await client.chat.completions.create(
        **chat_args(SELECTOR_MODEL, temperature=0, max_tokens=25 * len(skills) + 100),
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _SELECTOR_SYSTEM + "\n\n<bug_types>\n" + _bug_type_list(skills) + "\n</bug_types>"},
                  {"role": "user", "content": "\n\n".join(parts)}],
    )
    try:
        out = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        out = {}
    fits = out.get("fit") if isinstance(out, dict) and isinstance(out.get("fit"), dict) else {}
    where = out.get("where") if isinstance(out, dict) and isinstance(out.get("where"), dict) else {}
    ratings: Dict[str, Dict[str, Any]] = {}
    for type_id, fit in fits.items():
        skill = skill_by_id(skills, type_id)
        try:
            fit = max(0, min(3, int(fit)))
        except (TypeError, ValueError):
            continue
        if skill:
            ratings[skill["id"]] = {"fit": fit, "why": str(where.get(type_id) or "")[:200]}
    return {"order": _draw_bug_types(ratings, skills, recent), "choice": {
        "ratings": [{"bug_type": k, **v} for k, v in ratings.items()], "recent": recent[:RECENT_BUG_WINDOW]}}


def bug_option_count(value: Any, skills: List[Dict[str, Any]]) -> int:
    """The number of buggy options to offer: the quiz's setting (default 1), at most one per
    selected bug type (every buggy option has a different one) and at most MAX_BUG_OPTIONS."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 1
    return max(0, min(n if n >= 1 else 1, MAX_BUG_OPTIONS, len(skills)))


def _bug_items(raw: str, key: str) -> List[Dict[str, Any]]:
    """The model's list of bugs (under `key`), [] when it chose none or the JSON is broken."""
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = out.get(key) if isinstance(out, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


# Each buggy version comes with a short lesson, shown to a student who accepted it (behind
# "Show explanation" on the wrong-pick card). It is written in the background after the options are
# shown (like the classifier's label), not by the call that writes the bug: that call is what the
# student waits for, and the lesson made it ~0.8 s slower per buggy version.
_LESSON_SYSTEM = """A student accepted an AI code suggestion that had a bug in it. You get the correct \
version, the buggy one, the kind of bug and how it was made. Write a short lesson TO the student, like a \
friendly programming teacher: plain words, short sentences, no blame. Quote code in `backticks`. Never \
mention the study, planting, bug type ids, <CURSOR> or other markers. Reply with a JSON object with \
three keys, one or two sentences each:
  "what": what the buggy code does and why that is wrong, quoting the buggy part and the fix,
  "example": a concrete input from the problem (or a small one) and what the buggy code gives \
there versus the correct result,
  "tip": one habit or check that catches this kind of mistake next time (e.g. trace the loop on \
the smallest input, test the edge case n = 0, check the library's documentation)."""

LESSON_KEYS = ("what", "example", "tip")


def _lesson(raw: Any) -> Dict[str, str]:
    """The model's lesson as {"what", "example", "tip"} strings, {} when it gave none."""
    if not isinstance(raw, dict):
        return {}
    lesson = {k: str(raw.get(k) or "").strip()[:500] for k in LESSON_KEYS}
    return lesson if lesson["what"] else {}


async def write_lesson(
    client: AsyncOpenAI, *, language: str, filename: str, context: str, correct: str, buggy: str,
    bug_name: str, bug_plan: str = "", problem: str = "",
) -> Dict[str, str]:
    """The lesson for a student who accepted `buggy` instead of `correct`: {"what", "example", "tip"}."""
    parts = [f"<problem>\n{problem.strip()[:6000]}\n</problem>"] if problem.strip() else []
    parts += [f"Language: {language}. File: {filename}.",
              f"The code around it (<CURSOR> only marks where the suggestion goes):\n{context[-5000:]}",
              f"Correct version:\n```\n{correct[:6000]}\n```", f"Buggy version:\n```\n{buggy[:6000]}\n```",
              f"Kind of bug: {bug_name}." + (f" How it was made: {bug_plan}" if bug_plan else "")]
    resp = await client.chat.completions.create(
        **chat_args(BUG_MODEL, temperature=0.3, max_tokens=600), response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _LESSON_SYSTEM},
                  {"role": "user", "content": "\n\n".join(parts)}],
    )
    try:
        return _lesson(json.loads(resp.choices[0].message.content or "{}"))
    except json.JSONDecodeError:
        return {}


def _count_rule(count: int) -> str:
    if count == 1:
        return "Write 1 buggy version (<count> is 1)."
    return (f"Write {count} buggy versions (<count> is {count}), each with exactly one bug of a DIFFERENT bug type "
            "and each a different change. Give fewer only if fewer bug types fit this code.")


_COMPLETION_BUG_SYSTEM = """You help run a study of how students judge AI code suggestions. A Copilot-style tool \
suggests the code in <suggestion> at <CURSOR> in the student's {language} file. Write buggy suggestions to show \
next to it ({count}).
For each one:
  "bug_type": the id of a bug type from <bug_types>: the kind of mistake an AI coding assistant (Copilot, \
ChatGPT, Claude) would most plausibly make when writing this code for this problem. Read every bug type and \
pick the one(s) that fit this code best, the most plausible first; every buggy suggestion has a different type.
  "buggy": the code an assistant making that mistake would suggest at <CURSOR> instead. It may be a small change \
of <suggestion> or a different way to write it, but it does the same job, has about the same size, looks like a \
normal, confident suggestion (no comments or hints), and is wrong for the problem: on some valid input it gives \
a wrong result, crashes or fails to compile. Never a harmless change (e.g. to a value that is overwritten or \
never used).{others}{note}
Reply with a JSON object {{"bugs": [{{"bug_type": ..., "buggy": ...}}, ...]}}.

<bug_types>
{types}
</bug_types>"""


async def write_buggy_suggestions(
    client: AsyncOpenAI, *, language: str, prefix: str, suffix: str, correct: str, problem: str,
    skills: List[Dict[str, Any]], count: int, others: List[str], clean: Callable[[str], str] = lambda t: t,
    note: str = "",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Up to `count` buggy suggestions, each of a different bug type the model picks from `skills` as
    the most plausible for this code: ([{"buggy", "bug_skill", "bug_plan"}], reasons for the ones
    dropped: "unknown_type", "same_type", "empty", "same_as_correct", "same_as_other"). `others`:
    buggy suggestions already made, which the new ones must differ from; `note`: why the last attempt
    was rejected."""
    other = ("\nThey must differ from these buggy suggestions already shown:\n" + "\n---\n".join(others)) if others else ""
    want = "1 buggy suggestion" if count == 1 else f"{count} buggy suggestions"
    parts = [f"<problem>\n{problem.strip()[:6000]}\n</problem>"] if problem.strip() else []
    parts += [f"File:\n{prefix[-4000:]}<CURSOR>{suffix[:1500]}", f"<suggestion>\n{correct}\n</suggestion>"]
    lines = len(correct.strip("\n").split("\n"))
    resp = await client.chat.completions.create(
        **chat_args(BUG_MODEL, temperature=0.4, max_tokens=(80 + 60 * lines) * count),
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _COMPLETION_BUG_SYSTEM.format(
                      language=language, count=want, others=other, note=note, types=bug_guides(skills))},
                  {"role": "user", "content": "\n\n".join(parts)}],
    )
    bugs: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for item in _bug_items(resp.choices[0].message.content or "{}", "bugs")[:count]:
        skill = skill_by_id(skills, item.get("bug_type"))
        buggy = clean(str(item.get("buggy") or ""))
        made = [b["buggy"] for b in bugs] + others
        if not skill:
            dropped.append("unknown_type")
        elif any(b["bug_skill"]["id"] == skill["id"] for b in bugs):
            dropped.append("same_type")
        elif not buggy.strip():
            dropped.append("empty")
        elif _code_only(buggy) == _code_only(correct):
            dropped.append("same_as_correct")
        elif any(_code_only(buggy) == _code_only(o) for o in made):
            dropped.append("same_as_other")
        else:
            bugs.append({"buggy": buggy, "bug_skill": skill, "bug_plan": ""})
    return bugs, dropped


# Calls make_buggy_completions may make for one suggestion (the first and the retries).
BUG_ATTEMPTS = int(os.getenv("BUG_ATTEMPTS", "3"))
_RETRY_NOTE = ("). Every buggy suggestion must be non-empty code that differs from <suggestion> in what it does, "
               "not only in spacing or names, and that is wrong for the problem. If the most plausible types do "
               "not fit, use another type from <bug_types>.")


async def make_buggy_completions(
    client: AsyncOpenAI, *, skills: List[Dict[str, Any]], count: int = 1,
    recent: Optional[List[str]] = None, **kwargs: Any,
) -> Dict[str, Any]:
    """Up to `count` buggy versions of a correct inline suggestion, each with a different bug type.
    One call reads every selected bug type and picks the ones an AI assistant would most plausibly
    get wrong in this code, writing a buggy suggestion for each. Versions that come back empty, the
    same as the correct one or as each other are asked for again (other types, different code, told
    why), up to BUG_ATTEMPTS calls in all: the student is shown all `count` or none.
    {"bugs": [...], "choice": ...}, or {"no_bug": reason, "choice": ...}; "choice" records what was
    dropped and the session's recent types (for the log only: they do not steer the choice)."""
    count = max(1, min(count, len(skills)))
    args = {k: kwargs[k] for k in ("language", "prefix", "suffix", "correct")}
    args.update(problem=kwargs.get("problem", ""), clean=kwargs.get("clean", lambda t: t))
    bugs: List[Dict[str, Any]] = []
    dropped: List[str] = []
    attempts = 0
    note = ""
    for attempts in range(1, BUG_ATTEMPTS + 1):
        left = [s for s in skills if all(s["id"] != b["bug_skill"]["id"] for b in bugs)]
        if not left:
            break
        try:
            new, why = await write_buggy_suggestions(
                client, skills=left, count=count - len(bugs), others=[b["buggy"] for b in bugs], note=note, **args)
        except Exception as e:
            logger.warning("Buggy suggestion failed: %s", e)
            new, why = [], ["error"]
        bugs += new
        why = why or ([] if new else ["none_fits"])
        dropped += why
        if len(bugs) >= count:
            break
        note = "\nYour last answer was rejected (" + ", ".join(sorted(set(why))) + _RETRY_NOTE
    choice = {"method": "model_picks", "dropped": dropped, "attempts": attempts,
              "recent": list(recent or [])[:RECENT_BUG_WINDOW], "chosen": [b["bug_skill"]["id"] for b in bugs]}
    return {"bugs": bugs, "choice": choice} if bugs else {"no_bug": dropped[-1] if dropped else "none_fits", "choice": choice}


# ─── Buggy versions of an agent turn ────────────────────────────────────────

_AGENT_BUG_SYSTEM = """You help run a study of how students judge AI-written code. An AI coding agent \
just made the correct changes below. Make buggy versions of those changes (as many as <count> asks), \
each containing exactly one bug, so the student can be shown all of them next to the correct one.

1. For each buggy version pick ONE bug type from <bug_types> that fits the changed code, following \
the rules on how to choose the bug below. Every buggy version uses a different bug type. If no bug \
type fits, return an empty list.
2. Plant each bug by replacing one exact snippet of the changed code with a buggy version. The \
snippet must lie inside the lines the agent added or changed (the "+" lines of the diff). Each \
buggy version is made from the correct changes on its own (bugs are never combined), and no two \
buggy versions may make the same change.

Rules: exactly one bug of the chosen type per version; everything else unchanged; the bug must make \
the code actually wrong (wrong result, crash, or failure to compile), not just slower or longer; the \
buggy code must look like a normal, correct change at a glance; no comments, hints or markers about the bug.

Reply with a JSON object {"versions": [...]}, one object per buggy version, keys in this order:
  "bug_type": the id of the chosen bug type,
  "bug_plan": one sentence naming the exact change you will make, why it is that bug type, and the \
input or situation that exposes it,
  "filename": the file you change,
  "old": a short exact snippet (1 to 3 whole lines, never the whole file) copied from that \
file's new content; it must occur exactly once,
  "new": the replacement containing the bug."""

# Added when this turn may also get a wrong description (DESCRIPTION_ERROR_RATE).
_AGENT_DESCRIPTION_RULES = """The student also sees the agent's description of its changes (in <description>). \
In this turn each buggy version's description also contains exactly one wrong claim, besides the code bug:
- Make it the way a real AI agent misdescribes its own work: for example claiming an edge case or \
constraint is handled when it is not, claiming it tested an input it did not, a wrong explanation of \
the cause of the problem it fixed, or a wrong complexity. Prefer a claim that covers up or misexplains \
that version's bug, e.g. "negative numbers are handled" when the bug drops them.
- The wrong claim is short, confident and subtle: a careful student could check it against the code \
or by running it. Never an obvious contradiction, a hint that something is wrong, or a typo.
- Change or add only that one claim; keep everything else in the description word for word.
- Only if the description is too short to carry any claim, keep it unchanged.
Add two keys to each version:
  "description": that buggy version's full description (same tone, length and Markdown),
  "description_error": one sentence naming the wrong claim, or "" if the description is unchanged."""

# Share of agent turns whose buggy versions also get a wrong description (the rest keep the agent's
# own description, so most buggy versions differ only in the code).
DESCRIPTION_ERROR_RATE = float(os.getenv("DESCRIPTION_ERROR_RATE", "0.5"))


_COMMENT_RE = re.compile(r"\s*(#|//).*$", re.M)


def _code_only(snippet: str) -> str:
    """The snippet without line comments and whitespace (approximate: ignores comment markers in strings)."""
    return re.sub(r"\s+", "", _COMMENT_RE.sub("", snippet))


async def make_buggy_agent_versions(
    client: AsyncOpenAI,
    *,
    language: str,
    message: str,
    diffs: Dict[str, str],
    files_after: Dict[str, str],
    skills: List[Dict[str, Any]],
    count: int = 1,
    problem: str = "",
    description: str = "",
    recent: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Up to `count` buggy copies of the agent's final files, each with one bug of a different bug
    type ([] when no valid bug could be planted). choose_bug_types picks the types first (`recent`:
    the session's latest bug type ids, newest first), and every result's "choice" is that record.
    `problem` is the task the student is solving (statement, test cases, constraints).
    `description` is the agent's summary of its changes; each result's "description" is that buggy
    version's one, which on some turns (DESCRIPTION_ERROR_RATE) contains a wrong claim too."""
    if not skills or not files_after:
        return []
    count = max(1, min(count, len(skills)))
    code = ("The agent's changes (unified diffs; rate the bug types for the added or changed lines):\n"
            + "\n".join(diffs.values())[:12000])
    pick = await choose_bug_types(client, language=language, code=code, problem=problem,
                                  skills=skills, recent=recent)
    choice = pick["choice"]
    skills = pick["order"][:count]
    if not skills:
        logger.info("No bug type fits this agent turn")
        return []
    count = len(skills)
    vary_description = bool(description.strip()) and random.random() < DESCRIPTION_ERROR_RATE
    parts = [f"<problem>\n{problem.strip()[:6000]}\n</problem>"] if problem.strip() else []
    parts += [f"Language: {language}.", f"Student's request:\n{message[:3000]}",
             "The agent's changes (unified diffs):\n" + "\n".join(diffs.values())[:12000]]
    for name, content in files_after.items():
        parts.append(f"New content of {name}:\n```\n{content[:8000]}\n```")
    system = _AGENT_BUG_SYSTEM
    hints = {r["bug_type"]: r["why"] for r in choice["ratings"]}
    parts.append("Where each bug type fits (from an earlier look at the code):\n"
                 + "\n".join(f"- {s['id']}: {hints.get(s['id'], '')}" for s in skills))
    if vary_description:
        parts.append(f"<description>\n{description.strip()[:4000]}\n</description>")
        system += "\n\n" + _AGENT_DESCRIPTION_RULES
    parts.append(f"<count>{count}</count>\n{_count_rule(count)}")
    messages = [
        {"role": "system", "content": system + "\n\n" + BUG_CHOICE_RULES + "\n\n<bug_types>\n" + bug_guides(skills) + "\n</bug_types>"},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    versions: List[Dict[str, Any]] = []
    for _attempt in range(2):
        resp = await client.chat.completions.create(
            **chat_args(BUG_MODEL, temperature=0.4), response_format={"type": "json_object"}, messages=messages,
        )
        raw = resp.choices[0].message.content or "{}"
        items = _bug_items(raw, "versions")
        if not items:
            if not versions:
                logger.info("No bug type fits this agent turn")
            break
        problems = []
        for item in items[:count - len(versions)]:
            skill = skill_by_id(skills, item.get("bug_type"))
            fn, old, new = item.get("filename"), item.get("old"), item.get("new")
            problem = None
            if not skill:
                problem = "bug_type must be one of the ids in <bug_types>."
            elif any(v["bug_skill"]["id"] == skill["id"] for v in versions):
                problem = "every buggy version must use a different bug type."
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
            elif any(v["filename"] == fn and _code_only(v["old"]) == _code_only(old)
                     and _code_only(v["new"]) == _code_only(new) for v in versions):
                problem = "this is the same change as another buggy version; make a different change."
            new_description, description_error = description, ""
            if problem is None and vary_description:
                new_description = str(item.get("description") or "").strip() or description
                description_error = str(item.get("description_error") or "").strip()[:500]
                if not description_error:
                    new_description = description  # unchanged by the model's own account
                elif new_description.strip() == description.strip():
                    description_error = ""
                elif len(new_description) > 2 * len(description) + 200:
                    problem = "description must be the original with one changed claim, about the same length."
            if problem is None:
                buggy = dict(files_after)
                buggy[fn] = files_after[fn].replace(old, new, 1)
                versions.append({"bug_skill": skill, "filename": fn, "old": old, "new": new,
                                 "explanation": str(item.get("bug_plan") or "")[:500], "files": buggy,
                                 "description": new_description, "description_error": description_error,
                                 "description_offered": vary_description})
            else:
                problems.append(f"- {item.get('bug_type')}: {problem}")
        if len(versions) >= count or not problems:
            break
        used = ", ".join(v["bug_skill"]["id"] for v in versions) or "none yet"
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Some buggy versions are not valid:\n" + "\n".join(problems)
                      + f"\nReply with {{\"versions\": [...]}} holding only {count - len(versions)} new buggy "
                      f"version(s) to replace them, with bug types other than the valid ones ({used})."}]
    choice["chosen"] = [v["bug_skill"]["id"] for v in versions]
    for v in versions:
        v["choice"] = choice
    return versions


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
    '  "description": one or two sentences explaining the bug to a student, in plain words (never mention '
    '<CURSOR> or other markers: they only show where the code goes),\n'
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
