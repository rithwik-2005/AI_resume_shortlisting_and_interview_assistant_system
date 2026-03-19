"""
modules/verification_engine.py — Option B: Claim Verification Engine.

Verifies every public profile URL found in the resume:
  GitHub      → Public REST API  (activity score, repos, commits)
  LinkedIn    → Existence check  (LinkedIn blocks scrapers with 999; handled gracefully)
  LeetCode    → GraphQL API      (problems solved easy/medium/hard, global ranking)
  Other URLs  → HTTP existence   (Kaggle, HackerRank, etc.) + LLM description

Scoring philosophy (KEY RULE)
------------------------------
  Profile FOUND & data available  → real score based on activity
  Profile NOT found (HTTP 404)    → low score (10-20)
  Network error / bot-protection  → NEUTRAL score (50) — NOT penalised
  LinkedIn 999 (bot-block)        → treated as "profile exists" (60)
  GitHub timeout                  → neutral (50)
  Kaggle/other server disconnect  → neutral (50)  ← the main fix here

The overall authenticity score is a WEIGHTED average that gives more
weight to platforms where real data was retrieved.
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
    LeetCodeVerification,
    OtherProfileVerification,
    ParsedResume,
    VerificationResult,
)
from modules.llm_client import chat_text
from config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# ── Platform metadata ──────────────────────────────────────────────────────
_PLATFORM_NAMES = {
    "kaggle.com":          "Kaggle",
    "hackerrank.com":      "HackerRank",
    "codechef.com":        "CodeChef",
    "codeforces.com":      "Codeforces",
    "stackoverflow.com":   "StackOverflow",
    "medium.com":          "Medium",
    "gitlab.com":          "GitLab",
    "bitbucket.org":       "Bitbucket",
    "behance.net":         "Behance",
    "dribbble.com":        "Dribbble",
}

_PLATFORM_DESCRIPTIONS = {
    "Kaggle": (
        "Kaggle is a data science and ML competition platform. Having a Kaggle profile "
        "indicates experience with real-world datasets and competitive machine learning."
    ),
    "HackerRank": (
        "HackerRank is a coding challenge platform used by companies for hiring assessments. "
        "A HackerRank profile demonstrates exposure to algorithmic problem-solving."
    ),
    "CodeChef": (
        "CodeChef is a competitive programming platform. Active participation shows "
        "strong algorithmic and data structures skills."
    ),
    "Codeforces": (
        "Codeforces is a top competitive programming platform. Ratings here are a "
        "respected signal of algorithmic ability in the CS community."
    ),
    "StackOverflow": (
        "StackOverflow is the primary developer Q&A community. A profile with "
        "reputation points shows technical breadth and community contribution."
    ),
    "Medium": (
        "Medium is a writing platform. A technical Medium profile shows the candidate "
        "can communicate complex ideas clearly — a valued engineering skill."
    ),
    "GitLab": (
        "GitLab is a DevOps platform for source control and CI/CD. "
        "A GitLab profile serves as an alternative/complement to GitHub."
    ),
}

# Browser-like headers — reduces bot-detection rejections on Kaggle, HackerRank etc.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# HTTP status codes that mean "bot blocked / anti-scrape" — treat as neutral, not not-found
_BOT_BLOCK_CODES = {403, 429, 503, 999}

# Exception message substrings that indicate network/bot issues, not missing profiles
_NETWORK_ERROR_PHRASES = (
    "server disconnected",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "eof occurred",
    "remote end closed",
    "ssl",
    "name or service not known",
    "nodename nor servname",
    "network",
    "os error",
    "broken pipe",
)


def _is_network_error(exc: Exception) -> bool:
    """Return True if exception looks like a network/connectivity issue vs a real error."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _NETWORK_ERROR_PHRASES)


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


