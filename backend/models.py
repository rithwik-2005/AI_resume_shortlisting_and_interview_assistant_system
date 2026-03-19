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
    role: str = ""
    company: str = ""
    duration: str = ""          # was required str — LLM sometimes returns null
    bullets: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    description: str = ""
    stack: list[str] = Field(default_factory=list)


class Education(BaseModel):
    degree: str = ""
    institution: str = ""
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
    leetcode_url: Optional[str] = None
    other_links: list[str] = Field(default_factory=list)   # Kaggle, HackerRank, etc.

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
    unreachable: bool = False          # True when network/timeout error
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
    blocked: bool = False              # True when LinkedIn returns 999 or network error
    server_blocked: bool = False       # True when server can't reach but URL is valid
    profile_url: Optional[str] = None
    flags: list[str] = Field(default_factory=list)
    summary: str = ""


class LeetCodeVerification(BaseModel):
    """Stats pulled from LeetCode public GraphQL API."""
    profile_found: bool = False
    server_blocked: bool = False       # True when API unreachable server-side
    username: Optional[str] = None
    profile_url: Optional[str] = None
    total_solved: int = 0
    easy_solved: int = 0
    medium_solved: int = 0
    hard_solved: int = 0
    global_ranking: Optional[int] = None
    activity_score: float = Field(ge=0, le=100, default=0.0)
    platform_description: str = (
        "LeetCode is a competitive programming platform used to practise "
        "data structures and algorithms. Solving problems here demonstrates "
        "coding proficiency and problem-solving ability."
    )
    flags: list[str] = Field(default_factory=list)
    summary: str = ""


class OtherProfileVerification(BaseModel):
    """Lightweight verification for Kaggle, HackerRank, CodeChef, etc."""
    platform: str
    url: str
    username: Optional[str] = None
    profile_found: bool = False
    server_blocked: bool = False       # True when server can't reach but URL is from resume
    activity_score: float = Field(ge=0, le=100, default=0.0)
    platform_description: str = ""
    highlights: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    summary: str = ""


class VerificationResult(BaseModel):
    candidate_name: str
    github: Optional[GitHubVerification] = None
    linkedin: Optional[LinkedInVerification] = None
    leetcode: Optional[LeetCodeVerification] = None
    other_profiles: list[OtherProfileVerification] = Field(default_factory=list)
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