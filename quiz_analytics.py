"""Aggregate Firebase session payloads for instructor quiz analytics.

All session / per-problem duration fields exposed in JSON and CSV are in **seconds**.

Per-problem time uses editor/coding activity_log events per `problem_index` (excludes surveys);
submission timestamps are not used for time (they include pre-submit survey gaps).

Legacy sessions without submission rows or `problem_index` on activities may still show gaps.
Sessions without `quiz_id` in metadata are matched by `assignment_key` prefix `quiz_{uuid[:8]}`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def sanitize_for_json(obj: Any) -> Any:
    """Ensure JSON-serializable output (no NaN/Inf — breaks browser JSON.parse)."""
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {str(k): sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(x) for x in obj]
    if isinstance(obj, (int, str, bool)):
        return obj
    return str(obj)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _utc_for_delta(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize to UTC-aware so naive/aware timestamps never mix on subtraction."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _firebase_to_list(obj: Any) -> List[Any]:
    """Convert Firebase dict-as-array ({"0": v, "1": v, …}) to a Python list.

    Firebase RTDB may return sequential arrays as dicts with stringified integer
    keys when the data was modified or has gaps.  This helper normalises both
    representations so callers can always iterate over a list.
    """
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        int_keys: List[int] = []
        for k in obj:
            try:
                int_keys.append(int(k))
            except (TypeError, ValueError):
                return list(obj.values())
        if not int_keys:
            return []
        int_keys.sort()
        return [obj.get(str(k)) or obj.get(k) for k in int_keys]
    return []


def _int_pi(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _problem_index_from_entry(e: Dict[str, Any]) -> Optional[int]:
    """Resolve problem index from activity entry (multiple client/storage shapes)."""
    _sc = e.get("session_context")
    sc: Dict[str, Any] = _sc if isinstance(_sc, dict) else {}
    for key in ("problem_index", "problemIndex", "current_problem_index"):
        if key in sc:
            pi = _int_pi(sc.get(key))
            if pi is not None:
                return pi
    return _int_pi(e.get("problem_index"))


def _activity_entries(user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    al = user_data.get("activity_log")
    # Legacy RTDB: activity_log stored as a top-level list of entries (no {entries, summary} wrapper).
    if isinstance(al, list):
        return [x for x in al if isinstance(x, dict)]
    if not isinstance(al, dict):
        return []
    return _firebase_to_list(al.get("entries"))


def _session_meta(user_data: Dict[str, Any]) -> Dict[str, Any]:
    sm = user_data.get("session_metadata")
    return sm if isinstance(sm, dict) else {}


def _entry_activity_type(e: Dict[str, Any]) -> str:
    """Normalize activity type across client / export variants (camelCase, ``type`` alias)."""
    at = e.get("activity_type")
    if at is None:
        at = e.get("activityType") or e.get("type")
    return str(at or "").strip().lower()


def _safe_int_metric(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _distribute_count_by_problem(
    count: int,
    num_problems: int,
    completed_problem: List[bool],
) -> Dict[int, int]:
    """Spread a session-level integer across problem indices (prefer problems with submitted code)."""
    if count <= 0 or num_problems < 1:
        return {}
    if num_problems == 1:
        return {0: count}
    preferred = [i for i in range(num_problems) if i < len(completed_problem) and completed_problem[i]]
    targets = preferred if preferred else list(range(num_problems))
    n = len(targets)
    base = count // n
    rem = count % n
    out: Dict[int, int] = {}
    for j, pi in enumerate(targets):
        extra = 1 if j < rem else 0
        out[pi] = out.get(pi, 0) + base + extra
    return out


def _merge_int_maps(base: Dict[int, int], extra: Dict[int, int]) -> None:
    for k, v in extra.items():
        base[k] = base.get(k, 0) + v


def _activity_log_summary_dict(user_data: Dict[str, Any]) -> Dict[str, Any]:
    al = user_data.get("activity_log")
    if not isinstance(al, dict):
        return {}
    s = al.get("summary")
    return s if isinstance(s, dict) else {}


def _ai_generation_count_from_logs(user_data: Dict[str, Any]) -> int:
    """When activity entries lack AI rows, infer generations from stored Firebase ``logs`` (server SessionLog)."""
    logs = user_data.get("logs")
    if not isinstance(logs, dict):
        return 0
    ag = _normalize_log_dict_map(logs.get("ai_guesses"))
    so = _normalize_log_dict_map(logs.get("suggestions_only"))
    cg = _normalize_log_dict_map(logs.get("correct_guesses"))
    meta = _session_meta(user_data)
    try:
        cond = int(meta.get("condition") or 0)
    except (TypeError, ValueError):
        cond = 0
    if cond == 1:
        return max(len(ag), len(cg))
    if cond == 3:
        return max(len(ag), len(so))
    return len(ag)


# Approximate seconds to subtract per survey_interaction that falls between first/last editor
# events on the same problem (we only have point events, not survey duration).
_SURVEY_BLOCK_GUESS_SECONDS = 300.0


def _is_survey_activity_type(at: str) -> bool:
    return str(at or "").strip().lower() == "survey_interaction"


def _is_editor_time_activity(at: str) -> bool:
    """
    Activities that count toward "time on problem while working in the editor".
    Excludes surveys, tab/session noise, and passive idle.
    """
    a = str(at or "").strip().lower()
    if not a or _is_survey_activity_type(a):
        return False
    if a in (
        "session_start",
        "session_end",
        "tab_switching",
        "tab_visibility_change",
        "page_unload_attempt",
        "idle",
    ):
        return False
    # Editor + runs + AI/suggestions (same problem_index)
    if a in (
        "code_editing_insert",
        "code_editing_delete",
        "code_editing_modify",
        "code_edit",
        "code_execution",
        "paste_attempt",
        "ai_analysis_trigger",
        "ai_analysis_complete",
        "copilot_popup_cleared_for_analysis",
        "copilot_popup_accept",
        "copilot_popup_reject",
        "suggestion_accept",
        "suggestion_reject",
        "accept_suggestion",
        "reject_suggestion",
        "suggestion_interaction",
        "suggestion_click",
        "execution_output",
        "history_interaction",
        "history_view",
        "history_check",
        "font_size_change",
        "terminal_clear",
    ):
        return True
    return False


def _problem_editor_time_seconds(
    editor_ts: List[datetime],
    survey_ts: List[datetime],
) -> float:
    """
    Seconds from first to last editor/coding event on this problem, minus a rough allowance
    for each survey_interaction that falls strictly between those endpoints (surveys are not coding time).
    """
    if len(editor_ts) < 2:
        return 0.0
    times_sorted = sorted(editor_ts, key=lambda t: _utc_for_delta(t) or datetime.min.replace(tzinfo=timezone.utc))
    t0 = _utc_for_delta(times_sorted[0])
    t1 = _utc_for_delta(times_sorted[-1])
    if not t0 or not t1 or t1 <= t0:
        return 0.0
    span_sec = (t1 - t0).total_seconds()
    survey_between = 0
    for s in survey_ts:
        su = _utc_for_delta(s)
        if su and t0 < su < t1:
            survey_between += 1
    subtract_sec = min(float(span_sec), survey_between * _SURVEY_BLOCK_GUESS_SECONDS)
    return max(0.0, span_sec - subtract_sec)


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _focus_time_seconds_from_activity(user_data: Dict[str, Any]) -> Optional[float]:
    """Active/focus time from activity_log.summary (server ActivitySummary), if present."""
    al = user_data.get("activity_log")
    if not isinstance(al, dict):
        return None
    summ = al.get("summary")
    if not isinstance(summ, dict):
        return None
    fts = _safe_float(summ.get("focus_time_seconds"))
    if fts is None or fts < 0:
        return None
    return fts


def _submission_code_str(row: Dict[str, Any]) -> str:
    raw = row.get("code")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _final_code_per_problem(meta: Dict[str, Any], num_problems: int) -> List[Optional[str]]:
    """
    Latest code per problem from session_metadata.per_problem_submissions (by timestamp),
    then override with rows marked final_submit=True. Falls back to submitted_code for
    single-problem sessions when no per-row code exists.
    """
    rows = _firebase_to_list(meta.get("per_problem_submissions"))
    best: Dict[int, Tuple[Optional[datetime], str]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        pi = _int_pi(row.get("problem_index"))
        if pi is None or pi < 0 or pi >= num_problems:
            continue
        code = _submission_code_str(row)
        ts = _parse_iso(row.get("timestamp"))
        tzu = _utc_for_delta(ts) if ts else None
        cur = best.get(pi)
        if cur is None:
            best[pi] = (tzu, code)
            continue
        cur_t, _cur_c = cur
        if tzu is None:
            if cur_t is None:
                best[pi] = (None, code)
        elif cur_t is None or tzu >= cur_t:
            best[pi] = (tzu, code)

    out: List[Optional[str]] = []
    for i in range(num_problems):
        if i in best:
            c = best[i][1]
            out.append(c if c.strip() else None)
        else:
            out.append(None)

    for row in rows:
        if not isinstance(row, dict) or row.get("final_submit") is not True:
            continue
        pi = _int_pi(row.get("problem_index"))
        if pi is None or pi < 0 or pi >= num_problems:
            continue
        code = _submission_code_str(row)
        if code.strip():
            out[pi] = code

    sub = meta.get("submitted_code")
    if isinstance(sub, str) and sub.strip():
        if num_problems == 1 and (len(out) < 1 or out[0] is None or not str(out[0]).strip()):
            out[0] = sub

    return out


def _answer_display(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (list, dict)):
        try:
            return json.dumps(val, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(val)
    return str(val)


def _qa_from_pre_post_block(block: Any) -> List[Dict[str, str]]:
    """Build question/answer pairs from Firebase pre_survey or post_survey payload."""
    if not isinstance(block, dict):
        return []
    out: List[Dict[str, str]] = []
    qlist = _firebase_to_list(block.get("questions"))
    rlist = _firebase_to_list(block.get("responses"))
    id_to_q: Dict[str, str] = {}
    for q in qlist:
        if isinstance(q, dict):
            qid = str(q.get("id") or "")
            if qid:
                id_to_q[qid] = str(q.get("question") or "").strip()
    for r in rlist:
        if not isinstance(r, dict):
            continue
        qtext = str(r.get("question_text") or "").strip()
        if not qtext:
            qid = str(r.get("question_id") or "")
            qtext = id_to_q.get(qid, "") or "(question)"
        out.append({"q": qtext, "a": _answer_display(r.get("answer"))})
    return out


def _periodic_occurrence_dicts(user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    ps = user_data.get("periodic_surveys")
    if not isinstance(ps, dict):
        return []
    occ = ps.get("occurrences")
    if isinstance(occ, list):
        return [x for x in occ if isinstance(x, dict)]
    if isinstance(occ, dict):
        return [v for v in occ.values() if isinstance(v, dict)]
    return []


def _qa_from_periodic_occurrence(occ: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in _firebase_to_list(occ.get("responses")):
        if not isinstance(r, dict):
            continue
        qtext = str(r.get("question") or "").strip()
        if not qtext:
            qtext = str(r.get("question_text") or "").strip()
        if not qtext:
            qtext = "(question)"
        resp = r.get("response")
        if resp is None and "answer" in r:
            resp = r.get("answer")
        out.append({"q": qtext, "a": _answer_display(resp)})
    return out


def _survey_history_by_problem(user_data: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    """Every pre or post survey of the session, one per problem: [{"problem_index", "qa"}] by problem.
    From session_metadata.survey_history (the top-level pre_survey/post_survey keep only the latest)."""
    history = _session_meta(user_data).get("survey_history") or user_data.get("survey_history")
    out: Dict[int, List[Dict[str, str]]] = {}
    for h in _firebase_to_list(history):
        if not isinstance(h, dict) or h.get("survey_type") != kind:
            continue
        qa = _qa_from_pre_post_block({"responses": h.get("responses")})
        if qa:
            out[_int_pi(h.get("problem_index")) or 0] = qa
    return [{"problem_index": pi, "qa": out[pi]} for pi in sorted(out)]


def _summarize_survey_payload(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize pre/post/periodic survey Q&A for instructor JSON and UI.
    Reads top-level Firebase keys: pre_survey, post_survey, periodic_surveys, and
    session_metadata.survey_history for the pre/post survey of every problem ("by_problem").
    """
    pre = user_data.get("pre_survey")
    post = user_data.get("post_survey")
    pre_by_problem = _survey_history_by_problem(user_data, "pre")
    post_by_problem = _survey_history_by_problem(user_data, "post")
    # "qa" stays the latest survey (the only one older sessions have)
    pre_qa = pre_by_problem[-1]["qa"] if pre_by_problem else _qa_from_pre_post_block(pre) if pre else []
    post_qa = post_by_problem[-1]["qa"] if post_by_problem else _qa_from_pre_post_block(post) if post else []

    periodic_rows: List[Dict[str, Any]] = []
    for occ in _periodic_occurrence_dicts(user_data):
        raw_num = occ.get("occurrence_number")
        if raw_num is None:
            raw_num = occ.get("occurrence")
        try:
            num_int = int(raw_num) if raw_num is not None else 0
        except (TypeError, ValueError):
            num_int = 0
        periodic_rows.append({"occurrence_number": num_int, "qa": _qa_from_periodic_occurrence(occ)})
    periodic_rows.sort(key=lambda x: int(x.get("occurrence_number") or 0))

    out: Dict[str, Any] = {}
    if pre_qa:
        out["pre"] = {"qa": pre_qa, "by_problem": pre_by_problem}
    if post_qa:
        out["post"] = {"qa": post_qa, "by_problem": post_by_problem}
    if periodic_rows:
        out["periodic"] = {"occurrences": periodic_rows}
    return out


