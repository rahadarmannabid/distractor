"""Study log tables, kept per quiz and downloaded per condition from the instructor panel.

The browser keeps the rows of the current step (templates/study_log.js) and sends them when the
student moves to another step; survey answers are written here when a survey is submitted. Rows are
stored at study_logs/{quiz}/{table}/{session}/{row_id} in Firebase (row ids come from the browser, so
sending a row again overwrites it) plus a local JSONL backup.

Tables:
  autocomplete  one row per option of an inline suggestion (conditions 2 and 4)
  agent         one row per code version of an agent response, or per response without code (condition 3)
  compilation   one row per run or compile, by the student or the agent (every condition)
  surveys       one row per survey answer (every condition)

What the browser cannot know (which option is correct or buggy, the bug type: hidden from students
on purpose) is joined at export from the suggestion records at bug_injections/{session}/{pair_id}.
"""

import csv
import difflib
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONDITION_NAMES = {1: "no_ai", 2: "autocomplete_single_line", 3: "coding_agent", 4: "autocomplete_multi_line"}
CLIENT_TABLES = ("autocomplete", "agent", "compilation")
TABLES = CLIENT_TABLES + ("surveys", "submissions")
# The interaction table each condition gets (besides compilation and surveys): for autocomplete, the
# suggestion-level table built from the option rows (build_suggestions).
CONDITION_TABLE = {2: "suggestions", 3: "responses", 4: "suggestions"}
# Tables built from others (the condition's log), kept out of TABLES (the stored ones).
DERIVED_TABLES = ("suggestions", "responses")

_COMMON = ["participant_id", "session_id", "assignment_id", "condition_name", "timestamp", "problem_id",
           "problem_title", "language", "interaction_index"]
COLUMNS: Dict[str, List[str]] = {
    "autocomplete": _COMMON + [
        "suggestion_set_id", "suggestion_id", "option_label", "options_in_set", "suggestion_code",
        "suggestion_lines", "suggestion_correctness", "bug_types", "bug_type_method", "classifier_bug_type", "explanation",
        "generation_latency_ms", "times_shown", "previewed", "preview_count", "view_time_ms",
        "suggestion_status", "accept_method", "time_to_accept_ms", "time_to_decision_ms", "feedback_shown",
        "explanation_clicked", "corrected_code", "code_in_final", "bug_in_final_code", "next_compilation_id",
        "editor_snapshot", "suggestion_position", "file"],
    "agent": _COMMON + [
        "chat_id", "turn_index", "agent_mode", "response_id", "user_prompt", "agent_response",
        "description_correctness", "description_error", "tool_calls", "files_changed", "suggestion_id",
        "option_label", "options_in_set", "suggestion_code", "suggestion_correctness", "bug_types",
        "bug_type_method", "classifier_bug_type", "selected_code_block", "previewed", "preview_count",
        "suggestion_status", "accept_method", "time_to_accept_ms", "time_to_decision_ms", "feedback_shown",
        "explanation_clicked", "corrected_code", "reverted", "bug_in_final_code", "next_compilation_id",
        "response_time_ms", "time_to_first_token_ms"],
    "suggestions": ["participant_id", "timestamp", "condition_name", "problem_id", "problem_title", "interaction_index",
                    "suggestion_position", "correct_suggestion_code", "bug_types", "buggy_code_suggestions",
                    "time_to_accept_ms", "time_to_generate_ms", "suggestion_status", "accepted_code_suggestion",
                    "accepted_suggestion_correct", "explanation", "explanation_clicked", "code_snapshot", "code_run",
                    "code_edit_before_next", "code_edit_detail", "status_detail", "accept_method", "bug_type_accepted", "feedback_shown", "options_in_set",
                    "code_run_id", "suggestion_set_id", "session_id"],
    "responses": ["participant_id", "timestamp", "condition_name", "problem_id", "problem_title", "interaction_index",
                  "user_prompt", "agent_correct_response", "agent_correct_code", "agent_buggy_response_type",
                  "agent_buggy_response", "agent_buggy_code", "user_selected_status", "user_selected_option",
                  "is_option_correct", "explanation", "explanation_clicked", "time_to_generate_ms", "time_to_decide_ms",
                  "code_snapshot", "code_run",
                  "code_edit_before_next", "code_edit_detail", "status_detail", "agent_mode", "description_error",
                  "feedback_shown", "options_in_set", "code_run_id", "response_id", "session_id"],
    "compilation": ["participant_id", "session_id", "assignment_id", "condition_name", "problem_id",
                    "problem_title", "suggestion_id", "code_version_id", "compilation_attempt_id", "timestamp",
                    "run_by", "run_trigger", "language", "entry_file", "compile_only", "compilation_status",
                    "compilation_error", "compilation_time_ms", "run_status", "exit_code", "runtime_error",
                    "output_excerpt", "run_time_ms", "total_time_ms", "stdin_lines", "input_wait_count"],
    "submissions": ["participant_id", "session_id", "assignment_id", "condition_name", "problem_id", "problem_title",
                    "submission_index", "timestamp", "final_submit", "language", "file_count", "files", "code_lines",
                    "submitted_code"],
    "surveys": ["participant_id", "session_id", "assignment_id", "condition_name", "problem_id", "problem_title",
                "survey_type", "survey_round", "question_index", "question_id", "question_text", "question_type",
                "answer", "completion_time_seconds", "timestamp"],
}

_ROW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MAX_TEXT = 20000
_MAX_ROWS = 500


def _key(s: Any) -> str:
    t = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(s or ""))
    return (t or "none")[:96]


def quiz_key(quiz_id: Optional[str], assignment_key: Optional[str] = None) -> str:
    return _key(quiz_id or f"assignment_{assignment_key or 'none'}")


def clean_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """The browser's rows that are well formed: a known table, a valid row_id, plain values (long text cut)."""
    out = []
    for row in list(rows)[:_MAX_ROWS]:
        if not isinstance(row, dict) or row.get("table") not in CLIENT_TABLES:
            continue
        if not _ROW_ID_RE.match(str(row.get("row_id") or "")):
            continue
        clean: Dict[str, Any] = {}
        for k, v in row.items():
            k = _key(k)[:64]
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            if isinstance(v, str):
                v = v[:_MAX_TEXT]
            elif not isinstance(v, (int, float, bool)) and v is not None:
                v = str(v)[:_MAX_TEXT]
            clean[k] = v
        out.append(clean)
    return out


def _backup(lines: List[Dict[str, Any]]) -> None:
    try:
        path = Path(os.getenv("SUBMISSIONS_DIR", "session_logs")) / "study_logs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("Study log backup failed: %s", e)


def write_rows(root: Any, qkey: str, session_id: str, stamp: Dict[str, Any], rows: List[Dict[str, Any]]) -> int:
    """Stores rows (each with "table" and "row_id") under the quiz, stamped with the session's
    participant, condition and assignment (from the server, never from the browser)."""
    if not rows:
        return 0
    stamped = [{**r, **stamp, "session_id": session_id, "stored_at": datetime.now().isoformat()} for r in rows]
    _backup([{"quiz": qkey, **r} for r in stamped])
    if root is None:
        return len(stamped)
    updates: Dict[str, Any] = {}
    for r in stamped:
        updates[f"{r['table']}/{_key(session_id)}/{r['row_id']}"] = r
    root.child("study_logs").child(qkey).update(updates)
    return len(stamped)


