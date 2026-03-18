"""
modules/question_generator.py — Option C: Tiered Interview Plan Generator.

Generates a fully personalised interview plan grounded in the candidate's
own resume — not generic templates.

Tier → structure mapping
  A (≥75)  60 min  Deep architecture, leadership, culture fit
  B (≥50)  75 min  Balanced technical + behavioural
  C (<50)  45 min  Fundamentals, motivation, learning ability
"""

from __future__ import annotations

import json
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
from modules.llm_client import chat_json, chat_text

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
            {"section": "Gap Probe & Culture Fit",       "duration_min": 10, "focus": "Scoring gaps, values alignment"},
            {"section": "Candidate Q&A",                 "duration_min":  5, "focus": "Their questions for us"},
        ],
        "categories":   ["System Design", "Leadership/Ownership", "Domain Depth", "Gap Probe"],
        "num_questions": 10,
        "difficulty_mix": "1 Warm-up, 5 Core, 4 Stretch",
    },
    Tier.B: {
        "total_minutes": 75,
        "sections": [
            {"section": "Technical Fundamentals",    "duration_min": 25, "focus": "Core concepts relevant to JD"},
            {"section": "Experience Deep Dive",      "duration_min": 25, "focus": "Specific projects and outcomes from resume"},
            {"section": "Behavioural & Collaboration","duration_min": 15, "focus": "Teamwork, conflict resolution, growth"},
            {"section": "Problem Solving",           "duration_min":  5, "focus": "Live thinking / debugging"},
            {"section": "Candidate Q&A",             "duration_min":  5, "focus": "Their questions"},
        ],
        "categories":   ["Technical Fundamentals", "Past Experience", "Behavioural", "Problem Solving"],
        "num_questions": 10,
        "difficulty_mix": "2 Warm-up, 6 Core, 2 Stretch",
    },
    Tier.C: {
        "total_minutes": 45,
        "sections": [
            {"section": "Background & Motivation", "duration_min": 10, "focus": "Why this role, career goals"},
            {"section": "Fundamentals Check",      "duration_min": 20, "focus": "Core JD skills — assess baseline"},
            {"section": "Learning & Adaptability", "duration_min": 10, "focus": "How they handle gaps, new tech"},
            {"section": "Candidate Q&A",           "duration_min":  5, "focus": "Their questions"},
        ],
        "categories":   ["Fundamentals", "Behavioural", "Motivation", "Growth Potential"],
        "num_questions": 10,
        "difficulty_mix": "4 Warm-up, 5 Core, 1 Stretch",
    },
}

TIER_DESCRIPTIONS = {
    Tier.A: "Fast-track — strong candidate; focus on architecture depth and culture fit",
    Tier.B: "Technical Screen — verify depth; explore growth potential and collaboration",
    Tier.C: "Needs Evaluation — assess fundamentals and learning ability",
}

# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────

_QUESTIONS_SYSTEM = """
You are a senior technical interviewer crafting PERSONALISED interview questions.

Rules:
- Ground every question in THIS candidate's actual experience (name their real projects, companies, tools).
- Do NOT write generic questions like "Explain quicksort" or "What is REST?".
- Calibrate difficulty to the candidate's tier.
- Cover the categories proportionally.
- Return a JSON object with a single key "questions" containing an array.

Each question object must have:
{
  "category": string,
  "difficulty": "Warm-up" | "Core" | "Stretch",
  "question": string  (reference their actual experience),
  "expected_answer_hints": [string, string, string],
  "follow_up": string,
  "rationale": string  (why this question for THIS candidate)
}
"""

_BRIEFING_SYSTEM = """
You are writing an interviewer briefing memo.
Return a JSON object with exactly these keys:
{
  "opening_context": "<3-4 sentence briefing: who is this candidate, top signals, key probes, overall approach>",
  "evaluation_rubric": "<2 paragraphs: what HIRE looks like vs what NO-HIRE looks like for this tier>"
}
"""

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _fmt_work(jobs: list) -> str:
    return "\n\n".join(
        f"{j.role} @ {j.company} ({j.duration})\n  " + "\n  ".join(f"• {b}" for b in j.bullets[:5])
        for j in jobs
    ) or "No work experience."


def _fmt_projects(projects: list) -> str:
    return "\n".join(
        f"{p.name}: {p.description[:100]} [Stack: {', '.join(p.stack)}]"
        for p in projects
    ) or "No projects."


def _identify_gaps(scoring: ScoringResult) -> str:
    dims = {
        "Exact Match": scoring.exact_match.score,
        "Semantic Similarity": scoring.semantic_similarity.score,
        "Achievement Impact": scoring.achievement_impact.score,
        "Ownership/Leadership": scoring.ownership_leadership.score,
    }
    gaps = [f"{k} ({v:.0f}/100)" for k, v in dims.items() if v < 60]
    return ", ".join(gaps) if gaps else "No major scoring gaps."


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
    resume : ParsedResume
    jd : JobDescription
    scoring : ScoringResult  (includes tier)
    verification_summary : str
    red_flags : list[str]

    Returns
    -------
    InterviewPlan
    """
    tier = scoring.tier
    config = TIER_CONFIG[tier]
    logger.info("Generating Tier %s interview plan for %s", tier.value, resume.candidate_name)

    # ── Questions ──────────────────────────────────────────────────────────
    q_raw = chat_json(
        system=_QUESTIONS_SYSTEM,
        user=(
            f"Candidate: {resume.candidate_name} | Role: {jd.title}\n"
            f"Tier: {tier.value} — {TIER_DESCRIPTIONS[tier]}\n"
            f"Difficulty mix: {config['difficulty_mix']}\n"
            f"Categories: {config['categories']}\n"
            f"Generate exactly {config['num_questions']} questions.\n\n"
            f"Candidate skills: {', '.join((resume.skills + resume.tools_and_technologies)[:20])}\n\n"
            f"Work Experience:\n{_fmt_work(resume.work_experience)}\n\n"
            f"Projects:\n{_fmt_projects(resume.projects)}\n\n"
            f"Scoring gaps to probe: {_identify_gaps(scoring)}\n"
            f"JD focus skills: {', '.join((jd.required_skills + jd.preferred_skills)[:15])}"
        ),
        max_tokens=3500,
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

    # ── Briefing ───────────────────────────────────────────────────────────
    briefing_raw = chat_json(
        system=_BRIEFING_SYSTEM,
        user=(
            f"Candidate: {resume.candidate_name} | Role: {jd.title} | Tier: {tier.value}\n"
            f"Composite Score: {scoring.composite_score}/100\n"
            f"Scoring summary: {scoring.scoring_summary}\n"
            f"Verification: {verification_summary or 'Not performed'}\n"
            f"Red flags: {red_flags or []}"
        ),
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