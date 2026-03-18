"""
modules/scoring_engine.py — Option A: Multi-dimensional Evaluation & Scoring.

Four scoring dimensions
-----------------------
Dimension               Weight   What it measures
─────────────────────────────────────────────────────────────────────────────
Exact Match              25%     Literal skill / keyword overlap with JD
Semantic Similarity      35%     Conceptual equivalence (Kafka ↔ Kinesis etc.)
Achievement Impact       25%     Quantified accomplishments in work history
Ownership / Leadership   15%     Evidence of owning systems or leading teams

Composite = weighted sum. Tier assigned by threshold (75 / 50).

Parallelism
-----------
The four dimension calls are independent of each other.  They are fired
concurrently using a ThreadPoolExecutor so total wall-clock time is
~max(single_call) instead of ~4×single_call — typically 3–4 s vs 12–16 s.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from config import settings
from models import (
    DimensionScore,
    JobDescription,
    ParsedResume,
    ScoringResult,
    Tier,
)
from modules.llm_client import chat_json

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Prompt templates
# ──────────────────────────────────────────────

_EXACT_MATCH_SYSTEM = """
You are a technical recruiter scoring a candidate on EXACT SKILL MATCH.
Return a JSON object with exactly these keys:
{
  "score": <integer 0-100>,
  "explanation": "<2-3 sentence rationale>",
  "evidence": ["<exact matching skill or tool>", ...]
}
Rules:
- Count literal matches between candidate skills and JD required/preferred skills.
- Required skills are worth 2× more than preferred skills.
- Only include matches you can cite directly from both lists.
"""

_SEMANTIC_SYSTEM = """
You are an expert in technology equivalences scoring SEMANTIC SIMILARITY.

Key equivalence clusters to recognise (score these as strong matches):
  Message queues / streaming : Kafka ↔ RabbitMQ, SQS, Kinesis, Pulsar, EventBridge
  Container orchestration    : Kubernetes ↔ ECS, Nomad, Mesos
  Distributed data processing: Spark ↔ Flink, Beam, Dataflow
  In-memory cache            : Redis ↔ Memcached, ElastiCache
  Relational databases       : PostgreSQL ↔ MySQL, Aurora, MariaDB
  NoSQL                      : DynamoDB ↔ MongoDB, Cosmos DB, Firestore
  Frontend SPA               : React ↔ Vue, Angular, Svelte
  CI/CD                      : GitHub Actions ↔ Jenkins, CircleCI, GitLab CI
  IaC                        : Terraform ↔ CloudFormation, Pulumi

Score how well the candidate's tech exposure covers the JD needs,
EVEN IF different specific tools were used.

Return a JSON object with exactly these keys:
{
  "score": <integer 0-100>,
  "explanation": "<2-3 sentence rationale naming specific equivalences found>",
  "evidence": ["<equivalence pair found>", ...]
}
"""

_ACHIEVEMENT_SYSTEM = """
You are scoring a candidate on ACHIEVEMENT IMPACT.
Look for quantified, concrete accomplishments:
  ✓ Metrics with numbers (reduced latency 40%, served 10M users, $2M revenue)
  ✓ Awards, promotions, scope increases
  ✓ Open-source projects with community adoption
  ✗ Penalise vague: "helped with", "worked on", "involved in", "contributed to"

Return a JSON object with exactly these keys:
{
  "score": <integer 0-100>,
  "explanation": "<2-3 sentence rationale>",
  "evidence": ["<quoted achievement with metric>", ...]
}
"""

_OWNERSHIP_SYSTEM = """
You are scoring a candidate on OWNERSHIP & LEADERSHIP.
Strong signals:
  ✓ "Led", "owned", "architected", "designed from scratch", "built"
  ✓ Managed engineers or cross-functional teams
  ✓ Drove technical decisions / roadmaps
  ✓ On-call incident commander
  ✓ Open-source project maintainer