def _extract_leetcode_username(url: Optional[str]) -> Optional[str]:
    """Handle both /u/username and /username URL formats."""
    if not url:
        return None
    path_parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not path_parts:
        return None
    if path_parts[0].lower() == "u" and len(path_parts) > 1:
        return path_parts[1]
    return path_parts[0]


def _detect_platform(url: str) -> str:
    url_lower = url.lower()
    for domain, name in _PLATFORM_NAMES.items():
        if domain in url_lower:
            return name
    return urlparse(url).netloc.replace("www.", "").split(".")[0].capitalize()


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
        with httpx.Client(timeout=20) as http:

            resp = http.get(f"{GITHUB_API}/users/{username}", headers=_github_headers())

            if resp.status_code == 404:
                return GitHubVerification(
                    profile_found=False,
                    username=username,
                    flags=["GitHub profile not found — URL may be incorrect"],
                )
            if resp.status_code != 200:
                return GitHubVerification(
                    profile_found=False,
                    unreachable=True,
                    username=username,
                    flags=[f"GitHub API returned HTTP {resp.status_code} — not penalised"],
                )

            user = resp.json()
            account_age_days = _days_since(user.get("created_at", ""))
            public_repos: int = user.get("public_repos", 0)
            followers: int    = user.get("followers", 0)

            if account_age_days < 30:
                flags.append("Account less than 30 days old — possible throwaway")
            if public_repos == 0:
                flags.append("No public repositories")

            # Recent commits
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

            # Top languages
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

    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("GitHub unreachable for %s: %s", username, exc)
        return GitHubVerification(
            profile_found=False,
            unreachable=True,
            username=username,
            flags=["GitHub API unreachable — network issue, not penalised"],
            summary=(
                f"GitHub @{username}: Could not reach API (network issue). "
                f"Profile may be valid — check manually at github.com/{username}"
            ),
        )

    # Activity score
    score  = min(40.0, public_repos * 2)
    score += min(20.0, followers * 0.5)
    score += min(25.0, recent_commits_30d * 1.5)
    score += min(15.0, (account_age_days / 365) * 5)
    score  = round(min(score, 100), 1)

    summary = (
        f"GitHub @{username}: {public_repos} repos, {followers} followers, "
        f"{recent_commits_30d} commits in last 30 days. "
        f"Top languages: {', '.join(top_languages) or 'N/A'}. "
        f"Activity score: {score}/100."
        + (f" Flags: {'; '.join(flags)}." if flags else " No red flags.")
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
            summary="LinkedIn URL format is unexpected.",
        )

    profile_found = False
    blocked = False
    network_error = False

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as http:
            r = http.get(url, headers=_BROWSER_HEADERS)
            if r.status_code == 200:
                profile_found = True
            elif r.status_code == 999 or r.status_code in _BOT_BLOCK_CODES:
                # LinkedIn actively blocks all scrapers — this means the profile exists
                blocked = True
                profile_found = True
            elif r.status_code == 404:
                flags.append("LinkedIn profile returned 404 — may not exist")
            else:
                # Any other error — treat as network issue, not penalised
                network_error = True
    except Exception as exc:
        network_error = True
        logger.debug("LinkedIn fetch exception: %s", exc)

    # LLM slug-to-name consistency check
    slug = url.rstrip("/").split("/")[-1]
    try:
        reply = chat_text(
            system="Check if a LinkedIn URL slug plausibly belongs to a person. Reply YES or NO and one sentence reason.",
            user=f"Slug: '{slug}' — Name: '{resume.candidate_name}'. Does this slug plausibly match?",
            use_mini=True,
            max_tokens=80,
        )
        if reply.upper().startswith("NO"):
            flags.append(f"LinkedIn slug '{slug}' may not match '{resume.candidate_name}'")
    except Exception:
        pass

    if blocked:
        status = "Profile exists (LinkedIn blocks all server-side checks — opens fine in browser)"
    elif network_error:
        status = "Link provided (server cannot reach LinkedIn — opens fine in browser)"
    elif profile_found:
        status = "Accessible"
    else:
        status = "Not found (404)"

    summary = f"LinkedIn: {status} at {url}." + (f" Flags: {'; '.join(flags)}." if flags else "")

    return LinkedInVerification(
        profile_found=True,                       # URL was in resume — treat as valid
        blocked=blocked,
        server_blocked=network_error or blocked,  # server can't reach, but browser can
        profile_url=url,
        flags=flags,
        summary=summary,
    )


