"""Pydantic models for the instructor quiz-creation system.

These models are additive — the existing models in main.py are untouched.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime


# ─── OTP ────────────────────────────────────────────────────────────────────

class OTPRequest(BaseModel):
    email: str


class OTPVerify(BaseModel):
    email: str
    otp: str


# ─── Survey Questions ───────────────────────────────────────────────────────

class SurveyQuestionCreate(BaseModel):
    """A single survey question attached to a quiz."""
    id: str = ""
    question: str
    type: str = "likert"  # likert, likert_7_with_na, multiple_choice, open_ended, slider, info_block
    required: bool = True
    options: List[str] = Field(default_factory=list)  # for multiple_choice
    scale: Optional[Dict[str, Any]] = None  # for likert/slider
    placeholder: str = ""
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    help_text: str = ""
    content: str = ""  # for info_block


# ─── Test Cases ─────────────────────────────────────────────────────────────

class TestCase(BaseModel):
    input: str
    expected_output: str


# ─── Quiz Creation / Update ─────────────────────────────────────────────────

class QuizCreate(BaseModel):
    title: str
    description: str = ""
    problem_statement: str
    constraints: str = ""
    allowed_languages: List[str] = Field(default_factory=lambda: ["python", "javascript", "c"])
    time_limit_minutes: int = 0  # 0 = unlimited
    condition: int = 1  # exactly one condition: 1, 2, or 3
    starter_code: Dict[str, str] = Field(default_factory=dict)  # {"python": "...", "javascript": "...", "c": "..."}
    public_test_cases: List[TestCase] = Field(default_factory=list)
    hidden_test_cases: List[TestCase] = Field(default_factory=list)
    recurrent_survey: List[SurveyQuestionCreate] = Field(default_factory=list)
    pre_survey: List[SurveyQuestionCreate] = Field(default_factory=list)
    post_survey: List[SurveyQuestionCreate] = Field(default_factory=list)
    published: bool = False
    availability_start: Optional[str] = None
    availability_end: Optional[str] = None


class QuizUpdate(BaseModel):
    """Partial update — all fields optional."""
    title: Optional[str] = None
    description: Optional[str] = None
    problem_statement: Optional[str] = None
    constraints: Optional[str] = None
    allowed_languages: Optional[List[str]] = None
    time_limit_minutes: Optional[int] = None
    condition: Optional[int] = None
    starter_code: Optional[Dict[str, str]] = None
    public_test_cases: Optional[List[TestCase]] = None
    hidden_test_cases: Optional[List[TestCase]] = None
    recurrent_survey: Optional[List[SurveyQuestionCreate]] = None
    pre_survey: Optional[List[SurveyQuestionCreate]] = None
    post_survey: Optional[List[SurveyQuestionCreate]] = None
    published: Optional[bool] = None
    availability_start: Optional[str] = None
    availability_end: Optional[str] = None


# ─── Quiz Response (read) ───────────────────────────────────────────────────

class QuizSummary(BaseModel):
    """Lightweight quiz info for dashboard listing."""
    id: str
    title: str
    description: str = ""
    published: bool = False
    access_code: str = ""
    created_at: str = ""
    allowed_languages: List[str] = Field(default_factory=list)
    time_limit_minutes: int = 0
    condition: int = 1


# ─── Student quiz join ──────────────────────────────────────────────────────

class QuizJoinRequest(BaseModel):
    access_code: str
    user_id: str
    programming_language: str = "python"