def survey_rows(survey_type: str, responses: List[Tuple[str, str, str, Any]], *, problem_index: Optional[int],
                survey_round: Any = "", timestamp: str = "", completion_time_seconds: Any = None) -> List[Dict[str, Any]]:
    """Rows of the surveys table for one submitted survey. responses: (question_id, text, type, answer).
    Row ids depend only on the survey and question, so a survey stored twice is not counted twice."""
    out = []
    for i, (qid, text, qtype, answer) in enumerate(responses):
        if isinstance(answer, (dict, list)):
            answer = json.dumps(answer, ensure_ascii=False)
        out.append({
            "table": "surveys", "row_id": _key(f"{survey_type}_{problem_index}_{survey_round}_{i}")[:80],
            "problem_index": problem_index, "survey_type": survey_type, "survey_round": survey_round,
            "question_index": i + 1, "question_id": qid, "question_text": text, "question_type": qtype,
            "answer": answer, "completion_time_seconds": completion_time_seconds,
            "timestamp": timestamp or datetime.now().isoformat(),
        })
    return out


# ─── Export ──────────────────────────────────────────────────────────────────

def _option(record: Dict[str, Any], oid: str) -> Tuple[str, Dict[str, Any]]:
    """("correct" | "buggy" | "", the buggy option's entry) for an option id in a suggestion record."""
    options = record.get("options") or []
    if isinstance(options, dict):
        options = list(options.values())
    variant = next((str(o.get("variant") or "") for o in options if isinstance(o, dict) and o.get("oid") == oid), "")
    bugs = record.get("bugs") or []
    if isinstance(bugs, dict):
        bugs = list(bugs.values())
    bug = next((b for b in bugs if isinstance(b, dict) and b.get("oid") == oid), {})
    if not bug and variant == "buggy" and record.get("requested_bug"):
        # Older records keep their one bug at the top level.
        bug = {k: record.get(k) for k in ("requested_bug", "classification", "description_error", "lesson") if k in record}
    return variant, bug


def _explanation(bug: Dict[str, Any]) -> str:
    """What the wrong-pick card explains for a buggy option: its lesson (what is wrong, an example, a
    tip), or the classifier's description of the bug when there is no lesson."""
    lesson = bug.get("lesson") if isinstance(bug.get("lesson"), dict) else {}
    parts = [f"{label}: {str(lesson.get(k)).strip()}" for k, label in (("what", "What"), ("example", "Example"), ("tip", "Tip"))
             if str(lesson.get(k) or "").strip()]
    if parts:
        return "\n".join(parts)
    return str((bug.get("classification") or {}).get("description") or "").strip()


def _join_suggestion(row: Dict[str, Any], record: Optional[Dict[str, Any]], oid: str) -> None:
    """Fills correctness, bug type, how it was chosen and the classifier's label from the record."""
    variant, bug = _option(record or {}, oid) if oid else ("", {})
    if not variant and record is None and str(row.get("options_in_set") or "") == "1":
        variant = "correct"  # no buggy option was made (no bug types selected): only the correct one
    row["suggestion_correctness"] = variant
    if variant == "buggy":
        requested = bug.get("requested_bug") or {}
        row["bug_types"] = requested.get("id") or requested.get("name") or ""
        row["classifier_bug_type"] = (bug.get("classification") or {}).get("category", "")
        row["explanation"] = _explanation(bug)
        choice = (record or {}).get("bug_choice") or {}
        if choice:
            row["bug_type_method"] = f"{choice.get('method', '')}" + (
                f", {choice['attempts']} attempt(s)" if choice.get("attempts") else "")
        if "description_error" in bug:
            row["description_error"] = bug.get("description_error") or ""
            row["description_correctness"] = "wrong_claim" if bug.get("description_error") else "correct"
    elif variant == "correct":
        row["bug_types"] = ""
        if row.get("table") == "agent":
            row["description_correctness"] = "correct"
    accepted = row.get("suggestion_status") == "accepted"
    if variant == "buggy" and accepted:
        if row.get("feedback_shown") == "corrected":
            row["bug_in_final_code"] = "No"  # the correct code replaced it
        elif row.get("table") == "agent" and row.get("reverted") in ("Yes", "No"):
            row["bug_in_final_code"] = "No" if row["reverted"] == "Yes" else "Yes"
        elif row.get("code_in_final") in ("Yes", "No"):
            row["bug_in_final_code"] = row["code_in_final"]
        else:
            row["bug_in_final_code"] = ""  # not known (rebuilt from the server's records)
    elif accepted:
        row["bug_in_final_code"] = "No"


def _problem(row: Dict[str, Any], labels: List[str]) -> None:
    try:
        i = int(row.get("problem_index"))
    except (TypeError, ValueError):
        return
    row["problem_id"] = f"P{i + 1}"
    row["problem_title"] = labels[i] if 0 <= i < len(labels) else ""


def _list(v: Any) -> List[Any]:
    """Firebase lists come back as lists or as {"0": ..., "1": ...} / {push_id: ...} dicts."""
    if isinstance(v, list):
        return [x for x in v if x is not None]
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda k: (len(str(k)), str(k)))]
    return []


def _ms_between(a: Any, b: Any) -> Any:
    ta, tb = _ts(a), _ts(b)
    return int(round((tb - ta) * 1000)) if ta is not None and tb is not None else ""