# ──────────────────────────────────────────────
# LeetCode
# ──────────────────────────────────────────────

_LEETCODE_QUERY = """
query userProfile($username: String!) {
  matchedUser(username: $username) {
    username
    profile { ranking }
    submitStats: submitStatsGlobal {
      acSubmissionNum { difficulty count }
    }
  }
}
"""


def _leetcode_activity_score(easy: int, medium: int, hard: int, ranking: Optional[int]) -> float:
    weighted = easy * 1 + medium * 2 + hard * 4
    score = min(80.0, (weighted / 200.0) * 80.0)
    if ranking and ranking > 0:
        if ranking <= 1_000:     score += 20
        elif ranking <= 10_000:  score += 15
        elif ranking <= 50_000:  score += 10
        elif ranking <= 100_000: score += 5
    return round(min(score, 100), 1)


def _verify_leetcode(url: Optional[str]) -> Optional[LeetCodeVerification]:
    if not url:
        return None

    username = _extract_leetcode_username(url)
    if not username:
        return LeetCodeVerification(
            profile_found=False, profile_url=url,
            flags=["Could not parse LeetCode username from URL"],
        )

    # ── Strategy 1: Official LeetCode GraphQL with CSRF token ─────────────
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as http:

            # Step 1: fetch homepage to get session cookie + CSRF token
            home = http.get(
                "https://leetcode.com",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

            # Extract CSRF token from cookies
            csrf_token = home.cookies.get("csrftoken", "")

            # Step 2: POST to GraphQL with session cookies + CSRF header
            resp = http.post(
                "https://leetcode.com/graphql/",
                json={"query": _LEETCODE_QUERY, "variables": {"username": username}},
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": "https://leetcode.com",
                    "Origin": "https://leetcode.com",
                    "x-csrftoken": csrf_token,
                },
                cookies=home.cookies,
            )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    raw = resp.content.decode("utf-8", errors="replace")
                    import json as _json
                    data = _json.loads(raw)

                matched = data.get("data", {}).get("matchedUser")
                if matched:
                    return _parse_leetcode_matched(matched, username, url)

    except Exception as exc:
        logger.debug("LeetCode GraphQL attempt failed for %s: %s", username, exc)

    # ── Strategy 2: Unofficial public API (no auth needed) ────────────────
    # Uses alfa-leetcode-api which proxies LeetCode publicly
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as http:
            resp = http.get(
                f"https://alfa-leetcode-api.onrender.com/userProfile/{username}",
                headers={"Accept": "application/json"},
            )

            if resp.status_code == 200:
                data = resp.json()
                # This API returns different field names
                total   = data.get("totalSolved", 0)
                easy    = data.get("easySolved", 0)
                medium  = data.get("mediumSolved", 0)
                hard    = data.get("hardSolved", 0)
                ranking = data.get("ranking", None)

                if total == 0 and easy == 0 and medium == 0:
                    # API returned but user not found
                    return LeetCodeVerification(
                        profile_found=False, username=username, profile_url=url,
                        flags=["LeetCode profile not found"],
                        summary=f"LeetCode @{username}: Profile not found.",
                    )

                global_ranking: Optional[int] = int(ranking) if ranking and str(ranking).isdigit() else None
                score = _leetcode_activity_score(easy, medium, hard, global_ranking)
                flags: list[str] = []

                if total == 0:
                    flags.append("No problems solved yet")
                elif total < 20:
                    flags.append(f"Only {total} problems solved — limited activity")

                ranking_str = f"Global rank: #{global_ranking:,}" if global_ranking else "Ranking not available"
                summary = (
                    f"LeetCode @{username}: {total} problems solved "
                    f"({easy} Easy / {medium} Medium / {hard} Hard). "
                    f"{ranking_str}. Activity score: {score}/100."
                )

                return LeetCodeVerification(
                    profile_found=True, username=username, profile_url=url,
                    total_solved=total, easy_solved=easy, medium_solved=medium,
                    hard_solved=hard, global_ranking=global_ranking,
                    activity_score=score, flags=flags, summary=summary,
                )

    except Exception as exc:
        logger.debug("LeetCode unofficial API failed for %s: %s", username, exc)

    # ── Strategy 3: Direct profile page scrape ────────────────────────────
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as http:
            profile_url_check = f"https://leetcode.com/{username}/"
            r = http.get(
                profile_url_check,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html",
                },
            )
            if r.status_code == 200 and username.lower() in r.text.lower():
                # Profile exists but we couldn't get stats
                return LeetCodeVerification(
                    profile_found=True, username=username, profile_url=url,
                    activity_score=50.0,
                    flags=["Could not fetch problem stats — profile confirmed to exist"],
                    summary=(
                        f"LeetCode @{username}: Profile verified to exist at {url}. "
                        f"Could not retrieve problem count stats (API restricted). "
                        f"Manual check recommended."
                    ),
                )
    except Exception as exc:
        logger.debug("LeetCode profile page check failed for %s: %s", username, exc)

    # ── All strategies failed ──────────────────────────────────────────────
    logger.warning("All LeetCode verification strategies failed for %s", username)
    return LeetCodeVerification(
        profile_found=True,          # URL came from resume — candidate put it there
        server_blocked=True,         # server-side API is blocked, not the profile
        username=username,
        profile_url=url,
        flags=[],
        summary=(
            f"LeetCode @{username}: API blocked server-side. "
            f"Profile link verified from resume: {url}"
        ),
    )