def derive_session_metrics(
    user_data: Dict[str, Any],
    quiz_id: str,
    num_problems: int,
) -> Optional[Dict[str, Any]]:
    """
    Return per-session metrics or None if this row should be skipped (wrong quiz).
    """
    meta = _session_meta(user_data)
    qid = meta.get("quiz_id")
    quiz_norm = str(quiz_id).strip().lower()
    if qid is not None and str(qid).strip().lower() != quiz_norm:
        return None

    assignment_key = (meta.get("assignment_key") or "").strip()
    expected_prefix = f"quiz_{quiz_id[:8]}".lower()
    if assignment_key and not assignment_key.lower().startswith(expected_prefix):
        return None

    end_time = meta.get("end_time")
    completed_assignment = bool(end_time)

    session_id = str(meta.get("session_id") or "")
    user_id = _resolved_participant_user_id(meta.get("user_id"), session_id)
    condition = meta.get("condition")

    per_rows = _firebase_to_list(meta.get("per_problem_submissions"))

    indices_with_code: set = set()
    for row in per_rows:
        if not isinstance(row, dict):
            continue
        pi = _int_pi(row.get("problem_index"))
        if pi is not None:
            indices_with_code.add(pi)

    completed_problem = [False] * max(1, num_problems)
    for k in range(num_problems):
        if k in indices_with_code:
            completed_problem[k] = True

    # Total time (seconds, wall clock start → end)
    st = _parse_iso(meta.get("starting_time"))
    en = _parse_iso(end_time) if end_time else None
    total_time_seconds: Optional[float] = None
    if st and en:
        stu = _utc_for_delta(st)
        enu = _utc_for_delta(en)
        if stu and enu:
            total_time_seconds = max(0.0, (enu - stu).total_seconds())

    entries = _activity_entries(user_data)
    has_problem_index = False

    # Per-problem time: use editor/coding-related events only (excludes surveys); see _problem_editor_time_seconds.
    editor_ts_by_pi: Dict[int, List[datetime]] = {}
    survey_ts_by_pi: Dict[int, List[datetime]] = {}
    tss_by_pi: Dict[int, List[float]] = {}
    ai_gen_by_pi: Dict[int, int] = {}
    ai_trig_by_pi: Dict[int, int] = {}
    acc_by_pi: Dict[int, int] = {}
    rej_by_pi: Dict[int, int] = {}
    run_by_pi: Dict[int, int] = {}
    ai_gen_total = 0
    ai_trig_total = 0
    acc_total = 0
    rej_total = 0
    run_total = 0
    ai_gen_time_ms_total = 0.0
    ai_gen_time_ms_count = 0
    ai_gen_time_ms_by_pi: Dict[int, float] = {}
    ai_gen_time_ms_count_by_pi: Dict[int, int] = {}
    tab_switch_by_pi: Dict[int, int] = {}
    paste_attempt_by_pi: Dict[int, int] = {}
    tab_switch_total = 0
    paste_attempt_total = 0

    for e in entries:
        if not isinstance(e, dict):
            continue
        at = _entry_activity_type(e)
        _ad = e.get("activity_details")
        ad: Dict[str, Any] = _ad if isinstance(_ad, dict) else {}
        _sc = e.get("session_context")
        sc: Dict[str, Any] = _sc if isinstance(_sc, dict) else {}
        pi = _problem_index_from_entry(e)
        if pi is not None:
            has_problem_index = True

        ts = _parse_iso(e.get("timestamp"))
        if ts is not None and pi is not None:
            if _is_editor_time_activity(at):
                editor_ts_by_pi.setdefault(pi, []).append(ts)
            if _is_survey_activity_type(at):
                survey_ts_by_pi.setdefault(pi, []).append(ts)

        if pi is not None and _is_editor_time_activity(at):
            _tss_raw = sc.get("time_since_start")
            if _tss_raw is not None:
                try:
                    tss = float(_tss_raw)
                    if not math.isnan(tss) and not math.isinf(tss):
                        tss_by_pi.setdefault(pi, []).append(tss)
                except (TypeError, ValueError):
                    pass

        if at == "ai_analysis_complete":
            sg = ad.get("suggestion_generated")
            if sg is True or sg == "true" or sg == 1:
                ai_gen_total += 1
                if pi is not None:
                    ai_gen_by_pi[pi] = ai_gen_by_pi.get(pi, 0) + 1
                t_ms = _safe_float(ad.get("analysis_time_ms"))
                if t_ms is not None and t_ms >= 0:
                    ai_gen_time_ms_total += t_ms
                    ai_gen_time_ms_count += 1
                    if pi is not None:
                        ai_gen_time_ms_by_pi[pi] = ai_gen_time_ms_by_pi.get(pi, 0.0) + t_ms
                        ai_gen_time_ms_count_by_pi[pi] = ai_gen_time_ms_count_by_pi.get(pi, 0) + 1
        elif at == "code_suggestion_generated":
            # Some clients emit this instead of/in addition to ai_analysis_complete + suggestion_generated.
            ai_gen_total += 1
            if pi is not None:
                ai_gen_by_pi[pi] = ai_gen_by_pi.get(pi, 0) + 1
        elif at == "ai_analysis_trigger":
            ai_trig_total += 1
            if pi is not None:
                ai_trig_by_pi[pi] = ai_trig_by_pi.get(pi, 0) + 1
        elif at == "code_execution":
            run_total += 1
            if pi is not None:
                run_by_pi[pi] = run_by_pi.get(pi, 0) + 1
        elif at in (
            "copilot_popup_accept",
            "suggestion_accept",
            "accept_suggestion",
        ):
            acc_total += 1
            if pi is not None:
                acc_by_pi[pi] = acc_by_pi.get(pi, 0) + 1
        elif at in (
            "copilot_popup_reject",
            "suggestion_reject",
            "reject_suggestion",
        ):
            rej_total += 1
            if pi is not None:
                rej_by_pi[pi] = rej_by_pi.get(pi, 0) + 1
        elif at == "tab_switching":
            tab_switch_total += 1
            if pi is not None:
                tab_switch_by_pi[pi] = tab_switch_by_pi.get(pi, 0) + 1
        elif at == "paste_attempt":
            paste_attempt_total += 1
            if pi is not None:
                paste_attempt_by_pi[pi] = paste_attempt_by_pi.get(pi, 0) + 1

    time_per_problem_seconds: Dict[int, float] = {}
    for pi, ed in editor_ts_by_pi.items():
        if len(ed) < 2:
            if len(ed) == 1:
                time_per_problem_seconds[pi] = 0.0
            continue
        sv = survey_ts_by_pi.get(pi) or []
        secs = _problem_editor_time_seconds(ed, sv)
        if secs > 0:
            time_per_problem_seconds[pi] = secs

    for pi, arr in tss_by_pi.items():
        if len(arr) < 1:
            continue
        span_sec = max(arr) - min(arr)
        if span_sec < 0:
            continue
        prev = time_per_problem_seconds.get(pi, 0.0) or 0.0
        time_per_problem_seconds[pi] = max(prev, span_sec)

    focus_time_seconds = _focus_time_seconds_from_activity(user_data)

    # Fallback when no editor-derived spans: split focus time (preferred) or wall clock, never submission segments
    # (submission timestamps include survey time before "next problem" / final submit).
    if not any((time_per_problem_seconds.get(i, 0.0) or 0.0) > 0 for i in range(num_problems)):
        budget = focus_time_seconds if focus_time_seconds and focus_time_seconds > 0 else total_time_seconds
        if budget and budget > 0:
            if num_problems == 1:
                time_per_problem_seconds[0] = float(budget)
            else:
                share = float(budget) / max(num_problems, 1)
                for pi in range(num_problems):
                    time_per_problem_seconds[pi] = share

    # Balance remainder toward focus time when available (not wall clock — avoids survey inflation).
    # Only top up problems with *no* editor/coding activity rows — never inflate a problem we already
    # measured (including intentional ~0 after survey adjustment between editor events).
    if focus_time_seconds and focus_time_seconds > 0 and num_problems > 0:
        for pi in range(num_problems):
            time_per_problem_seconds.setdefault(pi, 0.0)
        known_sum = sum(float(time_per_problem_seconds.get(i, 0.0) or 0.0) for i in range(num_problems))
        remainder = float(focus_time_seconds) - known_sum
        if remainder > 15.0:
            zeros = [
                i
                for i in range(num_problems)
                if (time_per_problem_seconds.get(i, 0.0) or 0.0) <= 0.0
                and completed_problem[i]
                and len(editor_ts_by_pi.get(i) or []) == 0
            ]
            if not zeros:
                zeros = [
                    i
                    for i in range(num_problems)
                    if (time_per_problem_seconds.get(i, 0.0) or 0.0) <= 0.0
                    and len(editor_ts_by_pi.get(i) or []) == 0
                ]
            if zeros:
                share = remainder / len(zeros)
                for i in zeros:
                    time_per_problem_seconds[i] = float(time_per_problem_seconds.get(i, 0.0) or 0.0) + share

    summary = _activity_log_summary_dict(user_data)

    # Fallback runs: ``activity_log.summary`` is updated by the server's /log-activities handler even when
    # individual ``entries`` were throttled, lost, or stored under alternate keys.
    if run_total == 0:
        te = summary.get("total_code_executions")
        nruns = _safe_int_metric(te)
        if nruns > 0:
            run_total = nruns
            _merge_int_maps(run_by_pi, _distribute_count_by_problem(nruns, num_problems, completed_problem))
    if run_total == 0:
        cc_val = _safe_int_metric(meta.get("compiler_clicked"))
        if cc_val > 0:
            run_total = cc_val
            _merge_int_maps(run_by_pi, _distribute_count_by_problem(cc_val, num_problems, completed_problem))

    # Fallback AI generations: Firebase ``logs`` mirrors ``SessionLog.ai_guess_history`` at submit time.
    if ai_gen_total == 0:
        lg = _ai_generation_count_from_logs(user_data)
        if lg > 0:
            ai_gen_total = lg
            _merge_int_maps(ai_gen_by_pi, _distribute_count_by_problem(lg, num_problems, completed_problem))

    # Fallback analysis triggers: summary counter (paired with ai_analysis_trigger on server).
    if ai_trig_total == 0:
        ta = summary.get("total_ai_analyses")
        nt = _safe_int_metric(ta)
        if nt > 0:
            ai_trig_total = nt
            _merge_int_maps(ai_trig_by_pi, _distribute_count_by_problem(nt, num_problems, completed_problem))

    # Note: do not use session_metadata.suggestion_clicked as accepts_total — the client increments it
    # on both accept and reject (total copilot interactions).

    avg_suggestion_time_seconds_total = (
        (ai_gen_time_ms_total / 1000.0) / ai_gen_time_ms_count if ai_gen_time_ms_count > 0 else 0.0
    )
    avg_suggestion_time_seconds_by_pi: Dict[int, float] = {}
    for pi, total_ms in ai_gen_time_ms_by_pi.items():
        cnt = ai_gen_time_ms_count_by_pi.get(pi, 0)
        if cnt > 0:
            avg_suggestion_time_seconds_by_pi[pi] = (total_ms / 1000.0) / cnt

    final_code_per_problem = _final_code_per_problem(meta, num_problems)
    programming_language = str(meta.get("programming_language") or "").strip()

    survey_summary = _summarize_survey_payload(user_data)

    resolved_quiz_id = str(qid).strip() if qid is not None else quiz_norm

    return {
        "user_id": user_id,
        "session_id": session_id,
        "quiz_id": resolved_quiz_id,
        "starting_time": str(meta.get("starting_time") or "").strip(),
        "end_time": str(meta.get("end_time") or "").strip(),
        "condition": condition,
        "completed_assignment": completed_assignment,
        "completed_problem": completed_problem[:num_problems] if num_problems else [False],
        "total_time_seconds": total_time_seconds,
        "focus_time_seconds": focus_time_seconds,
        "final_code_per_problem": final_code_per_problem,
        "programming_language": programming_language,
        "time_per_problem_seconds": time_per_problem_seconds,
        "ai_generations_total": ai_gen_total,
        "ai_generations_by_problem": ai_gen_by_pi,
        "ai_analyses_total": ai_trig_total,
        "ai_analyses_by_problem": ai_trig_by_pi,
        "avg_suggestion_time_seconds_total": avg_suggestion_time_seconds_total,
        "avg_suggestion_time_seconds_by_problem": avg_suggestion_time_seconds_by_pi,
        "accepts_total": acc_total,
        "rejects_total": rej_total,
        "accepts_by_problem": acc_by_pi,
        "rejects_by_problem": rej_by_pi,
        "runs_total": run_total,
        "runs_by_problem": run_by_pi,
        "tab_switch_total": tab_switch_total,
        "paste_attempt_total": paste_attempt_total,
        "tab_switch_by_problem": tab_switch_by_pi,
        "paste_attempt_by_problem": paste_attempt_by_pi,
        "has_problem_index_on_activities": has_problem_index,
        "survey_summary": survey_summary,
    }


