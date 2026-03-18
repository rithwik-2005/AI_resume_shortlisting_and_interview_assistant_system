"""
modules/batch_evaluator.py — Batch evaluation of multiple resumes against one JD.

Evaluates N candidates in parallel using a ThreadPoolExecutor,
then returns them ranked by composite score with a leaderboard summary.

This addresses the "10,000 resumes/day" scalability requirement from the PRD:
- Each candidate is evaluated in a dedicated thread.
- Per-candidate scoring already parallelises its 4 dimension calls internally.
- So total parallelism = batch_workers × 4 dimension threads.

Usage
-----
    from modules.batch_evaluator import batch_evaluate
    results = batch_evaluate(resumes, jd, max_workers=10)
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from models import JobDescription, ParsedResume, ScoringResult, Tier
from modules.scoring_engine import score_candidate

logger = logging.getLogger(__name__)

# Max concurrent candidate evaluations per batch call.
# Keep at ≤10 to stay within OpenAI's default rate limits.
DEFAULT_BATCH_WORKERS = 5


@dataclass
class CandidateRank:
    """A single row in the batch leaderboard."""
    rank: int
    candidate_name: str
    composite_score: float
    tier: Tier
    tier_rationale: str
    top_signal: str           # best evidence item from the highest-scoring dimension
    biggest_gap: str          # explanation of the lowest-scoring dimension
    scoring: ScoringResult


@dataclass
class BatchResult:
    """Output of a batch evaluation run."""
    jd_title: str
    total_candidates: int
    tier_a_count: int
    tier_b_count: int
    tier_c_count: int
    leaderboard: list[CandidateRank] = field(default_factory=list)
    failed_candidates: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────

def _top_signal(scoring: ScoringResult) -> str:
    """Return the most impressive evidence item across all dimensions."""
    dims = [
        scoring.exact_match,
        scoring.semantic_similarity,
        scoring.achievement_impact,
        scoring.ownership_leadership,
    ]
    best_dim = max(dims, key=lambda d: d.score)
    if best_dim.evidence:
        return best_dim.evidence[0]
    return best_dim.explanation[:120] + "…" if len(best_dim.explanation) > 120 else best_dim.explanation


def _biggest_gap(scoring: ScoringResult) -> str:
    """Return the explanation of the weakest scoring dimension."""
    dims = {
        "Exact Match":           scoring.exact_match,
        "Semantic Similarity":   scoring.semantic_similarity,
        "Achievement Impact":    scoring.achievement_impact,
        "Ownership/Leadership":  scoring.ownership_leadership,
    }
    worst_name, worst_dim = min(dims.items(), key=lambda kv: kv[1].score)
    return f"{worst_name} ({worst_dim.score:.0f}/100): {worst_dim.explanation[:100]}…"


def _evaluate_one(resume: ParsedResume, jd: JobDescription) -> ScoringResult:
    """Score a single candidate. Called in a worker thread."""
    return score_candidate(resume, jd)


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────

def batch_evaluate(
    resumes: list[ParsedResume],
    jd: JobDescription,
    max_workers: int = DEFAULT_BATCH_WORKERS,
) -> BatchResult:
    """
    Evaluate multiple candidates against a single Job Description in parallel.

    Parameters
    ----------
    resumes : list[ParsedResume]
        List of parsed candidate resumes.
    jd : JobDescription
        The job description to evaluate against.
    max_workers : int
        Number of concurrent evaluation threads (default 5).

    Returns
    -------
    BatchResult
        Ranked leaderboard sorted by composite score (descending).
    """
    logger.info(
        "Batch evaluation: %d candidates × '%s' (workers=%d)",
        len(resumes), jd.title, max_workers,
    )

    scored: list[ScoringResult] = []
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_name = {
            pool.submit(_evaluate_one, resume, jd): resume.candidate_name
            for resume in resumes
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result()
                scored.append(result)
                logger.info("  ✓ %s → %.1f (%s)", name, result.composite_score, result.tier.value)
            except Exception as exc:
                logger.error("  ✗ %s failed: %s", name, exc)
                failed.append(f"{name}: {exc}")

    # ── Sort by composite score descending ────────────────────────────────
    scored.sort(key=lambda s: s.composite_score, reverse=True)

    leaderboard = [
        CandidateRank(
            rank=i + 1,
            candidate_name=s.candidate_name,
            composite_score=s.composite_score,
            tier=s.tier,
            tier_rationale=s.tier_rationale,
            top_signal=_top_signal(s),
            biggest_gap=_biggest_gap(s),
            scoring=s,
        )
        for i, s in enumerate(scored)
    ]

    tier_counts = {Tier.A: 0, Tier.B: 0, Tier.C: 0}
    for s in scored:
        tier_counts[s.tier] = tier_counts.get(s.tier, 0) + 1

    return BatchResult(
        jd_title=jd.title,
        total_candidates=len(resumes),
        tier_a_count=tier_counts[Tier.A],
        tier_b_count=tier_counts[Tier.B],
        tier_c_count=tier_counts[Tier.C],
        leaderboard=leaderboard,
        failed_candidates=failed,
    )