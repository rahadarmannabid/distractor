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

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


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
    if not isinstance(al, dict):
        return []
    return _firebase_to_list(al.get("entries"))


def _session_meta(user_data: Dict[str, Any]) -> Dict[str, Any]:
    sm = user_data.get("session_metadata")
    return sm if isinstance(sm, dict) else {}


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


def _summarize_survey_payload(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize pre/post/periodic survey Q&A for instructor JSON and UI.
    Reads top-level Firebase keys: pre_survey, post_survey, periodic_surveys.
    """
    pre = user_data.get("pre_survey")
    post = user_data.get("post_survey")
    pre_qa = _qa_from_pre_post_block(pre) if pre else []
    post_qa = _qa_from_pre_post_block(post) if post else []

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
        out["pre"] = {"qa": pre_qa}
    if post_qa:
        out["post"] = {"qa": post_qa}
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

    user_id = str(meta.get("user_id") or "unknown")
    session_id = str(meta.get("session_id") or "")

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
        at = str(e.get("activity_type") or "").strip().lower()
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
            try:
                tss = float(sc.get("time_since_start"))
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

    # Fallback for accept counts: use session_metadata.suggestion_clicked when
    # activity-log scanning found zero accepts.
    if acc_total == 0:
        sc_val = _int_pi(meta.get("suggestion_clicked"))
        if sc_val and sc_val > 0:
            acc_total = sc_val
            if num_problems == 1:
                acc_by_pi[0] = sc_val

    # Fallback for run/compile counts: use session_metadata.compiler_clicked when
    # activity-log scanning found zero code_execution events.
    if run_total == 0:
        cc_val = _int_pi(meta.get("compiler_clicked"))
        if cc_val and cc_val > 0:
            run_total = cc_val
            if num_problems == 1:
                run_by_pi[0] = cc_val

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

    return {
        "user_id": user_id,
        "session_id": session_id,
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


def aggregate_quiz_analytics(
    sessions: List[Dict[str, Any]],
    quiz_id: str,
    problem_labels: List[str],
) -> Dict[str, Any]:
    """
    sessions: list of user_data dicts from Firebase (session_metadata + activity_log, ...).
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
            derived.append(m)
            raw_matched.append(ud)

    sessions_considered = len(derived)
    completed_count = sum(1 for d in derived if d["completed_assignment"])

    completion_by_problem = [0] * n
    for d in derived:
        cp = d.get("completed_problem") or []
        for i in range(min(n, len(cp))):
            if cp[i]:
                completion_by_problem[i] += 1

    # Median + "Total" time boxplot: same metric as per-session Time column (focus when available).
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

    any_pi = any(d.get("has_problem_index_on_activities") for d in derived)

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

    def _median(xs: List[float]) -> Optional[float]:
        if not xs:
            return None
        s = sorted(xs)
        m = len(s) // 2
        if len(s) % 2:
            return s[m]
        return (s[m - 1] + s[m]) / 2.0

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
                "user_id": d.get("user_id") or "unknown",
                "session_id": d.get("session_id") or "",
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
        "completion_by_problem": completion_by_problem,
        "per_problem_completion": per_problem_completion,
        "per_problem_activity_breakdown": any_pi,
        "per_session": per_session_rows,
        "data_health": data_health,
        "summary": {
            # Same duration rule as per-session Time column: focus_time_seconds when present, else wall clock.
            "median_session_time_seconds": _median(total_times),
            "median_ai_generations": _median([float(d["ai_generations_total"]) for d in derived]),
            "median_ai_analyses": _median([float(d.get("ai_analyses_total") or 0.0) for d in derived]),
            "median_accepts": _median([float(d["accepts_total"]) for d in derived]),
            "median_rejects": _median([float(d["rejects_total"]) for d in derived]),
            "median_runs": _median([float(d.get("runs_total") or 0.0) for d in derived]),
        },
        "boxplot": {
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
        },
    }
    return sanitize_for_json(payload)  # type: ignore[return-value]