def _by_problem_num(d: Any, i: int, default: float = 0.0) -> float:
    """Read per-problem map from analytics (int or str keys after JSON round-trips)."""
    if not isinstance(d, dict):
        return default
    v = d.get(i)
    if v is None:
        v = d.get(str(i))
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _activity_entry_count(user_data: Dict[str, Any]) -> int:
    al = user_data.get("activity_log")
    if not isinstance(al, dict):
        return 0
    return len(_firebase_to_list(al.get("entries")))


def _per_problem_row_count(user_data: Dict[str, Any]) -> int:
    meta = _session_meta(user_data)
    return len(_firebase_to_list(meta.get("per_problem_submissions")))


def _primary_session_duration_seconds(d: Dict[str, Any]) -> Optional[float]:
    """Session duration for KPI / time charts: matches the instructor table (focus when logged, else wall)."""
    f = d.get("focus_time_seconds")
    if f is not None:
        try:
            fv = float(f)
            if not math.isnan(fv) and fv > 0:
                return fv
        except (TypeError, ValueError):
            pass
    t = d.get("total_time_seconds")
    if t is not None:
        try:
            tv = float(t)
            if not math.isnan(tv):
                return tv
        except (TypeError, ValueError):
            pass
    return None


def _normalize_log_dict_map(obj: Any) -> List[Dict[str, Any]]:
    """Convert Firebase dict ``{'1': {...}, '2': {...}}`` to an ordered list of dicts."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    keys: List[int] = []
    for k in obj.keys():
        try:
            keys.append(int(k))
        except (TypeError, ValueError):
            pass
    keys.sort()
    out: List[Dict[str, Any]] = []
    for k in keys:
        row = obj.get(str(k))
        if row is None:
            row = obj.get(k)
        if isinstance(row, dict):
            out.append(row)
    return out


_LOG_ACCEPT_TYPES = frozenset(
    {"copilot_popup_accept", "suggestion_accept", "accept_suggestion"}
)
_LOG_REJECT_TYPES = frozenset(
    {"copilot_popup_reject", "suggestion_reject", "reject_suggestion"}
)

# Activity types shown on the instructor interaction timeline (X=time, Y=sequence index).
_INTERACTION_TIMELINE_ACTIVITY_TYPES = frozenset(
    {
        "session_start",
        "code_editing_insert",
        "code_editing_delete",
        "code_editing_modify",
        "ai_analysis_trigger",
        "ai_analysis_complete",
        "code_suggestion_generated",
        "suggestion_popup_shown",
        "copilot_popup_accept",
        "copilot_popup_reject",
        "code_execution",
        "paste_attempt",
        "tab_switching",
        "code_submitted",
    }
)


def _timeline_category_for_activity_type(at: str) -> str:
    """Coarse bucket for chart coloring."""
    a = str(at or "").strip().lower()
    if a.startswith("code_editing"):
        return "editing"
    if a in (
        "ai_analysis_trigger",
        "ai_analysis_complete",
        "code_suggestion_generated",
        "suggestion_popup_shown",
    ):
        return "ai"
    if a in _LOG_ACCEPT_TYPES or a in _LOG_REJECT_TYPES:
        return "suggestion"
    if a == "code_execution":
        return "run"
    if a == "paste_attempt":
        return "paste"
    if a == "tab_switching":
        return "tab"
    if a == "session_start":
        return "session"
    if a == "code_submitted":
        return "submit"
    return "other"


def _build_timeline_points_by_problem(user_data: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Chronological interaction index (1..N) per problem for activity_log entries
    in :data:`_INTERACTION_TIMELINE_ACTIVITY_TYPES`.
    """
    entries = _sorted_activity_entries_chrono(user_data)
    by_pi: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if not isinstance(e, dict):
            continue
        at = _entry_activity_type(e)
        if at not in _INTERACTION_TIMELINE_ACTIVITY_TYPES:
            continue
        pi = _problem_index_from_entry(e)
        if pi is None:
            pi = 0
        ts = _parse_iso(e.get("timestamp"))
        if ts is None:
            continue
        by_pi[pi].append(e)

    out: Dict[int, List[Dict[str, Any]]] = {}
    for pi in sorted(by_pi.keys()):
        pts: List[Dict[str, Any]] = []
        for i, e in enumerate(by_pi[pi], start=1):
            at = _entry_activity_type(e)
            ts = _parse_iso(e.get("timestamp"))
            tu = _utc_for_delta(ts)
            if not tu:
                continue
            t_ms = int(tu.timestamp() * 1000)
            pts.append(
                {
                    "interaction_index": i,
                    "timestamp_ms": t_ms,
                    "timestamp_iso": _dt_to_iso_display(tu) or "",
                    "activity_type": at,
                    "chart_category": _timeline_category_for_activity_type(at),
                }
            )
        out[pi] = pts
    return out