Return a JSON object with exactly these keys:
{
  "score": <integer 0-100>,
  "explanation": "<2-3 sentence rationale>",
  "evidence": ["<ownership signal excerpt>", ...]
}
"""

_SUMMARY_SYSTEM = """
You are a senior technical recruiter writing a hiring decision summary.
Return a JSON object with exactly these keys:
{
  "scoring_summary": "<3-4 sentence narrative: decision, strongest signals, key gaps, next step>",
  "tier_rationale": "<1-2 sentence tier justification>"
}
"""


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────

def _fmt_work(jobs: list) -> str:
    parts = []
    for j in jobs:
        bullets = "\n  • ".join(j.bullets[:8])
        parts.append(f"{j.role} @ {j.company} ({j.duration})\n  • {bullets}")
    return "\n\n".join(parts) or "No work experience provided."


def _fmt_projects(projects: list) -> str:
    return "\n".join(
        f"{p.name}: {p.description[:120]} [Stack: {', '.join(p.stack)}]"
        for p in projects
    ) or "No projects listed."


def _assign_tier(score: float) -> Tier:
    if score >= settings.tier_a_threshold:
        return Tier.A
    if score >= settings.tier_b_threshold:
        return Tier.B
    return Tier.C


def _make_dimension(raw: dict, weight: float) -> DimensionScore:
    score = float(raw.get("score", 0))
    return DimensionScore(
        score=score,
        weight=weight,
        weighted_score=round(score * weight, 2),
        explanation=raw.get("explanation", ""),
        evidence=raw.get("evidence", []),
    )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def score_candidate(resume: ParsedResume, jd: JobDescription) -> ScoringResult:
    """
    Run all four scoring dimensions CONCURRENTLY and return a fully
    explainable ScoringResult.

    The four dimension calls (Exact Match, Semantic Similarity, Achievement
    Impact, Ownership) are independent — they are fired in parallel using a
    ThreadPoolExecutor, reducing wall-clock time from ~12 s to ~3–4 s.

    Parameters
    ----------
    resume : ParsedResume
    jd : JobDescription

    Returns
    -------
    ScoringResult
    """
    logger.info("Scoring candidate (parallel): %s", resume.candidate_name)

    all_candidate_skills = list(
        set(resume.skills + resume.tools_and_technologies + resume.programming_languages)
    )
    work_text    = _fmt_work(resume.work_experience)
    project_text = _fmt_projects(resume.projects)

    # ── Define each dimension as a (name, callable) pair ─────────────────

    def _call_exact() -> dict:
        return chat_json(
            system=_EXACT_MATCH_SYSTEM,
            user=(
                f"JD Required Skills: {jd.required_skills}\n"
                f"JD Preferred Skills: {jd.preferred_skills}\n\n"
                f"Candidate Skills: {resume.skills}\n"
                f"Candidate Tools: {resume.tools_and_technologies}\n"
                f"Candidate Languages: {resume.programming_languages}"
            ),
        )

    def _call_semantic() -> dict:
        return chat_json(
            system=_SEMANTIC_SYSTEM,
            user=(
                f"JD Required + Preferred Skills: {jd.required_skills + jd.preferred_skills}\n"
                f"JD Domain Keywords: {jd.domain_keywords}\n\n"
                f"Candidate All Skills: {all_candidate_skills}\n"
                f"Candidate Work Summary:\n{work_text[:900]}\n"
                f"Candidate Projects:\n{project_text[:400]}"
            ),
        )

    def _call_achievement() -> dict:
        return chat_json(
            system=_ACHIEVEMENT_SYSTEM,
            user=f"Work Experience:\n{work_text}\n\nProjects:\n{project_text}",
        )

    def _call_ownership() -> dict:
        return chat_json(
            system=_OWNERSHIP_SYSTEM,
            user=f"Work Experience:\n{work_text}",
        )

    tasks: list[tuple[str, Callable[[], dict]]] = [
        ("exact",       _call_exact),
        ("semantic",    _call_semantic),
        ("achievement", _call_achievement),
        ("ownership",   _call_ownership),
    ]

    # ── Fire all four calls in parallel ──────────────────────────────────
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_to_name = {pool.submit(fn): name for name, fn in tasks}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
                logger.debug("Dimension '%s' scored: %s", name, results[name].get("score"))
            except Exception as exc:
                logger.error("Dimension '%s' failed: %s — using zero score.", name, exc)
                results[name] = {"score": 0, "explanation": f"Scoring failed: {exc}", "evidence": []}

    # ── Build dimension objects ───────────────────────────────────────────
    exact       = _make_dimension(results["exact"],       settings.weight_exact_match)
    semantic    = _make_dimension(results["semantic"],    settings.weight_semantic_similarity)
    achievement = _make_dimension(results["achievement"], settings.weight_achievement_impact)
    ownership   = _make_dimension(results["ownership"],   settings.weight_ownership_leadership)

    # ── Composite & Tier ──────────────────────────────────────────────────
    composite = round(
        exact.weighted_score
        + semantic.weighted_score
        + achievement.weighted_score
        + ownership.weighted_score,
        1,
    )
    tier = _assign_tier(composite)

    # ── Summary (sequential — depends on all dimension results) ───────────
    summary_raw = chat_json(
        system=_SUMMARY_SYSTEM,
        user=(
            f"Candidate: {resume.candidate_name} | Role: {jd.title}\n"
            f"Scores: Exact={exact.score:.0f}(25%) Semantic={semantic.score:.0f}(35%) "
            f"Achievement={achievement.score:.0f}(25%) Ownership={ownership.score:.0f}(15%)\n"
            f"Composite: {composite} | Tier: {tier.value}\n\n"
            f"Exact evidence: {exact.evidence[:3]}\n"
            f"Semantic evidence: {semantic.evidence[:3]}\n"
            f"Achievement evidence: {achievement.evidence[:3]}\n"
            f"Ownership evidence: {ownership.evidence[:3]}"
        ),
        max_tokens=600,
    )

    return ScoringResult(
        candidate_name=resume.candidate_name,
        exact_match=exact,
        semantic_similarity=semantic,
        achievement_impact=achievement,
        ownership_leadership=ownership,
        composite_score=composite,
        scoring_summary=summary_raw.get("scoring_summary", ""),
        tier=tier,
        tier_rationale=summary_raw.get("tier_rationale", ""),
    )