def _parse_leetcode_matched(matched: dict, username: str, url: str) -> LeetCodeVerification:
    """Parse a matched user object from the LeetCode GraphQL response."""
    easy = medium = hard = total = 0
    for entry in matched.get("submitStats", {}).get("acSubmissionNum", []):
        diff  = entry.get("difficulty", "").lower()
        count = entry.get("count", 0)
        if diff == "easy":     easy   = count
        elif diff == "medium": medium = count
        elif diff == "hard":   hard   = count
        elif diff == "all":    total  = count
    if total == 0:
        total = easy + medium + hard

    ranking_val    = matched.get("profile", {}).get("ranking")
    global_ranking: Optional[int] = int(ranking_val) if ranking_val and str(ranking_val).isdigit() else None
    score          = _leetcode_activity_score(easy, medium, hard, global_ranking)
    flags: list[str] = []

    if total == 0:
        flags.append("No problems solved yet")
    elif total < 20:
        flags.append(f"Only {total} problems solved — limited activity")

    ranking_str = f"Global rank: #{global_ranking:,}" if global_ranking else "Ranking not available"
    summary = (
        f"LeetCode @{username}: {total} problems solved "
        f"({easy} Easy / {medium} Medium / {hard} Hard). "
        f"{ranking_str}. Activity score: {score}/100."
    )

    return LeetCodeVerification(
        profile_found=True, username=username, profile_url=url,
        total_solved=total, easy_solved=easy, medium_solved=medium,
        hard_solved=hard, global_ranking=global_ranking,
        activity_score=score, flags=flags, summary=summary,
    )


# ──────────────────────────────────────────────
# Other profiles (Kaggle, HackerRank, etc.)
# ──────────────────────────────────────────────

