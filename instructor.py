"""Instructor router — OTP auth, dashboard, quiz CRUD, share-link generation.

Mounted as a sub-router in main.py at prefix /instructor.
"""

import shutil
import copy

from fastapi import APIRouter, Request, HTTPException, Body, File, UploadFile, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from typing import List, Optional
import json
import logging
import os
import re
import secrets
import uuid
from urllib.parse import quote
from pathlib import Path

from firebase_utils import fb_dict
from models import QuizCreate, QuizUpdate, QuizJoinRequest
from quiz_analytics import (
    aggregate_quiz_analytics,
    build_user_ai_activity_log,
    collect_quiz_sessions_from_firebase,
    delete_user_data_for_quiz,
    export_activity_log_csv,
    export_per_session_csv,
    find_user_session_payload_for_quiz,
    validate_analytics_user_id,
)

from otp import (
    generate_otp,
    verify_otp_value,
    store_otp,
    get_otp as get_otp_record,
    increment_otp_attempts,
    delete_otp,
    get_otp_provider,
    is_email_domain_allowed,
    sanitize_key,
    OTP_EXPIRY_MINUTES,
    OTP_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instructor", tags=["instructor"])
templates = Jinja2Templates(directory="templates")

# Analytics responses must stay fresh after Firebase deletes (avoid stale chart data in the browser).
_ANALYTICS_JSON_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

# Instructor quiz builder: default survey question banks (editable JSON on disk)
_DEFAULT_SURVEY_TEMPLATE_FILES = {
    "pre": Path("surveys/instructor_default_pre_survey.json"),
    "post": Path("surveys/instructor_default_post_survey.json"),
    "recurrent": Path("surveys/instructor_default_recurrent_survey.json"),
}

# Per-quiz survey question images (uploaded by instructor); served via GET /quiz-images/{asset_id}/{filename}
QUIZ_ASSETS_DIR = Path("surveys") / "quiz_assets"
ALLOWED_QUIZ_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}


def _sanitize_quiz_asset_filename(name: str) -> str:
    base = Path(name).name
    stem = Path(base).stem
    suf = Path(base).suffix.lower()
    if suf not in ALLOWED_QUIZ_IMAGE_EXT:
        suf = ".png"
    clean = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)[:160] or "image"
    return clean + suf


def _rewrite_quiz_image_ids(obj: object, old_id: str, new_id: str) -> None:
    """Rewrite image.src from old_id/filename → new_id/filename everywhere in nested structures."""
    prefix = old_id + "/"
    if isinstance(obj, dict):
        im = obj.get("image")
        if isinstance(im, dict) and isinstance(im.get("src"), str):
            s = im["src"]
            if s.startswith(prefix):
                im["src"] = new_id + "/" + s[len(prefix) :]
        for v in obj.values():
            _rewrite_quiz_image_ids(v, old_id, new_id)
    elif isinstance(obj, list):
        for x in obj:
            _rewrite_quiz_image_ids(x, old_id, new_id)


@router.get("/default-survey-templates/{which}")
async def get_default_survey_templates(which: str):
    """Return default pre/post/recurrent question arrays from `surveys/instructor_default_*.json`."""
    path = _DEFAULT_SURVEY_TEMPLATE_FILES.get(which)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown or missing survey template")
    with open(path, encoding="utf-8") as f:
        return JSONResponse(json.load(f))


