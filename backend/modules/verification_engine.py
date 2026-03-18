"""
modules/verification_engine.py — Option B: Claim Verification Engine.

GitHub  → Public REST API (set GITHUB_TOKEN for 5 000 req/hr vs 60).
LinkedIn → Lightweight URL + slug consistency check (LLM-assisted).

Authenticity score (0–100) aggregates both signals.
Red flags are returned as human-readable strings.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from models import (
    GitHubVerification,
    LinkedInVerification,
    ParsedResume,
    VerificationResult,
)
from modules.llm_client import chat_text
from config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _github_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


def _extract_github_username(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else None


def _days_since(iso: str) -> int:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


# ──────────────────────────────────────────────
# GitHub
# ──────────────────────────────────────────────

def _verify_github(url: Optional[str]) -> Optional[GitHubVerification]:
    if not url:
        return None

    username = _extract_github_username(url)
    if not username:
        return GitHubVerification(
            profile_found=False,
            flags=["Could not parse GitHub username from URL"],
        )

    flags: list[str] = []

    try:
        with httpx.Client(timeout=12) as http:

            # ── User profile ──────────────────────────────────────────────
            resp = http.get(f"{GITHUB_API}/users/{username}", headers=_github_headers())
            if resp.status_code == 404:
                return GitHubVerification(
                    profile_found=False,
                    username=username,
                    flags=["GitHub profile not found — URL may be fabricated"],
                )
            if resp.status_code != 200:
                return GitHubVerification(
                    profile_found=False,
                    username=username,
                    flags=[f"GitHub API returned HTTP {resp.status_code}"],
                )

            user = resp.json()
            account_age_days = _days_since(user.get("created_at", ""))
            public_repos: int = user.get("public_repos", 0)
            followers: int = user.get("followers", 0)

            if account_age_days < 30:
                flags.append("Account is less than 30 days old — possible throwaway")
            if public_repos == 0:
                flags.append("Zero public repositories")
            if followers == 0 and public_repos > 5:
                flags.append("Many repos but zero followers — may be private or inactive")

            # ── Recent commits (via events) ───────────────────────────────
            events_resp = http.get(
                f"{GITHUB_API}/users/{username}/events/public",
                headers=_github_headers(),
                params={"per_page": 100},
            )
            recent_commits_30d = 0
            if events_resp.status_code == 200:
                cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
                for event in events_resp.json():
                    if event.get("type") == "PushEvent":
                        try:
                            ts = datetime.fromisoformat(
                                event["created_at"].replace("Z", "+00:00")
                            ).timestamp()
                            if ts > cutoff:
                                recent_commits_30d += len(
                                    event.get("payload", {}).get("commits", [])
                                )
                        except Exception:
                            pass

            if recent_commits_30d == 0:
                flags.append("No GitHub commits in the past 30 days")

            # ── Top languages ─────────────────────────────────────────────
            repos_resp = http.get(
                f"{GITHUB_API}/users/{username}/repos",
                headers=_github_headers(),
                params={"per_page": 30, "sort": "pushed"},
            )
            lang_counts: dict[str, int] = {}
            if repos_resp.status_code == 200:
                for repo in repos_resp.json():
                    lang = repo.get("language")
                    if lang:
                        lang_counts[lang] = lang_counts.get(lang, 0) + 1
            top_languages = sorted(lang_counts, key=lang_counts.get, reverse=True)[:5]  # type: ignore[arg-type]

    except httpx.RequestError as exc:
        logger.warning("GitHub request error: %s", exc)
        return GitHubVerification(
            profile_found=False,
            username=username,
            flags=["GitHub API unreachable — network error"],
        )

    # ── Activity score (0–100) ────────────────────────────────────────────
    score = min(40.0, public_repos * 2)               # repos: up to 40 pts
    score += min(20.0, followers * 0.5)                # followers: up to 20 pts
    score += min(25.0, recent_commits_30d * 1.5)       # recent commits: up to 25 pts
    score += min(15.0, (account_age_days / 365) * 5)   # account age: up to 15 pts
    score = round(min(score, 100), 1)

    summary = (
        f"GitHub @{username}: {public_repos} repos, {followers} followers, "
        f"{recent_commits_30d} commits in last 30 days. "
        f"Activity score: {score}/100. "
        + (f"Flags: {'; '.join(flags)}." if flags else "No red flags.")
    )

    return GitHubVerification(
        profile_found=True,
        username=username,
        public_repos=public_repos,
        followers=followers,
        recent_commits_30d=recent_commits_30d,
        top_languages=top_languages,
        account_age_days=account_age_days,
        activity_score=score,
        flags=flags,
        summary=summary,
    )


# ──────────────────────────────────────────────
# LinkedIn
# ──────────────────────────────────────────────

def _verify_linkedin(url: Optional[str], resume: ParsedResume) -> Optional[LinkedInVerification]:
    if not url:
        return None

    flags: list[str] = []

    if "linkedin.com/in/" not in url:
        return LinkedInVerification(
            profile_found=False,
            flags=["URL does not match linkedin.com/in/<slug> format"],
            summary="LinkedIn URL format is unexpected — manual review recommended.",
        )

    # Attempt a lightweight fetch (LinkedIn often blocks; we flag but don't fail)
    profile_found = False
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as http:
            r = http.get(url, headers={"User-Agent": "Mozilla/5.0"})
            profile_found = r.status_code == 200
            if r.status_code == 999:
                flags.append("LinkedIn blocked the request — manual review needed")
    except Exception:
        flags.append("Could not reach LinkedIn (network or anti-scrape)")

    # LLM slug-to-name consistency check
    slug = url.rstrip("/").split("/")[-1]
    reply = chat_text(
        system="You check if a LinkedIn URL slug plausibly belongs to a person. Reply YES or NO and one sentence reason.",
        user=f"Slug: '{slug}' — Person name: '{resume.candidate_name}'. Does this slug plausibly match?",
        use_mini=True,
        max_tokens=80,
    )
    if reply.upper().startswith("NO"):
        flags.append(f"LinkedIn slug '{slug}' may not match '{resume.candidate_name}'")

    summary = (
        f"LinkedIn profile {'accessible' if profile_found else 'not confirmed'} at {url}. "
        + (f"Flags: {'; '.join(flags)}." if flags else "No consistency issues.")
    )

    return LinkedInVerification(
        profile_found=profile_found,
        profile_url=url,
        flags=flags,
        summary=summary,
    )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def verify_candidate(resume: ParsedResume) -> VerificationResult:
    """
    Verify GitHub and LinkedIn claims from the parsed resume.

    Parameters
    ----------
    resume : ParsedResume

    Returns
    -------
    VerificationResult
    """
    logger.info("Verifying claims for: %s", resume.candidate_name)

    github = _verify_github(resume.github_url)
    linkedin = _verify_linkedin(resume.linkedin_url, resume)

    scores: list[float] = []
    all_flags: list[str] = []

    if github:
        scores.append(github.activity_score if github.profile_found else 10.0)
        all_flags.extend(github.flags)

    if linkedin:
        li_score = 70.0 if linkedin.profile_found else 20.0
        li_score -= len(linkedin.flags) * 15
        scores.append(max(0.0, li_score))
        all_flags.extend(linkedin.flags)

    if not scores:
        overall = 50.0
        all_flags.append("No social/GitHub profiles provided for verification")
    else:
        overall = round(sum(scores) / len(scores), 1)

    summary_parts = [
        p for p in [
            github.summary if github else None,
            linkedin.summary if linkedin else None,
        ] if p
    ] or ["No public profiles were provided for verification."]

    return VerificationResult(
        candidate_name=resume.candidate_name,
        github=github,
        linkedin=linkedin,
        overall_authenticity_score=overall,
        red_flags=all_flags,
        verification_summary=" | ".join(summary_parts),
    )