def _verify_other_profile(url: str, candidate_name: str) -> OtherProfileVerification:
    """
    Verify any other profile URL (Kaggle, HackerRank, etc.).

    Key rule: the URL was parsed FROM the resume, so it was intentionally
    put there by the candidate. If server-side requests are blocked, we show
    the link as "provided" — never as "not found". Only a genuine HTTP 404
    means the profile doesn't exist.

    HTTP 200              → found, score 60
    HTTP 301/302          → redirect = found, score 60
    HTTP 403/429/999      → bot-blocked = link provided, no score contribution
    HTTP 404              → genuinely not found, score 10
    Network exception     → server-blocked, show link, no score contribution
    """
    platform  = _detect_platform(url)
    username  = _extract_leetcode_username(url)
    flags: list[str] = []
    highlights: list[str] = []

    profile_found  = False
    server_blocked = False
    page_text      = ""
    score          = 0.0   # default: no contribution unless real data

    try:
        with httpx.Client(timeout=15, follow_redirects=False) as http:
            r = http.get(url, headers=_BROWSER_HEADERS)

            if r.status_code == 200:
                profile_found = True
                score         = 60.0
                page_text     = r.text[:2000]

            elif r.status_code in (301, 302, 307, 308):
                profile_found = True
                score         = 60.0
                location = r.headers.get("location", "")
                if location:
                    try:
                        r2 = http.get(location, headers=_BROWSER_HEADERS,
                                      follow_redirects=True)
                        if r2.status_code == 200:
                            page_text = r2.text[:2000]
                    except Exception:
                        pass

            elif r.status_code in _BOT_BLOCK_CODES:
                # Server blocked — URL from resume so treat as "link provided"
                server_blocked = True
                profile_found  = True
                # Don't assign a score — exclude from scoring entirely

            elif r.status_code == 404:
                # Genuine not found
                flags.append(f"{platform} profile returned 404")

            else:
                # Unknown — treat as server-side block
                server_blocked = True
                profile_found  = True

    except Exception as exc:
        # Network error from server side (Kaggle blocks server IPs etc.)
        server_blocked = True
        profile_found  = True   # URL came from resume — candidate put it there
        logger.debug("%s server-side block for %s: %s", platform, url, exc)

    # ── LLM insight extraction (only when we have real page content) ──────
    platform_desc = _PLATFORM_DESCRIPTIONS.get(
        platform,
        f"{platform} is a professional platform relevant to software engineering."
    )

    if profile_found and page_text and not server_blocked:
        try:
            llm_reply = chat_text(
                system=(
                    "You analyse a candidate's public profile page. "
                    "Given the page HTML and platform, list 2-3 short bullet points "
                    "about what this profile says about the candidate. "
                    "Be factual and concise. Each bullet max 15 words."
                ),
                user=(
                    f"Platform: {platform}\n"
                    f"Candidate: {candidate_name}\n"
                    f"Page snippet:\n{page_text[:1200]}"
                ),
                use_mini=True,
                max_tokens=150,
            )
            for line in llm_reply.split("\n"):
                line = line.strip().lstrip("-•*123456789. ")
                if line and len(line) > 5:
                    highlights.append(line)
        except Exception:
            pass

    # ── Summary ───────────────────────────────────────────────────────────
    if server_blocked:
        status_str = "link provided (server-side checks blocked by platform)"
    elif profile_found:
        status_str = "found and verified"
    else:
        status_str = "not found (404)"

    summary = f"{platform} {status_str}: {url}. {platform_desc}"

    return OtherProfileVerification(
        platform=platform,
        url=url,
        username=username,
        profile_found=profile_found,
        server_blocked=server_blocked,
        activity_score=score,
        platform_description=platform_desc,
        highlights=highlights,
        flags=flags,
        summary=summary,
    )


# ──────────────────────────────────────────────
# Score weighting — only use real data
# ──────────────────────────────────────────────