@router.post("/upload-survey-image")
async def upload_survey_image(
    request: Request,
    file: UploadFile = File(...),
    draft_upload_id: Optional[str] = Form(None),
    quiz_id: Optional[str] = Form(None),
):
    """Store image under surveys/quiz_assets/{draft_id|quiz_id}/. Returns image.src like \"uuid/filename.png\" for question JSON."""
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    did = (draft_upload_id or "").strip()
    qid = (quiz_id or "").strip()
    if bool(did) == bool(qid):
        return JSONResponse(
            {"success": False, "error": "Send exactly one of draft_upload_id or quiz_id"},
            status_code=400,
        )

    asset_id = qid if qid else did
    if qid:
        fb = _get_firebase()
        if fb:
            from firebase_admin import db
            quiz = fb_dict(db.reference(f"quizzes/{qid}").get())
            if not quiz or quiz.get("created_by") != email:
                return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    raw_name = file.filename or "image.png"
    safe_name = _sanitize_quiz_asset_filename(raw_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_QUIZ_IMAGE_EXT:
        return JSONResponse({"success": False, "error": f"File type not allowed ({ext})"}, status_code=400)

    dest_dir = QUIZ_ASSETS_DIR / asset_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name
    if dest_path.exists():
        stem = Path(safe_name).stem
        safe_name = f"{stem}_{secrets.token_hex(4)}{ext}"
        dest_path = dest_dir / safe_name

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        return JSONResponse({"success": False, "error": "File too large (max 5 MB)"}, status_code=400)
    dest_path.write_bytes(contents)

    src = f"{asset_id}/{safe_name}"
    logger.info("Instructor uploaded survey image %s -> %s", email, src)
    return JSONResponse({"success": True, "src": src})


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_firebase():
    """Lazy import to avoid circular dependency with main.py."""
    from main import firebase_db
    return firebase_db


def _require_instructor(request: Request):
    """Return instructor email from session or None."""
    return request.session.get("instructor_email") if request.session.get("is_instructor") else None


def _generate_access_code(length: int = 8) -> str:
    """Generate a high-entropy alphanumeric access code."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _normalize_quiz_data(quiz_data: dict) -> dict:
    """Backward-compat: wrap legacy single-problem/single-condition into list format."""
    if "problems" not in quiz_data and "problem_statement" in quiz_data:
        quiz_data["problems"] = [{
            "title": "Problem 1",
            "problem_statement": quiz_data.pop("problem_statement", ""),
            "constraints": quiz_data.pop("constraints", ""),
            "test_cases": "",
            "starter_code": quiz_data.pop("starter_code", {}),
        }]
    if "conditions" not in quiz_data and "condition" in quiz_data:
        quiz_data["conditions"] = [int(quiz_data.pop("condition", 1))]
    elif "conditions" not in quiz_data:
        quiz_data["conditions"] = [1]

    # Migrate quiz-level surveys into per-problem surveys if missing
    if "problems" in quiz_data:
        quiz_pre = quiz_data.get("pre_survey", [])
        quiz_post = quiz_data.get("post_survey", [])
        quiz_rec = quiz_data.get("recurrent_survey", [])
        quiz_rec_interval = quiz_data.get("recurrent_survey_interval", 4)
        quiz_rec_start = quiz_data.get("recurrent_survey_start", 5)
        for i, prob in enumerate(quiz_data["problems"]):
            if not (prob.get("title") or "").strip():
                prob["title"] = f"Problem {i + 1}"
            if "test_cases" not in prob:
                prob["test_cases"] = ""
            if not prob.get("_has_surveys"):
                prob["_has_surveys"] = True
                prob["pre_survey_enabled"] = bool(quiz_pre)
                prob["pre_survey"] = quiz_pre
                prob["post_survey_enabled"] = bool(quiz_post)
                prob["post_survey"] = quiz_post
                prob["recurrent_survey_enabled"] = bool(quiz_rec)
                prob["recurrent_survey"] = quiz_rec
                prob["recurrent_survey_interval"] = quiz_rec_interval
                prob["recurrent_survey_start"] = quiz_rec_start

    return quiz_data


# ─── OTP Auth Routes ────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse("instructor_login.html", {
        "request": request,
        "error": error,
    })


@router.post("/send-otp", response_class=HTMLResponse)
async def send_otp(request: Request):
    form = await request.form()
    email = str(form.get("email") or "").lower().strip()

    if "@" not in email:
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "Invalid email format.",
        })

    if not is_email_domain_allowed(email):
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "Email domain not allowed. Please use an approved domain.",
        })

    otp_plain, otp_hash, salt = generate_otp()
    expiry = datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    store_otp(_get_firebase(), email, otp_hash, salt, expiry)

    provider = get_otp_provider()
    sent = provider.send(email, otp_plain)
    if not sent:
        logger.error(f"OTP send failed for {email}")
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "Failed to send OTP email. Please check SMTP settings and try again.",
        })
    logger.info(f"OTP sent to {email}")

    return templates.TemplateResponse("instructor_login.html", {
        "request": request,
        "otp_sent": True,
        "email": email,
        "success": f"OTP sent to {email}. Valid for {OTP_EXPIRY_MINUTES} minutes.",
    })


@router.post("/verify-otp", response_class=HTMLResponse)
async def verify_otp(request: Request):
    form = await request.form()
    email = str(form.get("email") or "").lower().strip()
    otp_input = str(form.get("otp") or "").strip()

    stored = get_otp_record(_get_firebase(), email)

    if not stored:
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "OTP expired or not found. Please request a new one.",
        })

    if datetime.utcnow() > stored["expires"]:
        delete_otp(_get_firebase(), email)
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "OTP expired. Please request a new one.",
        })

    if stored["attempts"] >= OTP_MAX_ATTEMPTS:
        delete_otp(_get_firebase(), email)
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "error": "Too many failed attempts. Please request a new OTP.",
        })

    if verify_otp_value(otp_input, stored["otp_hash"], stored["salt"]):
        delete_otp(_get_firebase(), email)
        request.session["instructor_email"] = email
        request.session["is_instructor"] = True

        # Ensure instructor record exists in Firebase
        fb = _get_firebase()
        if fb:
            from firebase_admin import db
            sanitized = sanitize_key(email)
            ref = db.reference(f"instructors/{sanitized}")
            existing = ref.get()
            if not existing:
                ref.set({"email": email, "created_at": datetime.utcnow().isoformat()})

        logger.info(f"Instructor login success: {email}")
        return RedirectResponse(url="/instructor/dashboard", status_code=303)
    else:
        new_attempts = stored["attempts"] + 1
        increment_otp_attempts(_get_firebase(), email, new_attempts)
        remaining = OTP_MAX_ATTEMPTS - new_attempts
        return templates.TemplateResponse("instructor_login.html", {
            "request": request,
            "otp_sent": True,
            "email": email,
            "error": f"Invalid OTP. {remaining} attempts remaining.",
        })


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/instructor/login", status_code=303)


# ─── Dashboard ──────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    email = _require_instructor(request)
    if not email:
        return RedirectResponse(url="/instructor/login", status_code=303)

    quizzes = []
    fb = _get_firebase()
    if fb:
        from firebase_admin import db
        sanitized = sanitize_key(email)
        quiz_ids = fb_dict(db.reference(f"instructors/{sanitized}/quizzes").get()) or {}
        if quiz_ids:
            all_quizzes = fb_dict(db.reference("quizzes").get()) or {}
            for qid in quiz_ids:
                qdata = all_quizzes.get(qid)
                if qdata:
                    qdata["id"] = qid
                    quizzes.append(qdata)
        quizzes.sort(key=lambda q: q.get("created_at", ""), reverse=True)

        from main import get_active_quiz_session_counts

        for q in quizzes:
            qid = q.get("id")
            if qid:
                q["active_now"] = int(get_active_quiz_session_counts(qid).get("total_active") or 0)
            else:
                q["active_now"] = 0

    return templates.TemplateResponse("instructor_dashboard.html", {
        "request": request,
        "instructor_email": email,
        "quizzes": quizzes,
    })


# ─── Quiz CRUD ──────────────────────────────────────────────────────────────

@router.get("/create-quiz", response_class=HTMLResponse)
async def create_quiz_page(request: Request):
    email = _require_instructor(request)
    if not email:
        return RedirectResponse(url="/instructor/login", status_code=303)
    return templates.TemplateResponse("instructor_quiz_form.html", {
        "request": request,
        "instructor_email": email,
        "quiz": None,
        "edit_mode": False,
        "draft_upload_id": str(uuid.uuid4()),
    })


@router.post("/save-quiz")
async def save_quiz(request: Request, payload: dict = Body(...)):
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    # Derive title for internal use: teaser, first problem title, or "Quiz"
    teaser = (payload.get("teaser") or "").strip()
    problems = payload.get("problems", [])
    first_prob_title = (problems[0].get("title") or "").strip() if problems else ""
    title = teaser or first_prob_title or "Quiz"

    # Validate problems list (at least one required)
    if not problems or not isinstance(problems, list):
        return JSONResponse({"success": False, "error": "At least one problem is required."}, status_code=400)
    problems = copy.deepcopy(problems)
    for i, prob in enumerate(problems):
        if not (prob.get("title") or "").strip():
            return JSONResponse({"success": False, "error": f"Problem {i+1} requires a title."}, status_code=400)
        if not (prob.get("problem_statement") or "").strip():
            return JSONResponse({"success": False, "error": f"Problem {i+1} requires a problem statement."}, status_code=400)

    # Validate conditions (list, each must be 1, 2, or 3)
    conditions = payload.get("conditions", [1])
    if not conditions or not isinstance(conditions, list):
        return JSONResponse({"success": False, "error": "At least one condition is required."}, status_code=400)
    for c in conditions:
        if int(c) not in (1, 2, 3):
            return JSONResponse({"success": False, "error": "Each condition must be 1, 2, or 3."}, status_code=400)
    conditions = [int(c) for c in conditions]

    quiz_id = str(uuid.uuid4())
    access_code = _generate_access_code()

    draft_id = (payload.get("draft_upload_id") or "").strip()
    if draft_id:
        src_dir = QUIZ_ASSETS_DIR / draft_id
        dst_dir = QUIZ_ASSETS_DIR / quiz_id
        if src_dir.exists() and src_dir.is_dir():
            if dst_dir.exists():
                shutil.rmtree(dst_dir, ignore_errors=True)
            shutil.move(str(src_dir), str(dst_dir))
        _rewrite_quiz_image_ids(problems, draft_id, quiz_id)

    quiz_data = {
        "id": quiz_id,
        "title": title,
        "description": "",
        "teaser": teaser,
        "problems": problems,
        "allowed_languages": payload.get("allowed_languages", ["python", "javascript", "c"]),
        "time_limit_minutes": int(payload.get("time_limit_minutes", 0)),
        "conditions": conditions,
        "recurrent_survey": payload.get("recurrent_survey", []),
        "recurrent_survey_interval": int(payload.get("recurrent_survey_interval", 4)),
        "recurrent_survey_start": int(payload.get("recurrent_survey_start", 5)),
        "pre_survey": payload.get("pre_survey", []),
        "post_survey": payload.get("post_survey", []),
        "published": payload.get("published", False),
        "availability_start": payload.get("availability_start") or None,
        "availability_end": payload.get("availability_end") or None,
        "paste_allowed": payload.get("paste_allowed", True),
        "tab_tracking_enabled": payload.get("tab_tracking_enabled", True),
        "access_code": access_code,
        "created_by": email,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    fb = _get_firebase()
    if fb:
        from firebase_admin import db
        db.reference(f"quizzes/{quiz_id}").set(quiz_data)
        db.reference(f"quiz_access/{access_code}").set({
            "quiz_id": quiz_id,
            "created_at": datetime.utcnow().isoformat(),
        })
        db.reference(f"instructors/{sanitize_key(email)}/quizzes/{quiz_id}").set(True)

    return JSONResponse({"success": True, "quiz_id": quiz_id, "access_code": access_code})


@router.get("/edit-quiz/{quiz_id}", response_class=HTMLResponse)
async def edit_quiz_page(request: Request, quiz_id: str):
    email = _require_instructor(request)
    if not email:
        return RedirectResponse(url="/instructor/login", status_code=303)

    fb = _get_firebase()
    quiz = None
    if fb:
        from firebase_admin import db
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.get("created_by") != email:
        raise HTTPException(status_code=403, detail="Not authorised to edit this quiz")

    quiz["id"] = quiz_id
    # Normalize legacy quizzes (single problem/condition) into new list format
    quiz = _normalize_quiz_data(quiz)
    return templates.TemplateResponse("instructor_quiz_form.html", {
        "request": request,
        "instructor_email": email,
        "quiz": quiz,
        "edit_mode": True,
        "draft_upload_id": None,
    })


@router.post("/update-quiz/{quiz_id}")
async def update_quiz(request: Request, quiz_id: str, payload: dict = Body(...)):
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"success": False, "error": "Database unavailable"}, status_code=503)

    from firebase_admin import db
    existing = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not existing:
        return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
    if existing.get("created_by") != email:
        return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    # Validate conditions if provided
    conditions = payload.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, list) or not conditions:
            return JSONResponse({"success": False, "error": "At least one condition is required."}, status_code=400)
        for c in conditions:
            if int(c) not in (1, 2, 3):
                return JSONResponse({"success": False, "error": "Each condition must be 1, 2, or 3."}, status_code=400)
        payload["conditions"] = [int(c) for c in conditions]

    # Validate problems if provided
    problems = payload.get("problems")
    if problems is not None:
        if not isinstance(problems, list) or not problems:
            return JSONResponse({"success": False, "error": "At least one problem is required."}, status_code=400)
        for i, prob in enumerate(problems):
            if not (prob.get("title") or "").strip():
                return JSONResponse({"success": False, "error": f"Problem {i+1} requires a title."}, status_code=400)
            if not (prob.get("problem_statement") or "").strip():
                return JSONResponse({"success": False, "error": f"Problem {i+1} requires a problem statement."}, status_code=400)

    updatable_fields = [
        "teaser", "problems",
        "allowed_languages", "time_limit_minutes", "conditions",
        "recurrent_survey", "recurrent_survey_interval", "recurrent_survey_start",
        "pre_survey", "post_survey",
        "published", "availability_start", "availability_end",
        "paste_allowed", "tab_tracking_enabled",
    ]
    updates = {k: payload[k] for k in updatable_fields if k in payload}
    # Derive title from teaser or first problem
    teaser = (payload.get("teaser") or "").strip()
    problems = payload.get("problems") or existing.get("problems", [])
    first_title = (problems[0].get("title") or "").strip() if problems else ""
    updates["title"] = teaser or first_title or "Quiz"
    updates["description"] = ""
    updates["updated_at"] = datetime.utcnow().isoformat()

    db.reference(f"quizzes/{quiz_id}").update(updates)
    return JSONResponse({"success": True, "quiz_id": quiz_id})


@router.delete("/delete-quiz/{quiz_id}")
async def delete_quiz(request: Request, quiz_id: str):
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"success": False, "error": "Database unavailable"}, status_code=503)

    from firebase_admin import db
    existing = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not existing:
        return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
    if existing.get("created_by") != email:
        return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    access_code = existing.get("access_code", "")
    db.reference(f"quizzes/{quiz_id}").delete()
    if access_code:
        db.reference(f"quiz_access/{access_code}").delete()
    db.reference(f"instructors/{sanitize_key(email)}/quizzes/{quiz_id}").delete()

    qdir = QUIZ_ASSETS_DIR / quiz_id
    if qdir.exists() and qdir.is_dir():
        shutil.rmtree(qdir, ignore_errors=True)

    return JSONResponse({"success": True})


@router.post("/toggle-publish/{quiz_id}")
async def toggle_publish(request: Request, quiz_id: str):
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"success": False, "error": "Database unavailable"}, status_code=503)

    from firebase_admin import db
    existing = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not existing:
        return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
    if existing.get("created_by") != email:
        return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    new_status = not existing.get("published", False)
    db.reference(f"quizzes/{quiz_id}/published").set(new_status)
    return JSONResponse({"success": True, "published": new_status})


def _safe_quiz_export_slug(quiz_id: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]", "_", (quiz_id or "").strip())[:100]
    return s or "quiz"


def _quiz_problem_labels(quiz: dict) -> List[str]:
    probs = quiz.get("problems") or []
    labels: List[str] = []
    for i, p in enumerate(probs):
        t = (p.get("title") or "").strip() if isinstance(p, dict) else ""
        labels.append(t or f"Problem {i + 1}")
    return labels or ["Problem 1"]


@router.get("/quiz/{quiz_id}/analytics", response_class=HTMLResponse)
async def quiz_analytics_page(request: Request, quiz_id: str):
    email = _require_instructor(request)
    if not email:
        return RedirectResponse(url="/instructor/login", status_code=303)

    fb = _get_firebase()
    if not fb:
        return templates.TemplateResponse(
            "instructor_quiz_analytics.html",
            {
                "request": request,
                "instructor_email": email,
                "quiz_id": quiz_id,
                "quiz_teaser": "",
                "problem_labels": [],
                "firebase_available": False,
            },
        )

    from firebase_admin import db

    quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.get("created_by") != email:
        raise HTTPException(status_code=403, detail="Not authorised to view this quiz")

    quiz = _normalize_quiz_data(quiz)
    labels = _quiz_problem_labels(quiz)
    return templates.TemplateResponse(
        "instructor_quiz_analytics.html",
        {
            "request": request,
            "instructor_email": email,
            "quiz_id": quiz_id,
            "quiz_teaser": (quiz.get("teaser") or quiz.get("title") or "").strip(),
            "problem_labels": labels,
            "firebase_available": True,
        },
    )


@router.get("/quiz/{quiz_id}/activity-log", response_class=HTMLResponse)
async def quiz_activity_log_page(
    request: Request,
    quiz_id: str,
    user_id: str = Query(..., description="Student user id (Firebase)"),
):
    """Per-user AI suggestion log: timestamps, suggestions, accept/reject, code snapshots."""
    email = _require_instructor(request)
    if not email:
        return RedirectResponse(url="/instructor/login", status_code=303)

    try:
        uid = validate_analytics_user_id(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user id")

    uid_q = quote(uid, safe="")

    fb = _get_firebase()
    if not fb:
        return templates.TemplateResponse(
            "instructor_activity_log.html",
            {
                "request": request,
                "instructor_email": email,
                "quiz_id": quiz_id,
                "quiz_teaser": "",
                "user_id": uid,
                "user_id_query": uid_q,
                "programming_language": "",
                "log_rows": [],
                "problem_tables": [],
                "session_id": None,
                "error": "Database unavailable",
                "firebase_available": False,
            },
        )

    from firebase_admin import db

    quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.get("created_by") != email:
        raise HTTPException(status_code=403, detail="Not authorised")

    quiz = _normalize_quiz_data(quiz)
    labels = _quiz_problem_labels(quiz)
    n_prob = max(1, len(labels))

    sessions = collect_quiz_sessions_from_firebase(db, quiz_id)
    payload = find_user_session_payload_for_quiz(sessions, quiz_id, uid, n_prob)
    if not payload:
        return templates.TemplateResponse(
            "instructor_activity_log.html",
            {
                "request": request,
                "instructor_email": email,
                "quiz_id": quiz_id,
                "quiz_teaser": (quiz.get("teaser") or quiz.get("title") or "").strip(),
                "user_id": uid,
                "user_id_query": uid_q,
                "programming_language": "",
                "log_rows": [],
                "problem_tables": [],
                "session_id": None,
                "error": "No session found for this user on this quiz.",
                "firebase_available": True,
            },
        )

    sm = payload.get("session_metadata")
    prog_lang = ""
    if isinstance(sm, dict):
        prog_lang = str(sm.get("programming_language") or "").strip()

    log = build_user_ai_activity_log(payload, labels)
    rows = log.get("rows") or []
    problem_tables = log.get("problem_tables") or []
    return templates.TemplateResponse(
        "instructor_activity_log.html",
        {
            "request": request,
            "instructor_email": email,
            "quiz_id": quiz_id,
            "quiz_teaser": (quiz.get("teaser") or quiz.get("title") or "").strip(),
            "user_id": log.get("user_id") or uid,
            "user_id_query": uid_q,
            "programming_language": prog_lang,
            "log_rows": rows,
            "problem_tables": problem_tables,
            "session_id": log.get("session_id"),
            "error": None
            if rows
            else "No AI suggestion rows in Firebase logs for this session (older submissions may lack `logs.ai_guesses`).",
            "firebase_available": True,
        },
    )


@router.get("/quiz/{quiz_id}/activity-log-data")
async def quiz_activity_log_data(
    request: Request,
    quiz_id: str,
    user_id: str = Query(..., description="Student user id (Firebase)"),
):
    """JSON for the same table as the activity-log page (export / tooling)."""
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        uid = validate_analytics_user_id(user_id)
    except ValueError:
        return JSONResponse({"error": "Invalid user id"}, status_code=400)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    from firebase_admin import db

    try:
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
        if not quiz:
            return JSONResponse({"error": "Quiz not found"}, status_code=404)
        if quiz.get("created_by") != email:
            return JSONResponse({"error": "Not authorised"}, status_code=403)

        quiz = _normalize_quiz_data(quiz)
        labels = _quiz_problem_labels(quiz)
        n_prob = max(1, len(labels))

        sessions = collect_quiz_sessions_from_firebase(db, quiz_id)
        payload = find_user_session_payload_for_quiz(sessions, quiz_id, uid, n_prob)
        if not payload:
            return JSONResponse({"error": "No session for this user"}, status_code=404)

        data = build_user_ai_activity_log(payload, labels)
        return JSONResponse(content=data, headers=_ANALYTICS_JSON_NO_CACHE)
    except Exception as e:
        logger.exception("quiz_activity_log_data failed for quiz_id=%s", quiz_id)
        return JSONResponse({"error": f"Server error: {e!s}"}, status_code=500)


@router.get("/quiz/{quiz_id}/activity-log-export")
async def quiz_activity_log_export(
    request: Request,
    quiz_id: str,
    user_id: str = Query(..., description="Student user id (Firebase)"),
    problem_index: Optional[int] = Query(
        None,
        description="If set, CSV for this problem only; omit for all problems in one file.",
    ),
):
    """Download activity log as CSV (per problem or combined)."""
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        uid = validate_analytics_user_id(user_id)
    except ValueError:
        return JSONResponse({"error": "Invalid user id"}, status_code=400)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    from firebase_admin import db

    try:
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
        if not quiz:
            return JSONResponse({"error": "Quiz not found"}, status_code=404)
        if quiz.get("created_by") != email:
            return JSONResponse({"error": "Not authorised"}, status_code=403)

        quiz = _normalize_quiz_data(quiz)
        labels = _quiz_problem_labels(quiz)
        n_prob = max(1, len(labels))

        sessions = collect_quiz_sessions_from_firebase(db, quiz_id)
        payload = find_user_session_payload_for_quiz(sessions, quiz_id, uid, n_prob)
        if not payload:
            return JSONResponse({"error": "No session for this user"}, status_code=404)

        log = build_user_ai_activity_log(payload, labels)
        problem_tables = log.get("problem_tables") or []
        csv_text = export_activity_log_csv(
            problem_tables,
            str(log.get("user_id") or uid),
            str(log.get("session_id") or "") or None,
            only_problem_index=problem_index,
        )
        if problem_index is not None and not csv_text.strip():
            return JSONResponse({"error": "No rows for this problem"}, status_code=404)

        qslug = _safe_quiz_export_slug(quiz_id)
        uslug = re.sub(r"[^a-zA-Z0-9._-]", "_", uid)[:80] or "user"
        if problem_index is not None:
            fname = f"{qslug}-activity-log-{uslug}-p{int(problem_index)}.csv"
        else:
            fname = f"{qslug}-activity-log-{uslug}-all.csv"

        bom = "\ufeff"
        h = dict(_ANALYTICS_JSON_NO_CACHE)
        h["Content-Disposition"] = f'attachment; filename="{fname}"'
        return Response(
            content=(bom + csv_text).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers=h,
        )
    except Exception as e:
        logger.exception("quiz_activity_log_export failed for quiz_id=%s", quiz_id)
        return JSONResponse({"error": f"Server error: {e!s}"}, status_code=500)


@router.get("/quiz/{quiz_id}/analytics-data")
async def quiz_analytics_data(request: Request, quiz_id: str):
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    from firebase_admin import db

    try:
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
        if not quiz:
            return JSONResponse({"error": "Quiz not found"}, status_code=404)
        if quiz.get("created_by") != email:
            return JSONResponse({"error": "Not authorised"}, status_code=403)

        quiz = _normalize_quiz_data(quiz)
        labels = _quiz_problem_labels(quiz)
        sessions = collect_quiz_sessions_from_firebase(db, quiz_id)
        data = aggregate_quiz_analytics(sessions, quiz_id, labels)

        from main import get_active_quiz_session_counts

        live = get_active_quiz_session_counts(quiz_id)
        live_by_problem = [0] * len(labels)
        for idx, c in (live.get("by_problem_index") or {}).items():
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(live_by_problem):
                live_by_problem[i] = int(c)
        data["live_now"] = {
            "total_active": int(live.get("total_active") or 0),
            "by_problem": live_by_problem,
            "scope_note": "Live counts are from this server only (in-memory). Reload to refresh.",
        }
        return JSONResponse(content=data, headers=_ANALYTICS_JSON_NO_CACHE)
    except Exception as e:
        logger.exception("quiz_analytics_data failed for quiz_id=%s", quiz_id)
        return JSONResponse({"error": f"Server error: {e!s}"}, status_code=500)


@router.post("/quiz/{quiz_id}/analytics-delete-user")
async def quiz_analytics_delete_user(
    request: Request, quiz_id: str, body: Optional[dict] = Body(default=None)
):
    """Remove stored Firebase session data for one user on this quiz (and clear matching in-memory sessions)."""
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"success": False, "error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"success": False, "error": "Database unavailable"}, status_code=503)

    from firebase_admin import db

    user_id = (body or {}).get("user_id")
    if not user_id or not isinstance(user_id, str):
        return JSONResponse({"success": False, "error": "user_id required"}, status_code=400)

    try:
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
        if not quiz:
            return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
        if quiz.get("created_by") != email:
            return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

        result = delete_user_data_for_quiz(db, quiz_id, user_id)
        from main import forget_active_sessions_for_quiz_user

        result["cleared_active_sessions"] = forget_active_sessions_for_quiz_user(quiz_id, user_id)
        return JSONResponse(content={"success": True, **result}, headers=_ANALYTICS_JSON_NO_CACHE)
    except ValueError as ve:
        return JSONResponse({"success": False, "error": str(ve)}, status_code=400)
    except Exception as e:
        logger.exception("quiz_analytics_delete_user failed")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/quiz/{quiz_id}/analytics-export")
async def quiz_analytics_export(request: Request, quiz_id: str, fmt: str = "csv"):
    """Download per-session CSV (flattened) or full aggregate JSON for this quiz."""
    email = _require_instructor(request)
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    fb = _get_firebase()
    if not fb:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    from firebase_admin import db

    kind = (fmt or "csv").strip().lower()
    if kind not in ("csv", "json"):
        return JSONResponse({"error": "fmt must be csv or json"}, status_code=400)

    try:
        quiz = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
        if not quiz:
            return JSONResponse({"error": "Quiz not found"}, status_code=404)
        if quiz.get("created_by") != email:
            return JSONResponse({"error": "Not authorised"}, status_code=403)

        quiz = _normalize_quiz_data(quiz)
        labels = _quiz_problem_labels(quiz)
        sessions = collect_quiz_sessions_from_firebase(db, quiz_id)
        data = aggregate_quiz_analytics(sessions, quiz_id, labels)

        from main import get_active_quiz_session_counts

        live = get_active_quiz_session_counts(quiz_id)
        live_by_problem = [0] * len(labels)
        for idx, c in (live.get("by_problem_index") or {}).items():
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(live_by_problem):
                live_by_problem[i] = int(c)
        data["live_now"] = {
            "total_active": int(live.get("total_active") or 0),
            "by_problem": live_by_problem,
            "scope_note": "Live counts are from this server only (in-memory). Reload to refresh.",
        }

        slug = _safe_quiz_export_slug(quiz_id)
        if kind == "json":
            blob = json.dumps(data, ensure_ascii=False, indent=2)
            h = dict(_ANALYTICS_JSON_NO_CACHE)
            h["Content-Disposition"] = f'attachment; filename="{slug}-analytics.json"'
            return Response(
                content=blob.encode("utf-8"),
                media_type="application/json; charset=utf-8",
                headers=h,
            )

        csv_text = export_per_session_csv(labels, data.get("per_session") or [])
        bom = "\ufeff"
        h = dict(_ANALYTICS_JSON_NO_CACHE)
        h["Content-Disposition"] = f'attachment; filename="{slug}-analytics-sessions.csv"'
        return Response(
            content=(bom + csv_text).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers=h,
        )
    except Exception as e:
        logger.exception("quiz_analytics_export failed")
        return JSONResponse({"error": f"Server error: {e!s}"}, status_code=500)
