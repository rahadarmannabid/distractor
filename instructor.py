"""Instructor router — OTP auth, dashboard, quiz CRUD, share-link generation.

Mounted as a sub-router in main.py at prefix /instructor.
"""

from fastapi import APIRouter, Request, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from typing import Optional
import logging
import os
import re
import secrets
import uuid

from models import QuizCreate, QuizUpdate, QuizJoinRequest

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
    email = (form.get("email") or "").lower().strip()

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
    email = (form.get("email") or "").lower().strip()
    otp_input = (form.get("otp") or "").strip()

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
        quiz_ids = db.reference(f"instructors/{sanitized}/quizzes").get() or {}
        if quiz_ids:
            all_quizzes = db.reference("quizzes").get() or {}
            for qid in quiz_ids:
                qdata = all_quizzes.get(qid)
                if qdata:
                    qdata["id"] = qid
                    quizzes.append(qdata)
        quizzes.sort(key=lambda q: q.get("created_at", ""), reverse=True)

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
        quiz = db.reference(f"quizzes/{quiz_id}").get()
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
    existing = db.reference(f"quizzes/{quiz_id}").get()
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
    existing = db.reference(f"quizzes/{quiz_id}").get()
    if not existing:
        return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
    if existing.get("created_by") != email:
        return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    access_code = existing.get("access_code", "")
    db.reference(f"quizzes/{quiz_id}").delete()
    if access_code:
        db.reference(f"quiz_access/{access_code}").delete()
    db.reference(f"instructors/{sanitize_key(email)}/quizzes/{quiz_id}").delete()

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
    existing = db.reference(f"quizzes/{quiz_id}").get()
    if not existing:
        return JSONResponse({"success": False, "error": "Quiz not found"}, status_code=404)
    if existing.get("created_by") != email:
        return JSONResponse({"success": False, "error": "Not authorised"}, status_code=403)

    new_status = not existing.get("published", False)
    db.reference(f"quizzes/{quiz_id}/published").set(new_status)
    return JSONResponse({"success": True, "published": new_status})
