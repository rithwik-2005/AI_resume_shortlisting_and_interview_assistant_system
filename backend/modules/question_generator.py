"""
modules/question_generator.py — Option C: Tiered Interview Plan Generator.

Generates a fully personalised interview plan grounded in the candidate's
own resume — not generic templates.

Tier structure
  A (>=75)  60 min  Deep architecture, leadership, culture fit
  B (>=50)  75 min  Balanced technical + behavioural
  C (<50)   45 min  Fundamentals, motivation, learning ability

Fix applied: prompt content is now explicitly capped and truncated before
being sent to the LLM to prevent HTTP 400 "could not parse JSON body" errors
caused by oversized or character-corrupted payloads.
"""

from __future__ import annotations

import logging

from models import (
    InterviewPlan,
    InterviewQuestion,
    InterviewSection,
    JobDescription,
    ParsedResume,
    ScoringResult,
    Tier,
)
from modules.llm_client import chat_json

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Tier configuration
# ──────────────────────────────────────────────

TIER_CONFIG: dict[Tier, dict] = {
    Tier.A: {
        "total_minutes": 60,
        "sections": [
            {"section": "Architecture & System Design", "duration_min": 25, "focus": "Trade-offs, scalability, design decisions"},
            {"section": "Ownership & Leadership Stories", "duration_min": 20, "focus": "High-impact examples of owning outcomes"},
            {"section": "Gap Probe & Culture Fit",        "duration_min": 10, "focus": "Scoring gaps, values alignment"},
            {"section": "Candidate Q&A",                  "duration_min":  5, "focus": "Their questions for us"},
        ],
        "categories":    ["System Design", "Leadership/Ownership", "Domain Depth", "Gap Probe"],
        "num_questions": 8,
        "difficulty_mix": "1 Warm-up, 4 Core, 3 Stretch",
    },
    Tier.B: {
        "total_minutes": 75,
        "sections": [
            {"section": "Technical Fundamentals",     "duration_min": 25, "focus": "Core concepts relevant to JD"},
            {"section": "Experience Deep Dive",       "duration_min": 25, "focus": "Specific projects and outcomes from resume"},
            {"section": "Behavioural & Collaboration","duration_min": 15, "focus": "Teamwork, conflict resolution, growth"},
            {"section": "Problem Solving",            "duration_min":  5, "focus": "Live thinking / debugging"},
            {"section": "Candidate Q&A",              "duration_min":  5, "focus": "Their questions"},
        ],
        "categories":    ["Technical Fundamentals", "Past Experience", "Behavioural", "Problem Solving"],
        "num_questions": 8,
        "difficulty_mix": "2 Warm-up, 4 Core, 2 Stretch",
    },
    Tier.C: {
        "total_minutes": 45,
        "sections": [
            {"section": "Background & Motivation", "duration_min": 10, "focus": "Why this role, career goals"},
            {"section": "Fundamentals Check",      "duration_min": 20, "focus": "Core JD skills — assess baseline"},
            {"section": "Learning & Adaptability", "duration_min": 10, "focus": "How they handle gaps, new tech"},
            {"section": "Candidate Q&A",           "duration_min":  5, "focus": "Their questions"},
        ],
        "categories":    ["Fundamentals", "Behavioural", "Motivation", "Growth Potential"],
        "num_questions": 8,
        "difficulty_mix": "3 Warm-up, 4 Core, 1 Stretch",
    },
}

TIER_DESCRIPTIONS = {
    Tier.A: "Fast-track — strong candidate; focus on architecture depth and culture fit",
    Tier.B: "Technical Screen — verify depth; explore growth potential and collaboration",
    Tier.C: "Needs Evaluation — assess fundamentals and learning ability",
}

# ── Character caps for prompt sections ──────────────────────────────────────
# These keep the total user message well under the 60k-char limit in llm_client.
_MAX_WORK_CHARS    = 1500   # work experience block
_MAX_PROJECT_CHARS = 600    # projects block
_MAX_SKILLS_ITEMS  = 20     # max skill tokens in the list


# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────

_QUESTIONS_SYSTEM = """
You are a senior technical interviewer crafting PERSONALISED interview questions.

Rules:
- Ground every question in THIS candidate's actual experience (name their real projects, companies, tools).
- Do NOT write generic questions like "Explain quicksort" or "What is REST?".
- Calibrate difficulty to the candidate's tier.
- Cover the given categories proportionally.
- Return a JSON object with a single key "questions" containing an array.

Each element of "questions" must be an object with these exact keys:
  category              : string
  difficulty            : one of "Warm-up", "Core", "Stretch"
  question              : string referencing the candidate's actual experience
  expected_answer_hints : array of 2-3 short strings
  follow_up             : string (one follow-up question)
  rationale             : string (why this question for THIS candidate)
"""

_BRIEFING_SYSTEM = """
You are writing an interviewer briefing memo.
Return a JSON object with exactly these two keys:
  opening_context  : 3-4 sentence briefing covering who this candidate is,
                     their top signals, key probes, and overall interview approach.
  evaluation_rubric: 2 paragraphs — what a HIRE looks like vs a NO-HIRE for this tier.
"""


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _fmt_work(jobs: list, max_chars: int = _MAX_WORK_CHARS) -> str:
    """Format work experience, hard-capped at max_chars."""
    parts = []
    for j in jobs:
        bullets = "\n  ".join(f"- {b}" for b in j.bullets[:4])
        parts.append(f"{j.role} @ {j.company} ({j.duration})\n  {bullets}")
    text = "\n\n".join(parts) or "No work experience."
    return text[:max_chars]