def _record_rows(sid: str, pair_id: str, rec: Dict[str, Any], skip: set) -> List[Dict[str, Any]]:
    """Rows rebuilt from a suggestion record (bug_injections) for options the browser did not log:
    one per option, with its code, what happened to it (from the record's events) and the bug."""
    table = "agent" if rec.get("kind") == "agent" else "autocomplete"
    options = [o for o in _list(rec.get("options")) if isinstance(o, dict) and o.get("oid")]
    events = [e for e in _list(rec.get("events")) if isinstance(e, dict)]
    by_event: Dict[str, Dict[str, str]] = {}
    for e in sorted(events, key=lambda e: str(e.get("at") or "")):
        by_event.setdefault(str(e.get("event")), {}).setdefault(str(e.get("oid")), str(e.get("at") or ""))
    accepted = by_event.get("accepted", {})
    shown = by_event.get("shown", {})
    first_shown = min(shown.values()) if shown else ""
    bugs = [b for b in _list(rec.get("bugs")) if isinstance(b, dict)]
    out = []
    for i, o in enumerate(options):
        oid = str(o["oid"])
        if (pair_id, oid) in skip:
            continue
        variant = str(o.get("variant") or "")
        bug = next((b for b in bugs if b.get("oid") == oid), None)
        if bug is None and variant == "buggy":  # older records keep the one bug at the top level
            bug = {k: rec.get(k) for k in ("buggy", "requested_bug", "classification", "description_error",
                                            "description_buggy")}
        bug = bug or {}
        if oid in accepted:
            status = "accepted"
        elif accepted:
            status = "not_chosen"
        elif oid in by_event.get("rejected", {}):
            status = "rejected"
        elif shown:
            status = "not_accepted"  # shown, then ignored or dismissed (the record cannot tell which)
        else:
            status = "not_shown"     # written ahead or held back, never on screen
        code = bug.get("buggy") if variant == "buggy" else rec.get("correct")
        row: Dict[str, Any] = {
            "table": table, "row_id": f"{'ag' if table == 'agent' else 'ac'}_{pair_id}_{oid}", "source": "server_record",
            "participant_id": rec.get("user_id") or "", "session_id": sid, "condition": rec.get("condition"),
            "problem_index": rec.get("problem_index"), "timestamp": first_shown or rec.get("created_at") or "",
            "language": rec.get("language") or "", "suggestion_id": oid, "option_label": "ABCD"[i] if i < 4 else str(i + 1),
            "options_in_set": len(options), "suggestion_code": code or "", "suggestion_status": status,
            "time_to_accept_ms": _ms_between(first_shown or rec.get("created_at"), accepted[oid]) if oid in accepted else "",
            "previewed": "Yes" if oid in shown else "No",
            "feedback_shown": ("corrected" if by_event.get("corrected") else "revealed" if by_event.get("revealed") else "")
                              if status == "accepted" and variant == "buggy" else "",
            "suggestion_correctness": variant,
        }
        if table == "autocomplete":
            prefix = str(rec.get("prefix") or "")
            head = prefix.rsplit("\n", 1)[-1]
            row.update(suggestion_set_id=pair_id, editor_snapshot=(prefix + str(rec.get("suffix") or ""))[:20000],
                       cursor_offset=len(prefix), snapshot_cut="Yes" if len(prefix) >= 4000 or len(str(rec.get("suffix") or "")) >= 1500 else "",
                       file=rec.get("filename") or "", generation_latency_ms=rec.get("generation_ms", ""),
                       # The record keeps the last 4000 characters before the cursor: exact for files shorter than that.
                       suggestion_position=f"{'' if len(prefix) < 4000 else '~'}Line {prefix.count(chr(10)) + 1}, Col {len(head) + 1}")
        else:
            vfiles = rec.get("version_files") if isinstance(rec.get("version_files"), dict) else {}
            ws = _list(rec.get("workspace_snapshot"))
            decided = accepted.get(oid) or by_event.get("rejected", {}).get(oid)
            applied_oid = oid
            if status == "accepted" and variant == "buggy" and by_event.get("corrected"):
                applied_oid = next((str(o2["oid"]) for o2 in options if o2.get("variant") == "correct"), oid)
            row.update(response_time_ms=rec.get("generation_ms", ""),
                       time_to_decision_ms=_ms_between(first_shown or rec.get("created_at"), decided) if decided else "",
                       code_snapshot=_files_text(ws), workspace_files=json.dumps(ws) if ws else "",
                       applied_files=json.dumps(_list(vfiles.get(applied_oid))) if status == "accepted" and vfiles.get(applied_oid) else "")
            row.update(response_id=pair_id, agent_mode=rec.get("mode") or "", user_prompt=rec.get("message") or "",
                       agent_response=(bug.get("description_buggy") if variant == "buggy" else rec.get("description_correct")) or "",
                       files_changed=", ".join(_list(rec.get("files_changed"))) if rec.get("files_changed") else rec.get("filename") or "")
        _join_suggestion(row, rec, oid)
        out.append(row)
    return out


def _payload_rows(payload: Dict[str, Any], want_compilation: bool, want_surveys: bool) -> Dict[str, List[Dict[str, Any]]]:
    """Compilation rows (activity log "code_execution") and survey rows (pre/post/periodic) from a
    stored session, for sessions the browser log does not cover."""
    meta = payload.get("session_metadata") if isinstance(payload.get("session_metadata"), dict) else {}
    sid, uid, cond = meta.get("session_id") or "", meta.get("user_id") or "", meta.get("condition")
    base = {"source": "server_record", "participant_id": uid, "session_id": sid, "condition": cond}
    out: Dict[str, List[Dict[str, Any]]] = {"compilation": [], "surveys": [], "explanations": [], "submissions": []}
    # The code submitted for each problem (on moving to the next problem, and at the final submit).
    subs = [x for x in _list(meta.get("per_problem_submissions")) if isinstance(x, dict)]
    if not subs and str(meta.get("submitted_code") or "").strip():
        subs = [{"problem_index": None, "code": meta.get("submitted_code"), "files": meta.get("submitted_files"),
                 "timestamp": meta.get("end_time"), "final_submit": True}]
    for n, x in enumerate(subs):
        files = [f for f in _list(x.get("files")) if isinstance(f, dict) and f.get("filename")]
        code = _files_text(files) if files else str(x.get("code") or "")
        out["submissions"].append({**base, "table": "submissions", "row_id": f"cs_{n}",
            "problem_index": _int(x.get("problem_index")), "timestamp": x.get("timestamp") or "",
            "submission_index": n + 1, "final_submit": "Yes" if str(x.get("final_submit")).lower() == "true" else "No",
            "language": meta.get("programming_language") or "", "file_count": len(files) or 1,
            "files": ", ".join(str(f.get("filename")) for f in files), "code_lines": len(code.splitlines()),
            "submitted_code": code[:20000]})
    al = payload.get("activity_log")
    entries = al if isinstance(al, list) else _list((al or {}).get("entries")) if isinstance(al, dict) else []
    for e in entries:
        if isinstance(e, dict) and (e.get("activity_type") or e.get("type")) == "wrong_pick_explanation_opened":
            pid = (e.get("activity_details") or {}).get("pair_id")
            if pid:
                out["explanations"].append({"session_id": sid, "pair_id": str(pid)})
    if want_compilation:
        for n, e in enumerate(entries):
            if not isinstance(e, dict) or (e.get("activity_type") or e.get("type")) != "code_execution":
                continue
            d = e.get("activity_details") or {}
            ctx = e.get("session_context") or {}
            result = str(d.get("execution_result") or "")
            err = str(d.get("error_message") or "")
            compile_error = result == "compile_error"
            out["compilation"].append({**base, "table": "compilation", "row_id": f"cx_{n}",
                "problem_index": ctx.get("problem_index"), "timestamp": d.get("start_time") or e.get("timestamp") or "",
                "compilation_attempt_id": f"{sid}_run{d.get('execution_number') or n}", "run_by": "student",
                "run_trigger": d.get("execution_trigger") or "", "language": meta.get("programming_language") or "",
                "entry_file": d.get("entry_file") or "", "compile_only": "Yes" if d.get("compile_only") else "No",
                "compilation_status": "failure" if compile_error else "",
                "compilation_error": err if compile_error else "",
                "run_status": "not_run" if compile_error else ("success" if result == "success" else
                              ("stopped" if d.get("stop_reason") == "stopped" else f"stopped_{d['stop_reason']}"
                               if d.get("stop_reason") else result or "")),
                "exit_code": d.get("exit_code"), "runtime_error": err if not compile_error and result != "success" else "",
                "output_excerpt": d.get("output_summary") or "", "total_time_ms": d.get("duration"),
                "stdin_lines": d.get("stdin_lines")})
    if want_surveys:
        # Every pre/post survey per problem (sessions since survey_history was kept); the latest
        # pre_survey/post_survey blocks only for the types the history does not have.
        history = [h for h in _list(meta.get("survey_history") or payload.get("survey_history")) if isinstance(h, dict)]
        for h in history:
            kind, pi = str(h.get("survey_type") or ""), _int(h.get("problem_index"))
            for i, r in enumerate(_list(h.get("responses"))):
                if isinstance(r, dict):
                    ans = r.get("answer")
                    out["surveys"].append({**base, "table": "surveys", "row_id": f"sv_{kind}_p{pi}_{i}", "survey_type": kind,
                        "problem_index": pi, "question_index": i + 1, "question_id": r.get("question_id") or "",
                        "question_text": r.get("question_text") or "", "question_type": r.get("question_type") or "",
                        "answer": json.dumps(ans, ensure_ascii=False) if isinstance(ans, (list, dict)) else ans,
                        "completion_time_seconds": h.get("completion_time_seconds"), "timestamp": h.get("timestamp") or ""})
        in_history = {str(h.get("survey_type")) for h in history}
        for kind in ("pre", "post"):
            block = payload.get(f"{kind}_survey")
            if not isinstance(block, dict) or kind in in_history:
                continue
            for i, r in enumerate(_list(block.get("responses"))):
                if isinstance(r, dict):
                    out["surveys"].append({**base, "table": "surveys", "row_id": f"sv_{kind}_{i}", "survey_type": kind,
                        "question_index": i + 1, "question_id": r.get("question_id") or "",
                        "question_text": r.get("question_text") or "", "question_type": r.get("question_type") or "",
                        "answer": json.dumps(r.get("answer"), ensure_ascii=False) if isinstance(r.get("answer"), (list, dict)) else r.get("answer"),
                        "completion_time_seconds": block.get("completion_time_seconds"), "timestamp": block.get("timestamp") or ""})
        ps = payload.get("periodic_surveys")
        occs = _list(ps.get("occurrences")) if isinstance(ps, dict) else []
        for occ in occs:
            if not isinstance(occ, dict):
                continue
            num = occ.get("occurrence_number") or occ.get("occurrence") or occ.get("analysis_count") or ""
            for i, r in enumerate(_list(occ.get("responses"))):
                if isinstance(r, dict):
                    out["surveys"].append({**base, "table": "surveys", "row_id": f"sv_periodic_{num}_{i}",
                        "survey_type": occ.get("survey_type") or "periodic", "survey_round": num, "question_index": i + 1,
                        "question_id": f"q{i + 1}", "question_text": r.get("question") or r.get("question_text") or "",
                        "question_type": "likert" if isinstance(r.get("response"), (int, float)) else "text",
                        "answer": r.get("response", r.get("answer")), "timestamp": occ.get("timestamp") or ""})
    return out


