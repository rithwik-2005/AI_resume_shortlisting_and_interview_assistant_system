"""
models.py — All Pydantic data models for the AI Resume Shortlisting System.

This is the single source of truth for inter-module data contracts.
Every module imports from here; nothing defines its own schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────

class Tier(str, Enum):
    A = "A"   # Fast-track
    B = "B"   # Technical Screen
    C = "C"   # Needs Evaluation


# ──────────────────────────────────────────────
# Core document types
# ──────────────────────────────────────────────

class WorkExperience(BaseModel):
    role: str
    company: str
    duration: str
    bullets: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    description: str = ""
    stack: list[str] = Field(default_factory=list)


class Education(BaseModel):
    degree: str
    institution: str
    year: str = ""


class ParsedResume(BaseModel):
    """Structured data extracted from a raw resume file."""
    candidate_name: str
    contact_email: Optional[str] = None
    total_experience_years: float = 0.0
    current_role: Optional[str] = None

    skills: list[str] = Field(default_factory=list)
    tools_and_technologies: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)

    education: list[Education] = Field(default_factory=list)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    github_url: Optional[str] = None
    linkedin_url: Optional[str] = None

    raw_text: str = ""


class JobDescription(BaseModel):
    """Structured representation of a Job Description."""
    title: str = "Unknown Role"
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    required_experience_years: float = 0.0
    responsibilities: list[str] = Field(default_factory=list)
    domain_keywords: list[str] = Field(default_factory=list)
    raw_text: str = ""


# ──────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────

class DimensionScore(BaseModel):
    score: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    weighted_score: float = Field(ge=0, le=100)
    explanation: str
    evidence: list[str] = Field(default_factory=list)


class ScoringResult(BaseModel):
    candidate_name: str

    exact_match: DimensionScore
    semantic_similarity: DimensionScore
    achievement_impact: DimensionScore
    ownership_leadership: DimensionScore

    composite_score: float = Field(ge=0, le=100)
    scoring_summary: str
    tier: Tier
    tier_rationale: str


# ──────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────

class GitHubVerification(BaseModel):
    profile_found: bool = False
    username: Optional[str] = None
    public_repos: int = 0
    followers: int = 0
    recent_commits_30d: int = 0
    top_languages: list[str] = Field(default_factory=list)
    account_age_days: int = 0
    activity_score: float = Field(ge=0, le=100, default=0.0)
    flags: list[str] = Field(default_factory=list)
    summary: str = ""


class LinkedInVerification(BaseModel):
    profile_found: bool = False
    profile_url: Optional[str] = None
    flags: list[str] = Field(default_factory=list)
    summary: str = ""


class VerificationResult(BaseModel):
    candidate_name: str
    github: Optional[GitHubVerification] = None
    linkedin: Optional[LinkedInVerification] = None
    overall_authenticity_score: float = Field(ge=0, le=100, default=50.0)
    red_flags: list[str] = Field(default_factory=list)
    verification_summary: str = ""


# ──────────────────────────────────────────────
# Interview plan
# ──────────────────────────────────────────────

class InterviewQuestion(BaseModel):
    category: str
    difficulty: str       # "Warm-up" | "Core" | "Stretch"
    question: str
    expected_answer_hints: list[str] = Field(default_factory=list)
    follow_up: Optional[str] = None
    rationale: str


class InterviewSection(BaseModel):
    section: str
    duration_min: int
    focus: str


class InterviewPlan(BaseModel):
    candidate_name: str
    tier: Tier
    total_duration_minutes: int
    interview_sections: list[InterviewSection] = Field(default_factory=list)
    questions: list[InterviewQuestion] = Field(default_factory=list)
    opening_context: str
    evaluation_rubric: str


# ──────────────────────────────────────────────
# Pipeline result (top-level)
# ──────────────────────────────────────────────

class PipelineResult(BaseModel):
    resume: ParsedResume
    jd: JobDescription
    scoring: ScoringResult
    verification: Optional[VerificationResult] = None
    interview_plan: Optional[InterviewPlan] = None