def _sorted_activity_entries_chrono(user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _activity_entries(user_data)
    out = [e for e in raw if isinstance(e, dict)]

    def sort_key(e: Dict[str, Any]) -> datetime:
        t = _parse_iso(e.get("timestamp"))
        if t is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        u = _utc_for_delta(t)
        return u if u else datetime.min.replace(tzinfo=timezone.utc)

    return sorted(out, key=sort_key)


def _ai_suggestion_completion_events(user_data: Dict[str, Any]) -> List[Tuple[Optional[datetime], int]]:
    """``ai_analysis_complete`` with ``suggestion_generated``, chronologically — for pairing with ``logs.ai_guesses``."""
    out: List[Tuple[Optional[datetime], int]] = []
    for e in _sorted_activity_entries_chrono(user_data):
        at = _entry_activity_type(e)
        if at != "ai_analysis_complete":
            continue
        _ad = e.get("activity_details")
        ad: Dict[str, Any] = _ad if isinstance(_ad, dict) else {}
        sg = ad.get("suggestion_generated")
        if not (sg is True or sg == "true" or sg == 1):
            continue
        ts = _parse_iso(e.get("timestamp"))
        pi = _problem_index_from_entry(e)
        out.append((ts, int(pi) if pi is not None else 0))
    return out


def _problem_indices_for_guess_rows(
    base_rows: List[Dict[str, Any]], completions: List[Tuple[Optional[datetime], int]]
) -> List[int]:
    """One problem_index per Firebase guess row (aligned to activity completions or nearest timestamp)."""
    n = len(base_rows)
    if n == 0:
        return []
    m = len(completions)
    if m == 0:
        return [0] * n
    if m == n:
        return [completions[i][1] for i in range(n)]
    if m > n:
        return [completions[i][1] for i in range(n)]
    # m < n: nearest completion timestamp per guess
    indices: List[int] = []
    for g in base_rows:
        gt = _parse_iso(g.get("timestamp"))
        if gt is None:
            indices.append(completions[-1][1])
            continue
        gu = _utc_for_delta(gt)
        best_pi = completions[0][1]
        best_sec: Optional[float] = None
        for ts, pi in completions:
            if ts is None:
                continue
            tu = _utc_for_delta(ts)
            if not gu or not tu:
                continue
            sec = abs((gu - tu).total_seconds())
            if best_sec is None or sec < best_sec:
                best_sec = sec
                best_pi = pi
        indices.append(best_pi)
    return indices


def _all_ai_analysis_trigger_times(entries_sorted: List[Dict[str, Any]]) -> List[Optional[datetime]]:
    """Chronological ``ai_analysis_trigger`` timestamps (client clock — pairs with copilot events)."""
    out: List[Optional[datetime]] = []
    for e in entries_sorted:
        at = _entry_activity_type(e)
        if at != "ai_analysis_trigger":
            continue
        ts = _parse_iso(e.get("timestamp"))
        out.append(_utc_for_delta(ts) if ts else None)
    return out


def _all_copilot_reactions_chrono(
    entries_sorted: List[Dict[str, Any]],
) -> List[Tuple[Optional[datetime], str, Dict[str, Any]]]:
    """``(utc_datetime, 'accepted'|'rejected', entry)`` for each copilot popup action, in log order."""
    out: List[Tuple[Optional[datetime], str, Dict[str, Any]]] = []
    for e in entries_sorted:
        at = _entry_activity_type(e)
        ts = _parse_iso(e.get("timestamp"))
        tu = _utc_for_delta(ts) if ts else None
        if at in _LOG_ACCEPT_TYPES:
            out.append((tu, "accepted", e))
        elif at in _LOG_REJECT_TYPES:
            out.append((tu, "rejected", e))
    return out


def _snapshot_from_copilot_activity_entry(e: Dict[str, Any]) -> str:
    """Editor snapshot logged on accept/reject (post-decision), if present."""
    ad = e.get("activity_details")
    if not isinstance(ad, dict):
        return ""
    return str(ad.get("user_code_snapshot") or "").strip()


def _dt_to_iso_display(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc).isoformat()
        return dt.isoformat()
    except Exception:
        return None


def _first_accept_reject_between_timestamps(
    entries_sorted: List[Dict[str, Any]],
    start_ts: Optional[datetime],
    end_ts: Optional[datetime],
) -> Tuple[Optional[str], Optional[float], str]:
    """Last-resort pairing: first copilot action at or after ``start_ts``, before ``end_ts`` (Firebase guess windows)."""
    if start_ts is None:
        return None, None, ""
    su = _utc_for_delta(start_ts)
    eu = _utc_for_delta(end_ts) if end_ts else None
    for e in entries_sorted:
        ts = _parse_iso(e.get("timestamp"))
        if ts is None:
            continue
        tu = _utc_for_delta(ts)
        if tu is None:
            continue
        if su and tu < su:
            continue
        if eu is not None and tu >= eu:
            break
        at = _entry_activity_type(e)
        snap = _snapshot_from_copilot_activity_entry(e)
        if at in _LOG_ACCEPT_TYPES:
            sec = (tu - su).total_seconds() if su and tu else None
            return "accepted", float(sec) if sec is not None else None, snap
        if at in _LOG_REJECT_TYPES:
            sec = (tu - su).total_seconds() if su and tu else None
            return "rejected", float(sec) if sec is not None else None, snap
    return None, None, ""


def _verdict_latency_for_suggestion_row(
    entries_sorted: List[Dict[str, Any]],
    completion_events: List[Tuple[Optional[datetime], int]],
    row_index: int,
    guess_timestamp_raw: Any,
    next_guess_ts: Optional[datetime],
) -> Tuple[Optional[str], Optional[float], Optional[str], str]:
    """
    Pair each Firebase guess row with accept/reject using **activity_log** only for latency:

    1. Start time: ``ai_analysis_trigger`` at ``row_index`` (same clock as copilot).
    2. Window: after that trigger, before the next ``ai_analysis_trigger`` (if any).
    3. Fallback start: ``ai_analysis_complete`` (suggestion_generated) at same index.
    4. Fallback start: Firebase guess ``timestamp``.
    5. Fallback verdict: ``i``-th copilot accept/reject in chronological order (ordinal match).

    Returns ``(verdict, reaction_latency_seconds, suggestion_time_iso, activity_code_snapshot)``.
    The fourth value is the editor snapshot from the paired ``copilot_popup_*`` activity when logged.
    """
    triggers = _all_ai_analysis_trigger_times(entries_sorted)
    reactions = _all_copilot_reactions_chrono(entries_sorted)

    t_start: Optional[datetime] = None
    activity_snapshot = ""
    if row_index < len(triggers) and triggers[row_index] is not None:
        t_start = triggers[row_index]
    elif row_index < len(completion_events) and completion_events[row_index][0] is not None:
        t_start = _utc_for_delta(completion_events[row_index][0])
    else:
        t_start = _utc_for_delta(_parse_iso(guess_timestamp_raw))

    t_next_trigger: Optional[datetime] = None
    if row_index + 1 < len(triggers) and triggers[row_index + 1] is not None:
        t_next_trigger = triggers[row_index + 1]

    suggestion_time_iso = _dt_to_iso_display(t_start) or (
        str(guess_timestamp_raw).strip() if guess_timestamp_raw else None
    )

    verdict: Optional[str] = None
    reaction_seconds: Optional[float] = None

    if t_start is not None:
        for tu, v, entry in reactions:
            if tu is None:
                continue
            if tu <= t_start:
                continue
            if t_next_trigger is not None and tu >= t_next_trigger:
                break
            verdict = v
            reaction_seconds = float((tu - t_start).total_seconds())
            activity_snapshot = _snapshot_from_copilot_activity_entry(entry)
            break

    # Ordinal fallback: i-th copilot reaction ↔ i-th suggestion (ordering matches guesses)
    if verdict is None and row_index < len(reactions):
        tu, v, entry = reactions[row_index]
        if tu is not None and t_start is not None:
            verdict = v
            reaction_seconds = float((tu - t_start).total_seconds())
            activity_snapshot = _snapshot_from_copilot_activity_entry(entry)
        elif tu is not None:
            verdict = v
            reaction_seconds = None
            activity_snapshot = _snapshot_from_copilot_activity_entry(entry)

    # Last resort: Firebase guess timestamp window (older sessions without ai_analysis_trigger)
    if verdict is None:
        gs = _parse_iso(guess_timestamp_raw)
        verdict, reaction_seconds, activity_snapshot = _first_accept_reject_between_timestamps(
            entries_sorted, gs, next_guess_ts
        )
        if verdict is not None and gs is not None and suggestion_time_iso is None:
            suggestion_time_iso = _dt_to_iso_display(_utc_for_delta(gs)) or str(guess_timestamp_raw).strip()

    if reaction_seconds is not None and reaction_seconds < 0:
        reaction_seconds = 0.0

    return verdict, reaction_seconds, suggestion_time_iso, activity_snapshot


def _synthetic_ai_guess_rows_from_activity(user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """When ``logs.ai_guesses`` is empty, build pseudo-rows from ``ai_analysis_complete`` activity entries."""
    out: List[Dict[str, Any]] = []
    for e in _sorted_activity_entries_chrono(user_data):
        if _entry_activity_type(e) != "ai_analysis_complete":
            continue
        ad = e.get("activity_details") if isinstance(e.get("activity_details"), dict) else {}
        sg = ad.get("suggestion_generated")
        if not (sg is True or sg == "true" or sg == 1):
            continue
        out.append(
            {
                "timestamp": e.get("timestamp"),
                "ai_analysis": str(
                    ad.get("analysis_result") or ad.get("analysis") or ad.get("problem_analysis") or ""
                ).strip(),
                "next_line_suggestion": str(
                    ad.get("next_line_suggestion") or ad.get("next_line") or ""
                ).strip(),
                "full_code_suggestion": str(ad.get("full_code_suggestion") or "").strip(),
                "user_code_snapshot": str(ad.get("user_code_snapshot") or "").strip(),
            }
        )
    return out


def build_user_ai_activity_log_rows(user_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    One row per Firebase ``logs.ai_guesses`` (or ``suggestions_only``).

    Accept/reject and latency use ``activity_log``: ``ai_analysis_trigger`` start time (client clock),
    first ``copilot_popup_*`` before the next trigger; fallbacks for legacy data.
    """
    logs = user_data.get("logs")
    if not isinstance(logs, dict):
        logs = {}

    guesses = _normalize_log_dict_map(logs.get("ai_guesses"))
    sugg_only = _normalize_log_dict_map(logs.get("suggestions_only"))
    if not guesses and not sugg_only:
        guesses = _synthetic_ai_guess_rows_from_activity(user_data)

    if not guesses and sugg_only:
        base_rows = sugg_only
        source = "suggestions_only"
    else:
        base_rows = guesses
        source = "ai_guesses"

    entries_sorted = _sorted_activity_entries_chrono(user_data)
    completions = _ai_suggestion_completion_events(user_data)

    out_rows: List[Dict[str, Any]] = []
    for i, g in enumerate(base_rows):
        next_ts: Optional[datetime] = None
        if i + 1 < len(base_rows):
            next_ts = _parse_iso(base_rows[i + 1].get("timestamp"))

        if source == "suggestions_only":
            suggestion = str(g.get("suggestion_only") or g.get("next_line_suggestion") or "")
            interpretation = ""
        else:
            suggestion = str(g.get("next_line_suggestion") or g.get("full_code_suggestion") or "")
            interpretation = str(g.get("ai_analysis") or "")

        verdict, reaction_seconds, suggestion_time_iso, activity_snapshot = _verdict_latency_for_suggestion_row(
            entries_sorted,
            completions,
            i,
            g.get("timestamp"),
            next_ts,
        )
        ts_display = suggestion_time_iso if suggestion_time_iso else g.get("timestamp")

        snapshot = (activity_snapshot or "").strip() or str(g.get("user_code_snapshot") or "")

        out_rows.append(
            {
                "index": i + 1,
                "timestamp": ts_display,
                "suggestion": suggestion,
                "accept_reject": verdict,
                "reaction_latency_seconds": reaction_seconds,
                "interpretation": interpretation,
                "code_snapshot": snapshot,
            }
        )

    problem_indices = _problem_indices_for_guess_rows(out_rows, completions)
    for i, r in enumerate(out_rows):
        r["problem_index"] = problem_indices[i] if i < len(problem_indices) else 0

    return out_rows


def build_user_ai_activity_log(
    user_data: Dict[str, Any], problem_labels: Optional[List[str]] = None
) -> Dict[str, Any]:
    meta = _session_meta(user_data)
    rows = build_user_ai_activity_log_rows(user_data)

    labels = problem_labels if problem_labels and len(problem_labels) > 0 else []
    by_pi: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        try:
            pi = int(r.get("problem_index") or 0)
        except (TypeError, ValueError):
            pi = 0
        by_pi[pi].append(r)

    problem_tables: List[Dict[str, Any]] = []
    for pi in sorted(by_pi.keys()):
        sub = by_pi[pi]
        for j, row in enumerate(sub):
            row["index_in_problem"] = j + 1
        title = ""
        if labels and 0 <= pi < len(labels):
            title = str(labels[pi]).strip()
        if not title:
            title = f"Problem {pi + 1}"
        problem_tables.append({"problem_index": pi, "label": title, "rows": sub})

    timelines = _build_timeline_points_by_problem(user_data)
    for pt in problem_tables:
        try:
            tpi = int(pt.get("problem_index") or 0)
        except (TypeError, ValueError):
            tpi = 0
        pt["timeline"] = {
            "problem_index": tpi,
            "label": str(pt.get("label") or ""),
            "points": timelines.get(tpi, []),
        }

    return sanitize_for_json(
        {
            "user_id": meta.get("user_id"),
            "session_id": meta.get("session_id"),
            "rows": rows,
            "problem_tables": problem_tables,
        }
    )


def find_user_session_payload_for_quiz(
    sessions: List[Dict[str, Any]],
    quiz_id: str,
    user_id: str,
    num_problems: int,
) -> Optional[Dict[str, Any]]:
    """
    Return the Firebase user payload for this user on this quiz (same matching rules as
    aggregate_quiz_analytics). If multiple payloads exist, prefer the one with the most
    AI log rows and the latest ``end_time``.
    """
    want = str(user_id).strip()
    if not want:
        return None
    matches: List[Dict[str, Any]] = []
    for ud in sessions:
        if str(_session_meta(ud).get("user_id") or "").strip() != want:
            continue
        if derive_session_metrics(ud, quiz_id, num_problems) is None:
            continue
        matches.append(ud)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def score(ud: Dict[str, Any]) -> Tuple[int, str]:
        lg = ud.get("logs")
        if not isinstance(lg, dict):
            return 0, ""
        ag = len(_normalize_log_dict_map(lg.get("ai_guesses")))
        so = len(_normalize_log_dict_map(lg.get("suggestions_only")))
        return max(ag, so), str(_session_meta(ud).get("end_time") or "")

    return max(matches, key=score)


def _session_condition_key(d: Dict[str, Any]) -> str:
    cond_raw = d.get("condition")
    return str(cond_raw) if cond_raw is not None else "unknown"


def _resolved_participant_user_id(meta_user: Any, session_id: str) -> str:
    """Prefer real login user_id; never surface the literal 'unknown' in exports."""
    s = str(meta_user).strip() if meta_user is not None else ""
    if s and s.lower() not in ("anonymous", "unknown"):
        return s
    sid = (session_id or "").strip()
    if sid:
        return f"session:{sid}"
    return "unattributed"


def _grouped_condition_order_for_charts(all_conds: List[str]) -> List[str]:
    """Order conditions for grouped charts. Numeric RCT arms 1-4 always reserve their slots."""
    if not all_conds:
        return []
    obs_set = set(all_conds)
    non_unknown = {c for c in obs_set if c != "unknown"}
    if non_unknown and non_unknown.issubset({"1", "2", "3", "4"}):
        out = ["1", "2", "3", "4"] if "4" in non_unknown else ["1", "2", "3"]
        if "unknown" in obs_set:
            out.append("unknown")
        return out
    return list(all_conds)


def _median_float(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    if len(s) % 2:
        return s[m]
    return (s[m - 1] + s[m]) / 2.0


def _compute_boxplot_payload(
    derived: List[Dict[str, Any]],
    n: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build summary + boxplot blocks for a list of derived sessions (whole quiz or one condition)."""
    total_times: List[float] = []
    for d in derived:
        if not d["completed_assignment"]:
            continue
        t = _primary_session_duration_seconds(d)
        if t is not None:
            total_times.append(t)

    time_per_problem_series: List[List[float]] = [[] for _ in range(n)]
    ai_per_problem_series: List[List[float]] = [[] for _ in range(n)]
    ai_analyses_per_problem_series: List[List[float]] = [[] for _ in range(n)]
    acc_per_problem_series: List[List[float]] = [[] for _ in range(n)]
    rej_per_problem_series: List[List[float]] = [[] for _ in range(n)]
    run_per_problem_series: List[List[float]] = [[] for _ in range(n)]

    for d in derived:
        tpm = d.get("time_per_problem_seconds") or {}
        agb = d.get("ai_generations_by_problem") or {}
        aab = d.get("ai_analyses_by_problem") or {}
        acb = d.get("accepts_by_problem") or {}
        rjb = d.get("rejects_by_problem") or {}
        rub = d.get("runs_by_problem") or {}
        for i in range(n):
            time_per_problem_series[i].append(_by_problem_num(tpm, i, 0.0))
            ai_per_problem_series[i].append(_by_problem_num(agb, i, 0.0))
            ai_analyses_per_problem_series[i].append(_by_problem_num(aab, i, 0.0))
            acc_per_problem_series[i].append(_by_problem_num(acb, i, 0.0))
            rej_per_problem_series[i].append(_by_problem_num(rjb, i, 0.0))
            run_per_problem_series[i].append(_by_problem_num(rub, i, 0.0))

    ai_gen_total_series = [float(d["ai_generations_total"]) for d in derived]
    ai_analyses_total_series = [float(d.get("ai_analyses_total") or 0.0) for d in derived]
    acc_total_series = [float(d["accepts_total"]) for d in derived]
    rej_total_series = [float(d["rejects_total"]) for d in derived]
    run_total_series = [float(d.get("runs_total") or 0.0) for d in derived]

    summary: Dict[str, Any] = {
        "median_session_time_seconds": _median_float(total_times),
        "median_ai_generations": _median_float([float(d["ai_generations_total"]) for d in derived]),
        "median_ai_analyses": _median_float([float(d.get("ai_analyses_total") or 0.0) for d in derived]),
        "median_accepts": _median_float([float(d["accepts_total"]) for d in derived]),
        "median_rejects": _median_float([float(d["rejects_total"]) for d in derived]),
        "median_runs": _median_float([float(d.get("runs_total") or 0.0) for d in derived]),
    }

    boxplot: Dict[str, Any] = {
        "total_time_seconds": total_times,
        "time_per_problem_seconds": time_per_problem_series,
        "ai_generations_total": ai_gen_total_series,
        "ai_generations_per_problem": ai_per_problem_series,
        "ai_analyses_total": ai_analyses_total_series,
        "ai_analyses_per_problem": ai_analyses_per_problem_series,
        "accepts_total": acc_total_series,
        "rejects_total": rej_total_series,
        "accepts_per_problem": acc_per_problem_series,
        "rejects_per_problem": rej_per_problem_series,
        "runs_total": run_total_series,
        "runs_per_problem": run_per_problem_series,
    }
    return summary, boxplot


def aggregate_quiz_analytics(
    sessions: List[Dict[str, Any]],
    quiz_id: str,
    problem_labels: List[str],
    ai_counts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    sessions: list of user_data dicts from Firebase (session_metadata + activity_log, ...).
    ai_counts: per session id, AI numbers from the study log (study_log.session_ai_counts) that replace
    the ones counted from activity events (inline suggestions and the agent do not log those).
    """
    n = len(problem_labels)
    if n < 1:
        problem_labels = ["Problem 1"]
        n = 1

    derived: List[Dict[str, Any]] = []
    raw_matched: List[Dict[str, Any]] = []
    for ud in sessions:
        if not isinstance(ud, dict):
            continue
        m = derive_session_metrics(ud, quiz_id, n)
        if m is not None:
            if ai_counts and m.get("session_id") in ai_counts:
                m.update(ai_counts[m["session_id"]])
            derived.append(m)
            raw_matched.append(ud)

    sessions_considered = len(derived)
    completed_count = sum(1 for d in derived if d["completed_assignment"])
    incomplete_count = sessions_considered - completed_count

    # Per-condition breakdown
    condition_totals: Dict[str, int] = {}
    condition_completed: Dict[str, int] = {}
    condition_incomplete: Dict[str, int] = {}
    for d in derived:
        cond_raw = d.get("condition")
        cond_key = str(cond_raw) if cond_raw is not None else "unknown"
        condition_totals[cond_key] = condition_totals.get(cond_key, 0) + 1
        if d["completed_assignment"]:
            condition_completed[cond_key] = condition_completed.get(cond_key, 0) + 1
        else:
            condition_incomplete[cond_key] = condition_incomplete.get(cond_key, 0) + 1

    all_conds = sorted(condition_totals.keys(), key=lambda k: (k == "unknown", k))
    grouped_chart_conditions = _grouped_condition_order_for_charts(all_conds)
    condition_breakdown = {
        "conditions": grouped_chart_conditions,
        "totals": [condition_totals.get(c, 0) for c in grouped_chart_conditions],
        "completed": [condition_completed.get(c, 0) for c in grouped_chart_conditions],
        "incomplete": [condition_incomplete.get(c, 0) for c in grouped_chart_conditions],
    }

    completion_by_problem = [0] * n
    completion_by_problem_by_condition: Dict[str, List[int]] = {
        c: [0] * n for c in grouped_chart_conditions
    }
    for d in derived:
        cp = d.get("completed_problem") or []
        for i in range(min(n, len(cp))):
            if cp[i]:
                completion_by_problem[i] += 1
        ck = _session_condition_key(d)
        if ck not in completion_by_problem_by_condition:
            completion_by_problem_by_condition[ck] = [0] * n
        for i in range(min(n, len(cp))):
            if cp[i]:
                completion_by_problem_by_condition[ck][i] += 1

    any_pi = any(d.get("has_problem_index_on_activities") for d in derived)

    summary, boxplot = _compute_boxplot_payload(derived, n)

    boxplot_by_condition: Dict[str, Dict[str, Any]] = {}
    summary_by_condition: Dict[str, Dict[str, Any]] = {}
    for c in grouped_chart_conditions:
        sub = [d for d in derived if _session_condition_key(d) == c]
        s_c, bp_c = _compute_boxplot_payload(sub, n)
        summary_by_condition[c] = s_c
        boxplot_by_condition[c] = bp_c

    # Coverage: confirm Firebase payloads used by analytics contain expected fields
    total_act_entries = 0
    total_pps_rows = 0
    with_act = 0
    with_pps = 0
    with_suggestion_meta = 0
    for raw in raw_matched:
        na = _activity_entry_count(raw)
        nr = _per_problem_row_count(raw)
        total_act_entries += na
        total_pps_rows += nr
        if na > 0:
            with_act += 1
        if nr > 0:
            with_pps += 1
        meta = _session_meta(raw)
        if _int_pi(meta.get("suggestion_clicked")) not in (None, 0):
            with_suggestion_meta += 1

    has_any_accept = sum(1 for d in derived if float(d.get("accepts_total") or 0) > 0)

    per_session_rows: List[Dict[str, Any]] = []
    for d in derived:
        tpm = d.get("time_per_problem_seconds") or {}
        agb = d.get("ai_generations_by_problem") or {}
        acb = d.get("accepts_by_problem") or {}
        rjb = d.get("rejects_by_problem") or {}
        rub = d.get("runs_by_problem") or {}
        tsb = d.get("tab_switch_by_problem") or {}
        pab = d.get("paste_attempt_by_problem") or {}
        avg_s_by_pi = d.get("avg_suggestion_time_seconds_by_problem") or {}
        completed_prob = d.get("completed_problem") or [False] * n
        _raw_fc = d.get("final_code_per_problem") or []
        final_codes = [_raw_fc[i] if i < len(_raw_fc) else None for i in range(n)]

        per_session_rows.append(
            {
                "user_id": _resolved_participant_user_id(d.get("user_id"), str(d.get("session_id") or "")),
                "session_id": d.get("session_id") or "",
                "quiz_id": d.get("quiz_id") or quiz_id,
                "condition": d.get("condition"),
                "starting_time": d.get("starting_time") or "",
                "end_time": d.get("end_time") or "",
                "completed_assignment": d.get("completed_assignment"),
                "total_time_seconds": d.get("total_time_seconds"),
                "focus_time_seconds": d.get("focus_time_seconds"),
                "per_problem_time_seconds": [_by_problem_num(tpm, i, 0.0) for i in range(n)],
                "per_problem_ai_generated": [_by_problem_num(agb, i, 0.0) for i in range(n)],
                "per_problem_accepted": [_by_problem_num(acb, i, 0.0) for i in range(n)],
                "per_problem_rejected": [_by_problem_num(rjb, i, 0.0) for i in range(n)],
                "per_problem_avg_suggestion_time_seconds": [
                    _by_problem_num(avg_s_by_pi, i, 0.0) for i in range(n)
                ],
                "per_problem_compiles": [_by_problem_num(rub, i, 0.0) for i in range(n)],
                "per_problem_tab_switches": [int(_by_problem_num(tsb, i, 0.0)) for i in range(n)],
                "per_problem_paste_attempts": [int(_by_problem_num(pab, i, 0.0)) for i in range(n)],
                "ai_generated_total": d.get("ai_generations_total") or 0,
                "ai_analyses_total": int(d.get("ai_analyses_total") or 0),
                "accepted_total": d.get("accepts_total") or 0,
                "rejected_total": d.get("rejects_total") or 0,
                "avg_suggestion_time_seconds_total": d.get("avg_suggestion_time_seconds_total") or 0.0,
                "compiles_total": d.get("runs_total") or 0,
                "tab_switch_total": int(d.get("tab_switch_total") or 0),
                "paste_attempt_total": int(d.get("paste_attempt_total") or 0),
                "completed_problem": [bool(x) for x in completed_prob[:n]],
                "final_code_per_problem": final_codes,
                "programming_language": d.get("programming_language") or "",
                "survey_summary": d.get("survey_summary") or {},
            }
        )

    per_problem_completion: List[Dict[str, Any]] = []
    for i in range(n):
        per_problem_completion.append(
            {
                "problem_index": i,
                "label": problem_labels[i] if i < len(problem_labels) else f"Problem {i + 1}",
                "sessions_with_submitted_code": completion_by_problem[i],
            }
        )

    data_health: Dict[str, Any] = {
        "sessions_matched_this_quiz": sessions_considered,
        "sessions_with_activity_log": with_act,
        "total_activity_log_entries": total_act_entries,
        "sessions_with_per_problem_submissions": with_pps,
        "total_per_problem_submission_rows": total_pps_rows,
        "sessions_with_suggestion_clicked_metadata": with_suggestion_meta,
        "sessions_with_nonzero_accept_count": has_any_accept,
        "firebase_user_data_paths_used": [
            "session_metadata (quiz_id, assignment_key, times, per_problem_submissions, suggestion_clicked, …)",
            "activity_log.entries (timestamps, activity_type incl. tab_switching / paste_attempt, session_context.problem_index)",
            "logs.ai_guesses (AI analysis history)",
            "pre_survey / post_survey / periodic_surveys (questions + responses)",
        ],
    }

    payload = {
        "problem_labels": problem_labels,
        "sessions_considered": sessions_considered,
        "completed_assignments": completed_count,
        "incomplete_sessions": incomplete_count,
        "condition_breakdown": condition_breakdown,
        "grouped_chart_conditions": grouped_chart_conditions,
        "completion_by_problem": completion_by_problem,
        "completion_by_problem_by_condition": completion_by_problem_by_condition,
        "per_problem_completion": per_problem_completion,
        "per_problem_activity_breakdown": any_pi,
        "per_session": per_session_rows,
        "data_health": data_health,
        "summary": summary,
        "boxplot": boxplot,
        "summary_by_condition": summary_by_condition,
        "boxplot_by_condition": boxplot_by_condition,
    }
    return sanitize_for_json(payload)  # type: ignore[return-value]


def _checkpoint_session_to_user_data(checkpoint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a checkpoint payload (raw SessionLog dump) into the user_data shape analytics expects."""
    sess = checkpoint.get("session")
    if not isinstance(sess, dict):
        return None
    session_id = sess.get("session_id") or ""
    if not session_id:
        return None
    user_id = _resolved_participant_user_id(
        sess.get("user_id") or checkpoint.get("user_id"),
        session_id,
    )

    session_metadata: Dict[str, Any] = {
        "user_id": user_id,
        "assignment_key": sess.get("assignment_id") or checkpoint.get("assignment_id") or "",
        "session_id": session_id,
        "condition": sess.get("condition") or checkpoint.get("condition"),
        "programming_language": "unknown",
        "starting_time": sess.get("code_start_timestamp"),
        "end_time": sess.get("code_submit_timestamp"),  # None for truly incomplete
        "submitted_code": sess.get("submitted_full_code"),
        "submitted_files": sess.get("submitted_files"),
        "suggestion_clicked": 0,
        "compiler_clicked": 0,
        "total_problems": sess.get("total_problems", 1),
        "per_problem_submissions": sess.get("per_problem_submissions") or [],
        "survey_history": sess.get("survey_history") or [],
    }
    if sess.get("quiz_id"):
        session_metadata["quiz_id"] = sess["quiz_id"]

    activity_entries = sess.get("activity_log") or []
    activity_summary = sess.get("activity_summary") or {}
    activity_log_data: Dict[str, Any] = {
        "entries": activity_entries if isinstance(activity_entries, list) else [],
        "summary": activity_summary if isinstance(activity_summary, dict) else {},
    }

    logs_data: Dict[str, Any] = {"ai_guesses": {}}
    ai_hist = sess.get("ai_guess_history") or []
    if isinstance(ai_hist, list):
        for i, entry in enumerate(ai_hist, 1):
            if isinstance(entry, dict):
                logs_data["ai_guesses"][str(i)] = {
                    "ai_analysis": entry.get("problem_analysis", ""),
                    "timestamp": entry.get("timestamp", ""),
                    "next_line_suggestion": entry.get("next_line_suggestion", ""),
                    "full_code_suggestion": entry.get("full_code_suggestion", ""),
                    "user_code_snapshot": entry.get("user_code_snapshot", ""),
                }

    user_data: Dict[str, Any] = {
        "session_metadata": session_metadata,
        "logs": logs_data,
        "activity_log": activity_log_data,
        "_from_checkpoint": True,
    }

    if sess.get("pre_survey_responses") and isinstance(sess["pre_survey_responses"], dict):
        user_data["pre_survey"] = {
            "status": sess.get("pre_survey_status", "not_started"),
            "timestamp": sess.get("pre_survey_timestamp"),
            "completed": sess.get("pre_survey_status") in ("completed", "COMPLETED"),
            "questions": sess["pre_survey_responses"].get("questions", []),
            "responses": sess["pre_survey_responses"].get("responses", []),
        }

    if sess.get("periodic_surveys") and isinstance(sess["periodic_surveys"], list):
        occs: Dict[str, Any] = {}
        for idx, ps in enumerate(sess["periodic_surveys"]):
            if isinstance(ps, dict):
                key = f"occurrence_{ps.get('occurrence', idx + 1)}"
                occs[key] = ps
        if occs:
            user_data["periodic_surveys"] = {"metadata": {}, "occurrences": occs}

    return user_data


def collect_quiz_sessions_from_firebase(db, quiz_id: str) -> List[Dict[str, Any]]:
    """Load user payloads from condition_1..4 / quiz_{prefix} / * and mirror sessions/final_submit.

    Also picks up incomplete sessions from sessions/{id}/checkpoint when no
    final_submit exists, so abandoned/in-progress students appear in analytics.
    """
    prefix = f"quiz_{quiz_id[:8]}"
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _append_payload(payload: Dict[str, Any]) -> None:
        _sm = payload.get("session_metadata")
        meta: Dict[str, Any] = _sm if isinstance(_sm, dict) else {}
        sid = meta.get("session_id")
        if sid:
            if sid in seen_ids:
                return
            seen_ids.add(sid)
        out.append(payload)

    for cond in (1, 2, 3, 4):
        ref = db.reference(f"condition_{cond}/{prefix}")
        snap = ref.get()
        if not snap:
            continue
        if not isinstance(snap, dict):
            continue
        for _uid, payload in snap.items():
            if isinstance(payload, dict):
                _append_payload(payload)

    # Mirror path (same payload as save_session_to_firebase writes under sessions/{id}/final_submit)
    needle = f"_quiz_{quiz_id[:8]}_"
    checkpoint_candidates: List[Dict[str, Any]] = []
    try:
        sessions_root = db.reference("sessions")
        shallow = sessions_root.get(shallow=True)
        if isinstance(shallow, dict):
            for sid in shallow.keys():
                if not isinstance(sid, str) or needle not in sid:
                    continue
                final = db.reference(f"sessions/{sid}/final_submit").get()
                if isinstance(final, dict):
                    ud = final.get("user_data")
                    if isinstance(ud, dict):
                        _append_payload(ud)
                else:
                    # No final_submit — try checkpoint (incomplete session)
                    cp = db.reference(f"sessions/{sid}/checkpoint").get()
                    if isinstance(cp, dict):
                        checkpoint_candidates.append(cp)
    except Exception as e:
        logger.warning("sessions mirror scan failed: %s", e)

    # Convert checkpoint-only sessions to user_data format
    for cp in checkpoint_candidates:
        ud = _checkpoint_session_to_user_data(cp)
        if ud is not None:
            _append_payload(ud)

    return out


def validate_analytics_user_id(raw: Optional[str]) -> str:
    """Reject path-like or empty user ids for Firebase delete/export safety."""
    s = (raw or "").strip()
    if not s or len(s) > 256:
        raise ValueError("Invalid user id")
    if "/" in s or "\x00" in s:
        raise ValueError("Invalid user id")
    return s


def _session_final_submit_matches_user_quiz(
    final: Dict[str, Any], uid: str, quiz_id_full: str
) -> bool:
    """True if final_submit payload is this user's session for this quiz (same rules as analytics)."""
    ud = final.get("user_data")
    if not isinstance(ud, dict):
        return False
    meta = ud.get("session_metadata")
    if not isinstance(meta, dict):
        return False
    if str(meta.get("user_id") or "") != uid:
        return False
    qn = str(quiz_id_full).strip().lower()
    mq = meta.get("quiz_id")
    if mq is not None and str(mq).strip().lower() == qn:
        return True
    ak = str(meta.get("assignment_key") or "").strip().lower()
    return ak.startswith(f"quiz_{quiz_id_full[:8]}".lower())


def delete_user_data_for_quiz(db, quiz_id: str, user_id: str) -> Dict[str, Any]:
    """Delete Firebase session payloads for one user on this quiz so analytics re-aggregate excludes them.

    Removes:
    - ``condition_{1,2,3,4}/quiz_{quiz_id[:8]}/{user_id}`` (always attempted; RTDB delete is idempotent)
    - ``sessions/{session_id}`` when session id matches ``{user_id}_quiz_{quiz_id[:8]}_*``, or when
      ``sessions/.../final_submit`` mirrors this user + quiz (covers legacy / odd session ids)
    """
    uid = validate_analytics_user_id(user_id)
    q = str(quiz_id or "").strip()
    if len(q) < 8:
        raise ValueError("Invalid quiz id")
    prefix = f"quiz_{q[:8]}"
    deleted_paths: List[str] = []

    for cond in (1, 2, 3, 4):
        path = f"condition_{cond}/{prefix}/{uid}"
        ref = db.reference(path)
        try:
            ref.delete()
            deleted_paths.append(path)
        except Exception as e:
            logger.warning("delete_user_data_for_quiz: delete %s failed: %s", path, e)

    needle = f"_quiz_{q[:8]}_"
    pattern_prefix = f"{uid}_quiz_{q[:8]}_"
    try:
        sessions_root = db.reference("sessions")
        shallow = sessions_root.get(shallow=True)
        if isinstance(shallow, dict):
            for sid in list(shallow.keys()):
                if not isinstance(sid, str) or needle not in sid:
                    continue
                p = f"sessions/{sid}"
                if sid.startswith(pattern_prefix):
                    try:
                        db.reference(p).delete()
                        deleted_paths.append(p)
                    except Exception as e:
                        logger.warning("delete_user_data_for_quiz: delete %s failed: %s", p, e)
                    continue
                try:
                    final = db.reference(f"{p}/final_submit").get()
                except Exception as e:
                    logger.warning("delete_user_data_for_quiz: read %s/final_submit failed: %s", p, e)
                    continue
                if isinstance(final, dict) and _session_final_submit_matches_user_quiz(final, uid, q):
                    try:
                        db.reference(p).delete()
                        deleted_paths.append(p)
                    except Exception as e:
                        logger.warning("delete_user_data_for_quiz: delete %s failed: %s", p, e)
    except Exception:
        logger.exception("delete_user_data_for_quiz: sessions scan failed")
        raise

    return {"deleted_paths": deleted_paths, "deleted_count": len(deleted_paths)}


# (pre|post|periodic, problem index (pre/post; None = a session without per-problem surveys) or
# periodic occurrence, qa_index)
SurveySlot = Tuple[str, Optional[int], int]


def _get_qa_pair_at(ss: Dict[str, Any], kind: str, occ: Optional[int], idx: int) -> Optional[Dict[str, Any]]:
    """Return the Q/A dict at a fixed slot (pre/post list index or periodic occurrence + index)."""
    if kind in ("pre", "post"):
        block = ss.get(kind) or {}
        by_problem = block.get("by_problem") or []
        if occ is None:  # sessions from before survey_history: their one (latest) survey
            pq = [] if by_problem else block.get("qa") or []
        else:  # occ = problem index
            pq = next((e.get("qa") or [] for e in by_problem if isinstance(e, dict) and e.get("problem_index") == occ), [])
        if isinstance(pq, list) and 0 <= idx < len(pq) and isinstance(pq[idx], dict):
            return pq[idx]
        return None
    if kind == "periodic":
        for o in (ss.get("periodic") or {}).get("occurrences") or []:
            if not isinstance(o, dict):
                continue
            raw_num = o.get("occurrence_number")
            if raw_num is None:
                raw_num = o.get("occurrence")
            try:
                ni = int(raw_num) if raw_num is not None else 0
            except (TypeError, ValueError):
                ni = 0
            if occ is not None and ni != occ:
                continue
            qa = o.get("qa") or []
            if isinstance(qa, list) and 0 <= idx < len(qa) and isinstance(qa[idx], dict):
                return qa[idx]
        return None
    return None


def _question_text_for_slot(per_session: List[Dict[str, Any]], kind: str, occ: Optional[int], idx: int) -> str:
    """Column title = survey question text from the first session that has it."""
    for row in per_session:
        ss = row.get("survey_summary")
        if not isinstance(ss, dict):
            continue
        pair = _get_qa_pair_at(ss, kind, occ, idx)
        if isinstance(pair, dict):
            q = str(pair.get("q") or "").strip()
            if q:
                return q
    if kind in ("pre", "post"):
        return f"{kind.title()}-survey (question {idx + 1})"
    return f"Periodic (occ. {occ}) (question {idx + 1})"


def _dedupe_csv_headers(headers: List[str]) -> List[str]:
    """If the same question text appears twice, suffix duplicate column names so CSV stays valid."""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for h in headers:
        base = (h or "").strip() or "(empty question)"
        if base not in seen:
            seen[base] = 0
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base} [{seen[base]}]")
    return out


def _survey_csv_layout(per_session: List[Dict[str, Any]]) -> Tuple[List[str], List[SurveySlot]]:
    """Survey columns: header string is the question; rows contain answers only."""
    # pre/post: problem index (None = session without per-problem surveys) -> most questions
    pp_max: Dict[str, Dict[Optional[int], int]] = {"pre": {}, "post": {}}
    occ_max: Dict[int, int] = {}

    for row in per_session:
        ss = row.get("survey_summary")
        if not isinstance(ss, dict):
            continue
        for kind, slots in pp_max.items():
            block = ss.get(kind) or {}
            by_problem = [e for e in block.get("by_problem") or [] if isinstance(e, dict)]
            entries = [(e.get("problem_index"), e.get("qa")) for e in by_problem] or [(None, block.get("qa"))]
            for pi, qa in entries:
                if isinstance(qa, list) and qa:
                    slots[pi] = max(slots.get(pi, 0), len(qa))
        per = ss.get("periodic") or {}
        _occs = per.get("occurrences")
        occs: List[Any] = _occs if isinstance(_occs, list) else []
        for occ in occs:
            if not isinstance(occ, dict):
                continue
            raw_num = occ.get("occurrence_number")
            if raw_num is None:
                raw_num = occ.get("occurrence")
            try:
                ni = int(raw_num) if raw_num is not None else 0
            except (TypeError, ValueError):
                ni = 0
            _occ_qa = occ.get("qa")
            qa: List[Any] = _occ_qa if isinstance(_occ_qa, list) else []
            occ_max[ni] = max(occ_max.get(ni, 0), len(qa))

    specs: List[SurveySlot] = []
    prefixed: set = set()  # specs whose header names the problem (more than one survey of that kind)
    for kind, slots in pp_max.items():
        keys = sorted(slots, key=lambda k: -1 if k is None else k)
        for pi in keys:
            for i in range(slots[pi]):
                specs.append((kind, pi, i))
                if pi is not None and len(keys) > 1:
                    prefixed.add(len(specs) - 1)
    for occ_key in sorted(occ_max.keys()):
        m = occ_max[occ_key]
        for i in range(m):
            specs.append(("periodic", occ_key, i))

    raw_headers = [(f"P{o + 1} {k} · " if n in prefixed and o is not None else "") + _question_text_for_slot(per_session, k, o, i)
                   for n, (k, o, i) in enumerate(specs)]
    headers = _dedupe_csv_headers(raw_headers)
    return headers, specs


def _survey_csv_answers_for_row(survey_summary: Any, specs: List[SurveySlot]) -> List[str]:
    """One cell per column: answer text only (question is in the header row)."""
    ss = survey_summary if isinstance(survey_summary, dict) else {}
    out: List[str] = []
    for kind, occ, idx in specs:
        pair = _get_qa_pair_at(ss, kind, occ, idx)
        if isinstance(pair, dict):
            out.append(str(pair.get("a") or "").strip())
        else:
            out.append("")
    return out


def _csv_float_cell(v: Any, default: float = 0.0) -> float:
    """Excel-friendly numeric cell: never NaN/Inf/blank for a measured value."""
    if v is None:
        return default
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


def _csv_int_cell(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def export_per_session_csv(problem_labels: List[str], per_session: List[Dict[str, Any]]) -> str:
    """Flatten ``per_session`` rows to CSV (UTF-8, Excel-friendly).

    Survey columns use the **question text as the header**; each row cell is the **answer** only.
    """
    n = len(problem_labels) if problem_labels else 0
    if n == 0 and per_session:
        for row in per_session:
            for key in (
                "per_problem_time_seconds",
                "per_problem_ai_generated",
                "completed_problem",
            ):
                li = row.get(key)
                if isinstance(li, list) and len(li) > n:
                    n = len(li)
    if n < 1:
        n = 1

    survey_headers, survey_specs = _survey_csv_layout(per_session)

    buf = io.StringIO()
    w = csv.writer(buf)
    header = [
        "user_id",
        "session_id",
        "quiz_id",
        "condition",
        "starting_time",
        "end_time",
        "completed_assignment",
        "total_time_seconds",
        "focus_time_seconds",
        "programming_language",
        "ai_generated_total",
        "ai_analyses_total",
        "accepted_total",
        "rejected_total",
        "avg_suggestion_time_seconds_total",
        "compiles_total",
        "tab_switch_total",
        "paste_attempt_total",
    ]
    for i in range(n):
        lab = f"P{i + 1}"
        header.extend(
            [
                f"{lab}_time_sec",
                f"{lab}_ai_gen",
                f"{lab}_accept",
                f"{lab}_reject",
                f"{lab}_avg_sug_s",
                f"{lab}_runs",
                f"{lab}_tabs",
                f"{lab}_paste",
                f"{lab}_done",
            ]
        )
    header.extend(survey_headers)
    w.writerow(header)

    def _at_done(li: Any, idx: int) -> str:
        if not isinstance(li, list) or idx >= len(li):
            return "N"
        v = li[idx]
        if v is True or v == 1 or str(v).lower() in ("true", "1", "yes", "y"):
            return "Y"
        return "N"

    for row in per_session:
        ca = row.get("completed_assignment")
        completed_cell = "Y" if (ca is True or ca == 1 or str(ca).lower() in ("true", "1", "yes")) else "N"
        out: List[Any] = [
            row.get("user_id") or "",
            row.get("session_id") or "",
            row.get("quiz_id") or "",
            row.get("condition") if row.get("condition") is not None else "",
            row.get("starting_time") or "",
            row.get("end_time") or "",
            completed_cell,
            _csv_float_cell(row.get("total_time_seconds"), 0.0),
            _csv_float_cell(row.get("focus_time_seconds"), 0.0),
            row.get("programming_language") or "",
            _csv_int_cell(row.get("ai_generated_total"), 0),
            _csv_int_cell(row.get("ai_analyses_total"), 0),
            _csv_int_cell(row.get("accepted_total"), 0),
            _csv_int_cell(row.get("rejected_total"), 0),
            _csv_float_cell(row.get("avg_suggestion_time_seconds_total"), 0.0),
            _csv_int_cell(row.get("compiles_total"), 0),
            _csv_int_cell(row.get("tab_switch_total"), 0),
            _csv_int_cell(row.get("paste_attempt_total"), 0),
        ]

        def _pv(key: str, idx: int) -> Any:
            li = row.get(key)
            if not isinstance(li, list) or idx >= len(li):
                return None
            return li[idx]

        for i in range(n):
            done_s = _at_done(row.get("completed_problem"), i)
            out.extend(
                [
                    _csv_float_cell(_pv("per_problem_time_seconds", i), 0.0),
                    _csv_int_cell(_pv("per_problem_ai_generated", i), 0),
                    _csv_int_cell(_pv("per_problem_accepted", i), 0),
                    _csv_int_cell(_pv("per_problem_rejected", i), 0),
                    _csv_float_cell(_pv("per_problem_avg_suggestion_time_seconds", i), 0.0),
                    _csv_int_cell(_pv("per_problem_compiles", i), 0),
                    _csv_int_cell(_pv("per_problem_tab_switches", i), 0),
                    _csv_int_cell(_pv("per_problem_paste_attempts", i), 0),
                    done_s,
                ]
            )
        out.extend(_survey_csv_answers_for_row(row.get("survey_summary"), survey_specs))
        w.writerow(out)

    return buf.getvalue()


def export_activity_log_csv(
    problem_tables: List[Dict[str, Any]],
    user_id: str,
    session_id: Optional[str],
    only_problem_index: Optional[int] = None,
) -> str:
    """CSV for one problem table or all tables (Excel-friendly; multiline fields quoted)."""
    if not isinstance(problem_tables, list):
        problem_tables = []

    buf = io.StringIO()
    w = csv.writer(buf)

    def _latency_cell(r: Dict[str, Any]) -> Any:
        lat = r.get("reaction_latency_seconds")
        if lat is None:
            return ""
        try:
            x = float(lat)
            if math.isnan(x) or math.isinf(x):
                return ""
            return round(x, 4)
        except (TypeError, ValueError):
            return ""

    if only_problem_index is not None:
        want = int(only_problem_index)
        tables = [t for t in problem_tables if int(t.get("problem_index", -1)) == want]
        if not tables:
            return ""
        header = [
            "user_id",
            "session_id",
            "row_in_problem",
            "suggestion_time",
            "code_suggestion",
            "accept_reject",
            "reaction_latency_seconds",
            "interpretation",
            "code_snapshot",
        ]
        w.writerow(header)
        for r in tables[0].get("rows") or []:
            w.writerow(
                [
                    user_id,
                    session_id or "",
                    r.get("index_in_problem") or r.get("index") or "",
                    r.get("timestamp") or "",
                    r.get("suggestion") or "",
                    r.get("accept_reject") or "",
                    _latency_cell(r),
                    r.get("interpretation") or "",
                    r.get("code_snapshot") or "",
                ]
            )
    else:
        header = [
            "user_id",
            "session_id",
            "problem_index",
            "problem_label",
            "row_in_problem",
            "suggestion_time",
            "code_suggestion",
            "accept_reject",
            "reaction_latency_seconds",
            "interpretation",
            "code_snapshot",
        ]
        w.writerow(header)
        for t in problem_tables:
            pi = t.get("problem_index", "")
            lab = t.get("label") or ""
            for r in t.get("rows") or []:
                w.writerow(
                    [
                        user_id,
                        session_id or "",
                        pi,
                        lab,
                        r.get("index_in_problem") or r.get("index") or "",
                        r.get("timestamp") or "",
                        r.get("suggestion") or "",
                        r.get("accept_reject") or "",
                        _latency_cell(r),
                        r.get("interpretation") or "",
                        r.get("code_snapshot") or "",
                    ]
                )

    return buf.getvalue()


# ─── Correct / buggy pairs (bug_injections/{session_id}/{pair_id}, written by main.py) ───

def _pair_outcome(rec: Dict[str, Any], bug_oid: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """(shown, accepted variant or None) for one pair record, from its events. With `bug_oid` (one
    buggy option of a pair with several), accepting another buggy option counts as "other_buggy"."""
    variants = {o.get("oid"): o.get("variant") for o in _firebase_to_list(rec.get("options")) if isinstance(o, dict)}
    if bug_oid:
        variants = {k: ("other_buggy" if v == "buggy" and k != bug_oid else v) for k, v in variants.items()}
    events = rec.get("events")
    events = list(events.values()) if isinstance(events, dict) else _firebase_to_list(events)
    shown, accepted = False, None
    for ev in sorted((e for e in events if isinstance(e, dict)), key=lambda e: str(e.get("at") or "")):
        if ev.get("event") == "shown":
            shown = True
        elif ev.get("event") == "accepted" and ev.get("oid") in variants:
            shown, accepted = True, variants[ev["oid"]]
    return shown, accepted


def bug_pair_stats(db, quiz_id: str, bug_skills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per bug type and condition: pairs generated, shown, and whether students accepted the buggy or
    the correct option. Selected bug types with no pairs yet are listed with zeros. Also the time each
    suggestion took to generate (first request until its options were ready), per condition and problem."""
    q = str(quiz_id or "").strip()
    needle = f"_quiz_{q[:8]}_"
    keys = db.reference("bug_injections").get(shallow=True) or {}
    # (pair record, one planted bug, that bug's option id or None for the older one-bug records)
    records: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]] = []
    # condition -> {"total": [seconds], "per_problem": {problem index: [seconds]}}
    gen_times: Dict[str, Dict[str, Any]] = {}
    for sid in keys if isinstance(keys, dict) else []:
        if needle not in sid:
            continue
        for rec in (db.reference(f"bug_injections/{sid}").get() or {}).values():
            if not isinstance(rec, dict) or rec.get("quiz_id", q) != q:
                continue
            ms = _safe_float(rec.get("generation_ms"))
            if ms is not None and ms >= 0:
                cell = gen_times.setdefault(str(rec.get("condition") or "unknown"), {"total": [], "per_problem": {}})
                cell["total"].append(round(ms / 1000, 2))
                pi = _int_pi(rec.get("problem_index"))
                if pi is not None:
                    cell["per_problem"].setdefault(str(pi), []).append(round(ms / 1000, 2))
            if rec.get("requested_bug"):  # one buggy option, before quiz.bug_options existed
                records.append((rec, rec, None))
            for bug in _firebase_to_list(rec.get("bugs")):
                if isinstance(bug, dict) and bug.get("requested_bug"):
                    records.append((rec, bug, bug.get("oid")))

    def blank(type_id: str, name: str, category: str) -> Dict[str, Any]:
        return {"id": type_id, "name": name, "category": category, "generated": 0, "shown": 0,
                "accepted_buggy": 0, "accepted_correct": 0, "classifier_agrees": 0, "by_condition": {}}

    by_type: Dict[str, Dict[str, Any]] = {
        s["id"]: blank(s["id"], s["name"], s.get("category", "")) for s in bug_skills if s.get("enabled")
    }
    for rec, bug, bug_oid in records:
        req = bug["requested_bug"] if isinstance(bug["requested_bug"], dict) else {}
        tid = str(req.get("id") or "unknown")
        row = by_type.setdefault(tid, blank(tid, str(req.get("name") or tid), str(req.get("category") or "")))
        cond = str(rec.get("condition") or "unknown")
        cell = row["by_condition"].setdefault(cond, {"generated": 0, "accepted_buggy": 0, "accepted_correct": 0})
        shown, accepted = _pair_outcome(rec, bug_oid)
        row["generated"] += 1
        cell["generated"] += 1
        row["shown"] += int(shown)
        if accepted in ("buggy", "correct"):
            row[f"accepted_{accepted}"] += 1
            cell[f"accepted_{accepted}"] += 1
        cls = bug.get("classification") if isinstance(bug.get("classification"), dict) else {}
        row["classifier_agrees"] += int(cls.get("category") == tid)

    rows = sorted(by_type.values(), key=lambda r: (-r["generated"], r["name"].lower()))
    totals = {k: sum(r[k] for r in rows) for k in ("generated", "shown", "accepted_buggy", "accepted_correct")}
    conditions = sorted({c for r in rows for c in r["by_condition"]})
    return sanitize_for_json({"types": rows, "totals": totals, "conditions": conditions,
                              "generation_seconds": gen_times})