def collect_all(root: Any, quiz_id: str, labels: List[str], sessions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Every log row of the quiz, by table: what the browser logged (joined with the suggestion
    records), plus what can be rebuilt from the server's own records for anything it did not log
    (sessions before the browser log existed, options never shown): suggestion records for the
    autocomplete and agent tables, stored sessions (`sessions`) for compilation and surveys."""
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in TABLES}
    allrecs = root.child("bug_injections").get() or {}
    records: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for sid, recs in (allrecs.items() if isinstance(allrecs, dict) else []):
        for pid, rec in (recs.items() if isinstance(recs, dict) else []):
            if isinstance(rec, dict) and str(rec.get("quiz_id") or "") == quiz_id:
                records.setdefault(str(rec.get("session_id") or sid), {})[str(pid)] = rec
    logged = root.child("study_logs").child(quiz_key(quiz_id)).get() or {}
    covered: Dict[str, set] = {"pairs": set(), "compilation": set(), "surveys": set()}
    for table, by_session in (logged.items() if isinstance(logged, dict) else []):
        if table not in TABLES or not isinstance(by_session, dict):
            continue
        for by_id in by_session.values():
            for r in (by_id.values() if isinstance(by_id, dict) else []):
                if not isinstance(r, dict):
                    continue
                r = {**r, "source": "browser_log"}
                sid = str(r.get("session_id") or "")
                if table == "autocomplete":
                    pair, oid = str(r.get("suggestion_set_id") or ""), str(r.get("suggestion_id") or "")
                    covered["pairs"].add((pair, oid))
                    _join_suggestion(r, records.get(sid, {}).get(pair), oid)
                elif table == "agent":
                    pair, oid = str(r.get("response_id") or ""), str(r.get("suggestion_id") or "")
                    covered["pairs"].add((pair, oid))
                    if oid:
                        _join_suggestion(r, records.get(sid, {}).get(pair), oid)
                elif table == "compilation":
                    covered["compilation"].add(sid)
                elif table == "surveys":
                    # Per question: a session may have some surveys logged and others only stored
                    # (e.g. a pre-survey answered before the log existed). Also per problem, so one
                    # problem's logged post-survey does not hide another problem's stored one.
                    key = (sid, str(r.get("survey_type")), _squash(str(r.get("question_text") or "")))
                    covered["surveys"].add(key)
                    covered["surveys"].add(key + (_int(r.get("problem_index")),))
                out[table].append(r)
    for sid, recs in records.items():
        for pid, rec in recs.items():
            for r in _record_rows(sid, pid, rec, covered["pairs"]):
                out[r["table"]].append(r)
    explained: set = set()
    with_payload: set = set()
    for payload in sessions or []:
        if not isinstance(payload, dict):
            continue
        meta = payload.get("session_metadata") if isinstance(payload.get("session_metadata"), dict) else {}
        sid = str(meta.get("session_id") or "")
        with_payload.add(sid)
        extra = _payload_rows(payload, sid not in covered["compilation"], True)
        explained.update((x["session_id"], x["pair_id"]) for x in extra.pop("explanations"))
        def survey_key(r: Dict[str, Any]) -> tuple:
            key = (sid, str(r.get("survey_type")), _squash(str(r.get("question_text") or "")))
            pi = _int(r.get("problem_index"))  # stored rows without a problem (older sessions) match any
            return key if pi is None else key + (pi,)
        extra["surveys"] = [r for r in extra["surveys"] if survey_key(r) not in covered["surveys"]]
        for table, rows in extra.items():
            out[table].extend(rows)
    for r in out["autocomplete"] + out["agent"]:
        if r.get("source") == "server_record" and str(r.get("session_id")) in with_payload:
            pair = str(r.get("suggestion_set_id") or r.get("response_id") or "")
            if r.get("suggestion_status") == "accepted" and (str(r["session_id"]), pair) in explained:
                r["explanation_clicked"] = "Yes"  # a missing click is unknown: older pages did not log it
    for table, rows in out.items():
        for r in rows:
            c = _int(r.get("condition"))
            r["condition_name"] = CONDITION_NAMES.get(c, str(r.get("condition") or ""))
            r["participant_id"] = r.get("participant_id") or r.get("user_id") or ""
            _problem(r, labels)
        rows.sort(key=lambda r: (str(r.get("participant_id")), str(r.get("timestamp") or ""), _int(r.get("question_index")) or 0))
    out["suggestions"] = build_suggestions(out["autocomplete"], out["compilation"])
    out["responses"] = build_responses(out["agent"], out["compilation"])
    return out


def _files_text(files: List[Any]) -> str:
    return "\n\n".join(f"// {f.get('filename')}\n{f.get('content') or ''}" for f in files if isinstance(f, dict))[:20000]


def _files_dict(raw: Any) -> Optional[Dict[str, str]]:
    """{filename: content} from a JSON list of files (None when not recorded)."""
    try:
        files = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        return None
    if not isinstance(files, list):
        return None
    return {str(f.get("filename")): f.get("content") for f in files if isinstance(f, dict) and f.get("filename")}


def build_responses(versions: List[Dict[str, Any]], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The agent log for condition 3: one row per agent response (its versions grouped), with the
    correct and buggy versions, what the participant applied, and the first run after it."""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in versions:
        key = str(r.get("response_id") or r.get("row_id"))
        groups.setdefault((str(r.get("session_id")), key), []).append(r)
    rows: List[Dict[str, Any]] = []
    for (sid, rid), opts in groups.items():
        if all(o.get("suggestion_status") == "not_shown" for o in opts):
            continue
        opts.sort(key=lambda o: str(o.get("option_label") or ""))
        correct = next((o for o in opts if o.get("suggestion_correctness") == "correct"), None)
        buggy = [o for o in opts if o.get("suggestion_correctness") == "buggy"]
        chosen = next((o for o in opts if o.get("suggestion_status") == "accepted"), None)
        first = opts[0]
        has_code = any(o.get("suggestion_id") for o in opts)
        if chosen:
            status = "accepted"
        elif not has_code:
            status = str(first.get("suggestion_status") or "no_code")  # a reply without code to apply
        else:
            status = "rejected"
        detail = "accepted" if chosen else next((o.get("suggestion_status") for o in opts if o.get("suggestion_status")), "")
        gen = next((o.get("response_time_ms") for o in opts if str(o.get("response_time_ms") or "").strip() != ""), "")
        decide = (chosen or {}).get("time_to_accept_ms") or (chosen or {}).get("time_to_decision_ms") or next(
            (o.get("time_to_decision_ms") for o in opts if str(o.get("time_to_decision_ms") or "").strip() != ""), "")
        types = []
        for o in buggy:
            t = str(o.get("bug_types") or "")
            if o.get("description_error"):
                t += " + wrong claim in description"
            types.append(t)
        rows.append({
            "table": "responses", "source": first.get("source"), "participant_id": first.get("participant_id"),
            "session_id": sid, "condition": first.get("condition"), "condition_name": first.get("condition_name"),
            "problem_index": first.get("problem_index"), "problem_id": first.get("problem_id"),
            "problem_title": first.get("problem_title"),
            "timestamp": first.get("prompted_at") or first.get("timestamp"), "interaction_index": first.get("interaction_index"),
            "response_id": rid, "agent_mode": first.get("agent_mode", ""), "user_prompt": first.get("user_prompt", ""),
            "agent_correct_response": (correct or first if not has_code else correct or {}).get("agent_response", ""),
            "agent_correct_code": (correct or {}).get("suggestion_code", ""),
            "agent_buggy_response_type": "; ".join(t for t in types if t),
            "agent_buggy_response": "\n---\n".join(str(o.get("agent_response") or "") for o in buggy),
            "agent_buggy_code": "\n---\n".join(str(o.get("suggestion_code") or "") for o in buggy),
            "description_error": "; ".join(str(o.get("description_error")) for o in buggy if o.get("description_error")),
            "user_selected_status": status, "status_detail": detail,
            "user_selected_option": (chosen or {}).get("option_label", ""),
            "is_option_correct": "" if not chosen else ("Yes" if chosen.get("suggestion_correctness") == "correct"
                                                        else "No" if chosen.get("suggestion_correctness") == "buggy" else ""),
            "feedback_shown": (chosen or {}).get("feedback_shown", ""),
            "explanation": (chosen or {}).get("explanation", "") if chosen and chosen.get("suggestion_correctness") == "buggy" else "",
            "explanation_clicked": _clicked(chosen),
            "time_to_generate_ms": gen, "time_to_decide_ms": decide,
            "code_snapshot": first.get("code_snapshot") or "", "options_in_set": len(opts) if has_code else 0,
            "_files": _files_dict(first.get("workspace_files")), "_applied": _files_dict((chosen or {}).get("applied_files")),
            "_chosen": bool(chosen), "_reverted": (chosen or {}).get("reverted") == "Yes",
        })
    rows.sort(key=lambda r: (str(r["participant_id"]), str(r["session_id"]), _ts(r["timestamp"]) or 0))
    seqs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        seqs.setdefault((str(r["session_id"]), str(r.get("problem_index"))), []).append(r)
    runs_by: Dict[Tuple[str, str], List[Tuple[float, Dict[str, Any]]]] = {}
    for c in runs:
        t = _ts(c.get("timestamp"))
        if t is not None:
            runs_by.setdefault((str(c.get("session_id")), str(c.get("problem_index"))), []).append((t, c))
    for key, seq in seqs.items():
        their_runs = sorted(runs_by.get(key, []), key=lambda x: x[0])
        for i, r in enumerate(seq):
            if str(r.get("interaction_index") or "").strip() == "":
                r["interaction_index"] = i + 1
            start = _ts(r["timestamp"]) or 0
            end = _ts(seq[i + 1]["timestamp"]) if i + 1 < len(seq) else None
            run = next((c for t, c in their_runs if t >= start and (end is None or t < end)), None)
            r["code_run"] = f"Yes: {run.get('run_status') or run.get('compilation_status') or 'ran'}" if run else "No"
            r["code_run_id"] = (run or {}).get("compilation_attempt_id", "")
            r["code_edit_before_next"], r["code_edit_detail"] = _files_edit_before_next(r, seq[i + 1] if i + 1 < len(seq) else None)
    for r in rows:
        for k in ("_files", "_applied", "_chosen", "_reverted"):
            r.pop(k, None)
    return rows


def _files_edit_before_next(r: Dict[str, Any], nxt: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Whether the student changed their files between this response and their next prompt: the files at
    the next prompt against this prompt's files with the applied version on top (whitespace ignored)."""
    if nxt is None:
        return "", "last interaction on this problem"
    before, after = r.get("_files"), nxt.get("_files")
    if before is None or after is None or (r.get("_chosen") and r.get("_applied") is None and not r.get("_reverted")):
        return "", "not recorded"
    expected = dict(before)
    if r.get("_chosen") and not r.get("_reverted"):
        for name, content in (r.get("_applied") or {}).items():
            if content is None:
                expected.pop(name, None)  # deleted by the version
            else:
                expected[name] = content
    names = sorted(set(expected) | set(after))
    a = "".join(f"\0{n}\0{_squash(expected.get(n) or '')}" for n in names)
    b = "".join(f"\0{n}\0{_squash(after.get(n) or '')}" for n in names)
    if a == b:
        return "No", ""
    added = removed = 0
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            removed += a2 - a1
        if op in ("insert", "replace"):
            added += b2 - b1
    return "Yes", f"+{added} −{removed} chars"


def _clicked(chosen: Optional[Dict[str, Any]]) -> str:
    """"Yes"/"No" when known; "not recorded" when an explanation was offered but older pages could not
    log the click; "" when there was nothing to click (no buggy option accepted)."""
    if not chosen or chosen.get("suggestion_correctness") != "buggy":
        return ""
    v = str(chosen.get("explanation_clicked") or "")
    if v in ("Yes", "No"):
        return v
    return "not recorded" if chosen.get("source") == "server_record" else "No"


def build_suggestions(options: List[Dict[str, Any]], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The autocomplete log for conditions 2 and 4: one row per suggestion the participant saw (its
    options grouped), with the correct and buggy code, what they accepted and the first run after it.
    Suggestions written ahead and never shown are left out (they stay in the option rows)."""
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in options:
        groups.setdefault((str(r.get("session_id")), str(r.get("suggestion_set_id"))), []).append(r)
    rows: List[Dict[str, Any]] = []
    for (sid, set_id), opts in groups.items():
        if all(o.get("suggestion_status") in ("not_shown", None, "") for o in opts):
            continue
        opts.sort(key=lambda o: str(o.get("option_label") or ""))
        correct = next((o for o in opts if o.get("suggestion_correctness") == "correct"), None)
        buggy = [o for o in opts if o.get("suggestion_correctness") == "buggy"]
        chosen = next((o for o in opts if o.get("suggestion_status") == "accepted"), None)
        first = min(opts, key=lambda o: str(o.get("timestamp") or ""))
        detail = "accepted" if chosen else next((o.get("suggestion_status") for o in opts
                                                if o.get("suggestion_status") in ("rejected", "ignored", "not_accepted")), "not_accepted")
        gen = [o.get("generation_latency_ms") for o in opts if str(o.get("generation_latency_ms") or "").strip() != ""]
        rows.append({
            "table": "suggestions", "source": first.get("source"), "participant_id": first.get("participant_id"),
            "session_id": sid, "condition": first.get("condition"), "condition_name": first.get("condition_name"),
            "problem_index": first.get("problem_index"), "problem_id": first.get("problem_id"),
            "problem_title": first.get("problem_title"), "timestamp": first.get("timestamp"),
            "interaction_index": first.get("interaction_index"), "suggestion_set_id": set_id,
            "suggestion_position": first.get("suggestion_position") or "",
            "correct_suggestion_code": (correct or {}).get("suggestion_code", ""),
            "bug_types": ", ".join(str(o.get("bug_types") or "") for o in buggy if o.get("bug_types")),
            "buggy_code_suggestions": "\n---\n".join(str(o.get("suggestion_code") or "") for o in buggy),
            "time_to_accept_ms": (chosen or {}).get("time_to_accept_ms", ""),
            "time_to_generate_ms": max((int(float(g)) for g in gen), default=""),
            "suggestion_status": "accepted" if chosen else "rejected", "status_detail": detail,
            "accept_method": (chosen or {}).get("accept_method", ""),
            "accepted_code_suggestion": (chosen or {}).get("suggestion_code", ""),
            "accepted_suggestion_correct": "" if not chosen else ("Yes" if chosen.get("suggestion_correctness") == "correct"
                                                                  else "No" if chosen.get("suggestion_correctness") == "buggy" else ""),
            "bug_type_accepted": (chosen or {}).get("bug_types", "") if chosen and chosen.get("suggestion_correctness") == "buggy" else "",
            "feedback_shown": (chosen or {}).get("feedback_shown", ""),
            # The explanation the wrong-pick card offered (only after accepting a buggy suggestion).
            "explanation": (chosen or {}).get("explanation", "") if chosen and chosen.get("suggestion_correctness") == "buggy" else "",
            "explanation_clicked": _clicked(chosen),
            "code_snapshot": first.get("editor_snapshot") or "",
            "_cursor": first.get("cursor_offset"), "_cut": first.get("snapshot_cut") == "Yes",
            # What the student's code gained from this suggestion: the accepted option, or the correct
            # code when the wrong pick was swapped for it.
            "_inserted": ((correct or {}).get("suggestion_code", "") if chosen and chosen.get("feedback_shown") == "corrected"
                          else (chosen or {}).get("suggestion_code", "")) or "",
            "options_in_set": len(opts),
        })
    # Interaction number (where the browser did not record it) and the first run after each suggestion.
    rows.sort(key=lambda r: (str(r["participant_id"]), str(r["session_id"]), _ts(r["timestamp"]) or 0))
    by_person: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        by_person.setdefault((str(r["session_id"]), str(r.get("problem_index"))), []).append(r)
    runs_by: Dict[Tuple[str, str], List[Tuple[float, Dict[str, Any]]]] = {}
    for c in runs:
        t = _ts(c.get("timestamp"))
        if t is not None:
            runs_by.setdefault((str(c.get("session_id")), str(c.get("problem_index"))), []).append((t, c))
    for key, seq in by_person.items():
        their_runs = sorted(runs_by.get(key, []), key=lambda x: x[0])
        for i, r in enumerate(seq):
            if str(r.get("interaction_index") or "").strip() == "":
                r["interaction_index"] = i + 1
            start = _ts(r["timestamp"]) or 0
            end = _ts(seq[i + 1]["timestamp"]) if i + 1 < len(seq) else None
            run = next((c for t, c in their_runs if t >= start and (end is None or t < end)), None)
            r["code_run"] = f"Yes: {run.get('run_status') or run.get('compilation_status') or 'ran'}" if run else "No"
            r["code_run_id"] = (run or {}).get("compilation_attempt_id", "")
            r["code_edit_before_next"], r["code_edit_detail"] = _edit_before_next(r, seq[i + 1] if i + 1 < len(seq) else None)
    for r in rows:
        for k in ("_cursor", "_cut", "_inserted"):
            r.pop(k, None)
    return rows


def _squash(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def _edit_before_next(r: Dict[str, Any], nxt: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Whether the student changed the code between this suggestion and the next one: the code when the
    next suggestion appeared, against this suggestion's code with what it inserted (whitespace ignored,
    so Enter and indentation do not count). ("Yes" | "No" | "", detail like "+14 −3 chars")."""
    if nxt is None:
        return "", "last suggestion on this problem"
    snap, nsnap, cur = str(r.get("code_snapshot") or ""), str(nxt.get("code_snapshot") or ""), _int(r.get("_cursor"))
    if not snap or not nsnap or cur is None:
        return "", "not recorded"
    expected = _squash(snap[:cur] + str(r.get("_inserted") or "") + snap[cur:])
    actual = _squash(nsnap)
    approx = " (approx.: long file)" if r.get("_cut") or nxt.get("_cut") else ""
    if expected == actual:
        return "No", approx.strip(" ()")
    added = removed = 0
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes():
        if op in ("delete", "replace"):
            removed += a2 - a1
        if op in ("insert", "replace"):
            added += b2 - b1
    return "Yes", f"+{added} −{removed} chars{approx}"


def export_rows(root: Any, quiz_id: str, table: str, condition: int, labels: List[str],
                participant: Optional[str] = None, sessions: Optional[List[Dict[str, Any]]] = None,
                collected: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Dict[str, Any]]:
    """The rows of `table` for participants in `condition` (or only `participant`), in time order."""
    data = collected if collected is not None else collect_all(root, quiz_id, labels, sessions)
    return [r for r in data.get(table, []) if _int(r.get("condition")) == condition
            and (participant is None or str(r.get("participant_id")) == participant)]


def all_logs_csv(collected: Dict[str, List[Dict[str, Any]]]) -> str:
    """One big table: every row of every table and condition, with "log_type" and the union of columns."""
    columns = ["log_type", "source"]
    for t in DERIVED_TABLES + TABLES:
        for c in COLUMNS[t]:
            if c not in columns:
                columns.append(c)
    names = {"autocomplete": "autocomplete_option", "agent": "agent_version"}
    rows = [{**r, "log_type": names.get(t, t)} for t in DERIVED_TABLES + TABLES for r in collected.get(t, [])]
    rows.sort(key=lambda r: (str(r.get("condition_name")), str(r.get("participant_id")), str(r.get("timestamp") or "")))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_csv(table: str, rows: List[Dict[str, Any]]) -> str:
    columns = COLUMNS[table] + ["source"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


def available_tables(condition: int) -> List[str]:
    """The CSV files a condition has: its interaction table (none for No AI), compilation, surveys."""
    t = CONDITION_TABLE.get(condition)
    return ([t] if t else []) + ["compilation", "submissions", "surveys"]


# ─── File names and the per-participant view ─────────────────────────────────

TABLE_FILE = {"submissions": "code-submissions", "suggestions": "autocomplete-log", "autocomplete": "autocomplete-options", "responses": "agent-log", "agent": "agent-versions",
              "compilation": "compilation-log", "surveys": "survey-answers"}
TABLE_TITLE = {"submissions": "Code submissions", "suggestions": "Autocomplete log", "autocomplete": "Autocomplete options", "responses": "Agent log", "agent": "Agent versions",
               "compilation": "Compilation log", "surveys": "Survey answers"}


def export_filename(slug: str, condition: int, table: str, participant: Optional[str] = None) -> str:
    """A name unique per quiz, condition, table (and participant), e.g.
    "two-sum_C2-autocomplete-single-line_autocomplete-log.csv"."""
    cond = CONDITION_NAMES.get(condition, str(condition)).replace("_", "-")
    who = f"_{_key(participant)}" if participant else ""
    return f"{slug}_C{condition}-{cond}_{TABLE_FILE[table]}{who}.csv"


# Columns shown in the per-participant tables (the CSV has them all).
VIEW_COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    "submissions": [("timestamp", "Time"), ("problem_id", "Problem"), ("submission_index", "#"), ("final_submit", "Final"),
                    ("files", "Files"), ("code_lines", "Lines"), ("submitted_code", "Code")],
    "responses": [("timestamp", "Time"), ("problem_id", "Problem"), ("interaction_index", "#"), ("user_prompt", "Prompt"),
                  ("agent_correct_response", "Correct response"), ("agent_correct_code", "Correct code"),
                  ("agent_buggy_response_type", "Buggy type"), ("agent_buggy_response", "Buggy response"),
                  ("agent_buggy_code", "Buggy code"), ("user_selected_status", "Status"),
                  ("user_selected_option", "Option"), ("is_option_correct", "Option correct?"),
                  ("explanation", "Explanation"), ("explanation_clicked", "Explanation clicked"),
                  ("time_to_generate_ms", "Generate ms"), ("time_to_decide_ms", "Decide ms"),
                  ("code_snapshot", "Code snapshot"), ("code_run", "Code run"),
                  ("code_edit_before_next", "Code edit (before next)"), ("code_edit_detail", "Edit size")],
    "suggestions": [("timestamp", "Time"), ("problem_id", "Problem"), ("interaction_index", "#"),
                    ("suggestion_position", "Position"), ("correct_suggestion_code", "Correct suggestion"),
                    ("bug_types", "Bug types"), ("buggy_code_suggestions", "Buggy suggestion(s)"),
                    ("time_to_accept_ms", "Accept ms"), ("time_to_generate_ms", "Generate ms"),
                    ("suggestion_status", "Status"), ("accepted_code_suggestion", "Accepted code"),
                    ("accepted_suggestion_correct", "Accepted correct?"), ("explanation", "Explanation"),
                    ("explanation_clicked", "Explanation clicked"),
                    ("code_snapshot", "Code snapshot"), ("code_run", "Code run"),
                    ("code_edit_before_next", "Code edit (before next suggestion)"), ("code_edit_detail", "Edit size")],
    "autocomplete": [("timestamp", "Time"), ("problem_id", "Problem"), ("interaction_index", "#"),
                     ("option_label", "Option"), ("suggestion_code", "Code"), ("suggestion_correctness", "Correct?"),
                     ("bug_types", "Bug type"), ("suggestion_status", "Status"), ("accept_method", "How"),
                     ("time_to_accept_ms", "Accept ms"), ("previewed", "Previewed"), ("feedback_shown", "Feedback"),
                     ("corrected_code", "Edited"), ("code_in_final", "In final")],
    "agent": [("timestamp", "Time"), ("problem_id", "Problem"), ("turn_index", "Turn"), ("agent_mode", "Mode"),
              ("user_prompt", "Prompt"), ("option_label", "Option"), ("suggestion_correctness", "Correct?"),
              ("bug_types", "Bug type"), ("description_correctness", "Description"), ("suggestion_status", "Status"),
              ("time_to_accept_ms", "Accept ms"), ("response_time_ms", "Response ms"), ("corrected_code", "Edited"),
              ("reverted", "Reverted")],
    "compilation": [("timestamp", "Time"), ("problem_id", "Problem"), ("run_by", "By"), ("run_trigger", "Trigger"),
                    ("compilation_status", "Compile"), ("run_status", "Run"), ("exit_code", "Exit"),
                    ("total_time_ms", "Time ms"), ("input_wait_count", "Input waits"), ("compilation_error", "Compile error"),
                    ("runtime_error", "Runtime error")],
    "surveys": [("timestamp", "Time"), ("problem_id", "Problem"), ("survey_type", "Survey"), ("survey_round", "Round"),
                ("question_text", "Question"), ("answer", "Answer")],
}


def participant_rows(root: Any, quiz_id: str, participant: str, condition: int, labels: List[str],
                     sessions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """The participant's rows of every table their condition has."""
    data = collect_all(root, quiz_id, labels, sessions)
    raw = {"suggestions": "autocomplete", "responses": "agent"}.get(CONDITION_TABLE.get(condition, ""))
    tables = available_tables(condition) + ([raw] if raw else [])
    return {t: export_rows(root, quiz_id, t, condition, labels, participant, collected=data) for t in tables}


def _ts(v: Any) -> Optional[float]:
    """Seconds since the epoch for an ISO timestamp (with or without "Z"), or None."""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def timeline(rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """One participant's task as a single line of events for the activity-log chart: {"events": [[minutes,
    n, kind, detail], ...] in time order (n = 1, 2, …), "problems": [[minutes, "P1"], ...] where each
    problem starts}. Kinds: suggestion, accepted_correct, accepted_buggy, rejected (autocomplete, only
    suggestions that were shown); prompt, applied_correct, applied_buggy, rejected (agent); run_ok,
    run_failed."""
    events: List[Tuple[float, str, str, str]] = []  # (time, kind, detail, problem)

    def ms(v: Any) -> float:
        try:
            return float(v) / 1000
        except (TypeError, ValueError):
            return 0.0

    for r in rows.get("suggestions", []):
        t = _ts(r.get("timestamp"))
        if t is None:
            continue
        p = str(r.get("problem_id") or "")
        events.append((t, "suggestion", f"#{r.get('interaction_index')} at {r.get('suggestion_position') or ''}".strip(), p))
        if r.get("suggestion_status") == "accepted":
            ok = r.get("accepted_suggestion_correct") != "No"
            events.append((t + ms(r.get("time_to_accept_ms")), "accepted_correct" if ok else "accepted_buggy",
                           "" if ok else f"bug: {r.get('bug_types') or '?'}", p))
        else:
            events.append((t + 0.001, "rejected", str(r.get("status_detail") or ""), p))
    for r in rows.get("responses", []):
        t = _ts(r.get("timestamp"))
        if t is None:
            continue
        p = str(r.get("problem_id") or "")
        events.append((t, "prompt", str(r.get("user_prompt") or "")[:80], p))
        done = t + ms(r.get("time_to_generate_ms")) + ms(r.get("time_to_decide_ms"))
        if r.get("user_selected_status") == "accepted":
            ok = r.get("is_option_correct") != "No"
            events.append((done, "applied_correct" if ok else "applied_buggy",
                           "" if ok else f"bug: {r.get('agent_buggy_response_type') or '?'}", p))
        elif r.get("user_selected_status") == "rejected":
            events.append((done + 0.001, "rejected", str(r.get("status_detail") or ""), p))
    for r in rows.get("compilation", []):
        t = _ts(r.get("timestamp"))
        if t is None:
            continue
        failed = r.get("compilation_status") == "failure" or r.get("run_status") not in ("success", "not_run", None, "")
        events.append((t, "run_failed" if failed else "run_ok", str(r.get("run_status") or r.get("compilation_status") or ""),
                       str(r.get("problem_id") or "")))
    if not events:
        return {"events": [], "problems": []}
    events.sort(key=lambda e: e[0])
    start = events[0][0]
    out = [[round((t - start) / 60, 2), i + 1, kind, detail] for i, (t, kind, detail, _p) in enumerate(events)]
    first: Dict[str, float] = {}
    for t, _k, _d, p in events:
        if p and p not in first:
            first[p] = t
    return {"events": out, "problems": sorted([[round((t - start) / 60, 2), p] for p, t in first.items()])}


def session_ai_counts(collected: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Per session, the AI numbers of the analytics sessions table, from the study log: suggestions shown
    (C2/C4) or agent responses (C3), accepted, rejected, and the average time to generate (seconds, where
    recorded). Keys match quiz_analytics.derive_session_metrics."""
    out: Dict[str, Dict[str, Any]] = {}
    rows = [(r, r.get("suggestion_status")) for r in collected.get("suggestions", [])] + \
           [(r, r.get("user_selected_status")) for r in collected.get("responses", [])]
    gen_ms: Dict[str, List[Tuple[Optional[int], float]]] = {}
    for r, status in rows:
        sid = str(r.get("session_id") or "")
        if not sid:
            continue
        m = out.setdefault(sid, {"ai_generations_total": 0, "ai_generations_by_problem": {}, "accepts_total": 0,
                                 "accepts_by_problem": {}, "rejects_total": 0, "rejects_by_problem": {}})
        pi = _int(r.get("problem_index"))
        m["ai_generations_total"] += 1
        if pi is not None:
            m["ai_generations_by_problem"][pi] = m["ai_generations_by_problem"].get(pi, 0) + 1
        for key, hit in (("accepts", status == "accepted"), ("rejects", status == "rejected")):
            if hit:
                m[f"{key}_total"] += 1
                if pi is not None:
                    m[f"{key}_by_problem"][pi] = m[f"{key}_by_problem"].get(pi, 0) + 1
        try:
            ms = float(r.get("time_to_generate_ms"))
        except (TypeError, ValueError):
            continue  # not recorded (suggestions from before it was measured)
        gen_ms.setdefault(sid, []).append((pi, ms))
    for sid, vals in gen_ms.items():
        m = out[sid]
        m["avg_suggestion_time_seconds_total"] = sum(v for _, v in vals) / len(vals) / 1000
        by: Dict[int, List[float]] = {}
        for pi, v in vals:
            if pi is not None:
                by.setdefault(pi, []).append(v)
        m["avg_suggestion_time_seconds_by_problem"] = {pi: sum(v) / len(v) / 1000 for pi, v in by.items()}
    return out


def session_surveys(collected: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Per session, every survey answered, from the study log's surveys table (which keeps the problem of
    each survey; the stored session keeps only the last problem's periodic surveys, and older sessions
    only the last pre/post survey): {"pre"|"post": [{"problem_index", "qa"}], "periodic": [{"problem_index",
    "occurrence_number", "is_final", "qa"}]}, with qa as [{"q", "a"}] in question order."""
    groups: Dict[Tuple[str, str, int, str], List[Dict[str, Any]]] = {}
    for r in collected.get("surveys", []):
        sid, kind, pi = str(r.get("session_id") or ""), str(r.get("survey_type") or ""), _int(r.get("problem_index"))
        if not sid or pi is None or kind not in ("pre", "post", "periodic", "final"):
            continue
        groups.setdefault((sid, kind, pi, str(r.get("survey_round") or "")), []).append(r)
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for (sid, kind, pi, _round), rows in groups.items():
        rows.sort(key=lambda r: _int(r.get("question_index")) or 0)
        qa = [{"q": str(r.get("question_text") or "").strip() or "(question)",
               "a": "" if r.get("answer") is None else str(r.get("answer"))} for r in rows]
        entry = {"problem_index": pi, "qa": qa, "at": min(str(r.get("timestamp") or "") for r in rows)}
        if kind in ("periodic", "final"):
            entry["is_final"] = kind == "final"
        out.setdefault(sid, {}).setdefault("periodic" if kind in ("periodic", "final") else kind, []).append(entry)
    for by_kind in out.values():
        for kind, entries in by_kind.items():
            entries.sort(key=lambda e: (e["problem_index"], e["at"]))
        for n, e in enumerate(by_kind.get("periodic", []), 1):
            e["occurrence_number"] = n
    return out