def collect_quiz_sessions_from_firebase(db, quiz_id: str) -> List[Dict[str, Any]]:
    """Load user payloads from condition_1..3 / quiz_{prefix} / * and mirror sessions/final_submit."""
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

    for cond in (1, 2, 3):
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
    try:
        sessions_root = db.reference("sessions")
        shallow = sessions_root.get(shallow=True)
        if isinstance(shallow, dict):
            for sid in shallow.keys():
                if not isinstance(sid, str) or needle not in sid:
                    continue
                final = db.reference(f"sessions/{sid}/final_submit").get()
                if not isinstance(final, dict):
                    continue
                ud = final.get("user_data")
                if isinstance(ud, dict):
                    _append_payload(ud)
    except Exception as e:
        logger.warning("sessions/final_submit mirror scan failed: %s", e)

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
    - ``condition_{1,2,3}/quiz_{quiz_id[:8]}/{user_id}`` (always attempted; RTDB delete is idempotent)
    - ``sessions/{session_id}`` when session id matches ``{user_id}_quiz_{quiz_id[:8]}_*``, or when
      ``sessions/.../final_submit`` mirrors this user + quiz (covers legacy / odd session ids)
    """
    uid = validate_analytics_user_id(user_id)
    q = str(quiz_id or "").strip()
    if len(q) < 8:
        raise ValueError("Invalid quiz id")
    prefix = f"quiz_{q[:8]}"
    deleted_paths: List[str] = []

    for cond in (1, 2, 3):
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
    except Exception as e:
        logger.exception("delete_user_data_for_quiz: sessions scan failed")
        raise

    return {"deleted_paths": deleted_paths, "deleted_count": len(deleted_paths)}


SurveySlot = Tuple[str, Optional[int], int]  # (pre|post|periodic, occurrence or None, qa_index)


def _get_qa_pair_at(ss: Dict[str, Any], kind: str, occ: Optional[int], idx: int) -> Optional[Dict[str, Any]]:
    """Return the Q/A dict at a fixed slot (pre/post list index or periodic occurrence + index)."""
    if kind == "pre":
        pq = (ss.get("pre") or {}).get("qa") or []
        if isinstance(pq, list) and 0 <= idx < len(pq) and isinstance(pq[idx], dict):
            return pq[idx]
        return None
    if kind == "post":
        pq = (ss.get("post") or {}).get("qa") or []
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
    if kind == "pre":
        return f"Pre-survey (question {idx + 1})"
    if kind == "post":
        return f"Post-survey (question {idx + 1})"
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
    max_pre = 0
    max_post = 0
    occ_max: Dict[int, int] = {}

    for row in per_session:
        ss = row.get("survey_summary")
        if not isinstance(ss, dict):
            continue
        pre = ss.get("pre") or {}
        pq = pre.get("qa") if isinstance(pre.get("qa"), list) else []
        max_pre = max(max_pre, len(pq))
        post = ss.get("post") or {}
        poq = post.get("qa") if isinstance(post.get("qa"), list) else []
        max_post = max(max_post, len(poq))
        per = ss.get("periodic") or {}
        occs = per.get("occurrences") if isinstance(per.get("occurrences"), list) else []
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
            qa = occ.get("qa") if isinstance(occ.get("qa"), list) else []
            occ_max[ni] = max(occ_max.get(ni, 0), len(qa))

    specs: List[SurveySlot] = []
    for i in range(max_pre):
        specs.append(("pre", None, i))
    for i in range(max_post):
        specs.append(("post", None, i))
    for occ_key in sorted(occ_max.keys()):
        m = occ_max[occ_key]
        for i in range(m):
            specs.append(("periodic", occ_key, i))

    raw_headers = [_question_text_for_slot(per_session, k, o, i) for (k, o, i) in specs]
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
        "completed_assignment",
        "total_time_seconds",
        "focus_time_seconds",
        "programming_language",
        "ai_generated_total",
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

    def _at(li: Any, idx: int) -> Any:
        if not isinstance(li, list) or idx >= len(li):
            return ""
        return li[idx] if li[idx] is not None else ""

    for row in per_session:
        out: List[Any] = [
            row.get("user_id"),
            row.get("session_id"),
            row.get("completed_assignment"),
            row.get("total_time_seconds"),
            row.get("focus_time_seconds"),
            row.get("programming_language"),
            row.get("ai_generated_total"),
            row.get("accepted_total"),
            row.get("rejected_total"),
            row.get("avg_suggestion_time_seconds_total"),
            row.get("compiles_total"),
            row.get("tab_switch_total"),
            row.get("paste_attempt_total"),
        ]
        for i in range(n):
            done = _at(row.get("completed_problem"), i)
            done_s = "Y" if done else "N"
            out.extend(
                [
                    _at(row.get("per_problem_time_seconds"), i),
                    _at(row.get("per_problem_ai_generated"), i),
                    _at(row.get("per_problem_accepted"), i),
                    _at(row.get("per_problem_rejected"), i),
                    _at(row.get("per_problem_avg_suggestion_time_seconds"), i),
                    _at(row.get("per_problem_compiles"), i),
                    _at(row.get("per_problem_tab_switches"), i),
                    _at(row.get("per_problem_paste_attempts"), i),
                    done_s,
                ]
            )
        out.extend(_survey_csv_answers_for_row(row.get("survey_summary"), survey_specs))
        w.writerow(out)

    return buf.getvalue()