def _fmt_projects(projects: list, max_chars: int = _MAX_PROJECT_CHARS) -> str:
    """Format projects, hard-capped at max_chars."""
    lines = []
    for p in projects:
        stack = ", ".join(p.stack[:6])
        lines.append(f"{p.name}: {p.description[:80]} [Stack: {stack}]")
    text = "\n".join(lines) or "No projects."
    return text[:max_chars]


def _fmt_skills(resume: ParsedResume, max_items: int = _MAX_SKILLS_ITEMS) -> str:
    """Combine all skill buckets into a short comma-separated string."""
    combined = list(dict.fromkeys(
        resume.skills + resume.tools_and_technologies + resume.programming_languages
    ))
    return ", ".join(combined[:max_items])


def _identify_gaps(scoring: ScoringResult) -> str:
    dims = {
        "Exact Match":           scoring.exact_match.score,
        "Semantic Similarity":   scoring.semantic_similarity.score,
        "Achievement Impact":    scoring.achievement_impact.score,
        "Ownership/Leadership":  scoring.ownership_leadership.score,
    }
    gaps = [f"{k} ({v:.0f}/100)" for k, v in dims.items() if v < 60]
    return ", ".join(gaps) if gaps else "No major gaps."


def _build_questions_user_msg(
    resume: ParsedResume,
    jd: JobDescription,
    scoring: ScoringResult,
    tier: Tier,
    config: dict,
) -> str:
    """
    Build the user message for question generation.
    All sections are capped so the total stays under llm_client's 60k limit.
    """
    lines = [
        f"Candidate: {resume.candidate_name} | Role: {jd.title}",
        f"Tier: {tier.value} — {TIER_DESCRIPTIONS[tier]}",
        f"Difficulty mix: {config['difficulty_mix']}",
        f"Categories: {config['categories']}",
        f"Generate exactly {config['num_questions']} questions.",
        "",
        f"Skills: {_fmt_skills(resume)}",
        "",
        "Work Experience:",
        _fmt_work(resume.work_experience),
        "",
        "Projects:",
        _fmt_projects(resume.projects),
        "",
        f"Scoring gaps to probe: {_identify_gaps(scoring)}",
        f"JD required skills: {', '.join(jd.required_skills[:12])}",
        f"JD preferred skills: {', '.join(jd.preferred_skills[:8])}",
    ]
    return "\n".join(lines)


def _build_briefing_user_msg(
    resume: ParsedResume,
    jd: JobDescription,
    scoring: ScoringResult,
    verification_summary: str,
    red_flags: list[str],
    tier: Tier,
) -> str:
    summary = (scoring.scoring_summary or "")[:600]
    return (
        f"Candidate: {resume.candidate_name} | Role: {jd.title} | Tier: {tier.value}\n"
        f"Composite Score: {scoring.composite_score}/100\n"
        f"Scoring summary: {summary}\n"
        f"Verification: {(verification_summary or 'Not performed')[:300]}\n"
        f"Red flags: {red_flags[:5] if red_flags else []}"
    )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def generate_interview_plan(
    resume: ParsedResume,
    jd: JobDescription,
    scoring: ScoringResult,
    verification_summary: str = "",
    red_flags: list[str] | None = None,
) -> InterviewPlan:
    """
    Generate a tailored interview plan for the candidate.

    Parameters
    ----------
    resume               : ParsedResume
    jd                   : JobDescription
    scoring              : ScoringResult  (includes tier)
    verification_summary : str
    red_flags            : list[str]

    Returns
    -------
    InterviewPlan
    """
    tier   = scoring.tier
    config = TIER_CONFIG[tier]
    logger.info("Generating Tier %s interview plan for %s", tier.value, resume.candidate_name)

    # ── Questions ──────────────────────────────────────────────────────────
    q_user_msg = _build_questions_user_msg(resume, jd, scoring, tier, config)
    logger.debug("Questions prompt length: %d chars", len(q_user_msg))

    q_raw = chat_json(
        system=_QUESTIONS_SYSTEM,
        user=q_user_msg,
        max_tokens=3000,
    )

    questions = [
        InterviewQuestion(
            category=q.get("category", "General"),
            difficulty=q.get("difficulty", "Core"),
            question=q.get("question", ""),
            expected_answer_hints=q.get("expected_answer_hints", []),
            follow_up=q.get("follow_up"),
            rationale=q.get("rationale", ""),
        )
        for q in q_raw.get("questions", [])
    ]

    if not questions:
        logger.warning("No questions returned by model — returning empty plan.")

    # ── Briefing ───────────────────────────────────────────────────────────
    briefing_user_msg = _build_briefing_user_msg(
        resume, jd, scoring,
        verification_summary or "",
        red_flags or [],
        tier,
    )
    logger.debug("Briefing prompt length: %d chars", len(briefing_user_msg))

    briefing_raw = chat_json(
        system=_BRIEFING_SYSTEM,
        user=briefing_user_msg,
        max_tokens=700,
    )

    sections = [InterviewSection(**s) for s in config["sections"]]

    return InterviewPlan(
        candidate_name=resume.candidate_name,
        tier=tier,
        total_duration_minutes=config["total_minutes"],
        interview_sections=sections,
        questions=questions,
        opening_context=briefing_raw.get("opening_context", ""),
        evaluation_rubric=briefing_raw.get("evaluation_rubric", ""),
    )