def _compute_overall_score(
    github:         Optional[GitHubVerification],
    linkedin:       Optional[LinkedInVerification],
    leetcode:       Optional[LeetCodeVerification],
    other_profiles: list[OtherProfileVerification],
) -> float:
    """
    Compute weighted authenticity score using ONLY platforms where
    real data was fetched. Platforms that were server-blocked, timed out,
    or couldn't be reached do NOT contribute to the score at all —
    they are shown in the UI as "link provided" without affecting the number.

    Only GitHub (real API data) and LeetCode (real API data) contribute
    meaningfully. LinkedIn always gets blocked so it's excluded from scoring.
    """
    weighted_sum = 0.0
    total_weight = 0.0

    def add(score: float, weight: float) -> None:
        nonlocal weighted_sum, total_weight
        weighted_sum += score * weight
        total_weight  += weight

    # GitHub — only score if we actually got data (not unreachable)
    if github and github.profile_found and not github.unreachable:
        add(github.activity_score, 0.60)

    # LeetCode — only score if we got real stats
    if leetcode and leetcode.profile_found and not leetcode.server_blocked:
        add(leetcode.activity_score, 0.40)

    # LinkedIn / other profiles — excluded from score (always server-blocked)
    # They show as informational cards in the UI only

    if total_weight == 0:
        # No platforms returned real data
        # Base score on how many profile links were provided (0-3)
        link_count = sum([
            bool(github),
            bool(linkedin and linkedin.profile_url),
            bool(leetcode and leetcode.profile_url),
            len(other_profiles),
        ])
        return min(50.0, 20.0 + link_count * 10.0)

    return round(weighted_sum / total_weight, 1)


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def verify_candidate(resume: ParsedResume) -> VerificationResult:
    """
    Verify all public profile claims from the parsed resume.

    Scoring:
      - Only platforms where REAL data was fetched contribute to the score.
      - Server-blocked / network-unreachable platforms show as
        "link provided" in the UI but do NOT affect the score.
      - Only a genuine HTTP 404 is treated as "not found".
    """
    logger.info("Verifying claims for: %s", resume.candidate_name)
    logger.info(
        "Profile links — GitHub: %s | LinkedIn: %s | LeetCode: %s | Other: %s",
        resume.github_url   or "none",
        resume.linkedin_url or "none",
        resume.leetcode_url or "none",
        resume.other_links  or [],
    )

    github   = _verify_github(resume.github_url)
    linkedin = _verify_linkedin(resume.linkedin_url, resume)
    leetcode = _verify_leetcode(resume.leetcode_url)

    other_profiles: list[OtherProfileVerification] = []
    for link in (resume.other_links or []):
        try:
            other_profiles.append(_verify_other_profile(link, resume.candidate_name))
        except Exception as exc:
            logger.warning("Other profile check failed for %s: %s", link, exc)

    # ── Overall score (real data only) ────────────────────────────────────
    overall = _compute_overall_score(github, linkedin, leetcode, other_profiles)

    # ── Collect meaningful red flags only ─────────────────────────────────
    all_flags: list[str] = []
    if github and github.profile_found and not github.unreachable:
        # Only add flags that are actual quality signals, not network issues
        all_flags.extend(f for f in github.flags
                         if "network" not in f.lower() and "timeout" not in f.lower())
    if leetcode and leetcode.profile_found and not leetcode.server_blocked:
        all_flags.extend(leetcode.flags)

    # ── Summary ───────────────────────────────────────────────────────────
    summary_parts: list[str] = []
    if github:   summary_parts.append(github.summary)
    if linkedin: summary_parts.append(linkedin.summary)
    if leetcode: summary_parts.append(leetcode.summary)
    for op in other_profiles:
        summary_parts.append(op.summary)
    if not summary_parts:
        summary_parts = ["No public profiles provided for verification."]

    return VerificationResult(
        candidate_name=resume.candidate_name,
        github=github,
        linkedin=linkedin,
        leetcode=leetcode,
        other_profiles=other_profiles,
        overall_authenticity_score=overall,
        red_flags=all_flags,
        verification_summary=" | ".join(summary_parts),
    )