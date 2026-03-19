"""
modules/parser.py — Resume & Job Description parser.

Takes raw extracted text (from file_extractor) and uses OpenAI to
convert it into structured ParsedResume / JobDescription objects.

The extracted text now includes a "--- HYPERLINKS FOUND IN DOCUMENT ---"
section appended by file_extractor.py.  The LLM prompt is explicitly
instructed to read that section first when looking for profile URLs.
"""

from __future__ import annotations

import logging
import re

from models import Education, JobDescription, ParsedResume, Project, WorkExperience
from modules.llm_client import chat_json

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# System prompts
# ──────────────────────────────────────────────

_RESUME_SYSTEM = """
You are a precise resume-parsing engine. Extract all information from the resume
text and return a single JSON object with EXACTLY these keys:

{
  "candidate_name": string,
  "contact_email": string or null,
  "total_experience_years": number,
  "current_role": string or null,
  "skills": [string],
  "tools_and_technologies": [string],
  "programming_languages": [string],
  "education": [{"degree": string, "institution": string, "year": string}],
  "work_experience": [
    {"role": string, "company": string, "duration": string, "bullets": [string]}
  ],
  "projects": [{"name": string, "description": string, "stack": [string]}],
  "certifications": [string],
  "github_url": string or null,
  "linkedin_url": string or null,
  "leetcode_url": string or null,
  "other_links": [string]
}

===== CRITICAL RULES FOR EXTRACTING PROFILE LINKS =====

The text may contain a section at the bottom labelled:
  "--- HYPERLINKS FOUND IN DOCUMENT ---"

This section lists real URLs extracted from invisible hyperlinks in the file
(e.g., a word like "GitHub" or "LinkedIn" that was a clickable link).

PRIORITY ORDER for finding URLs:
1. Look in the "HYPERLINKS FOUND" section FIRST — these are the actual URLs.
2. Then look in the visible text for written-out URLs (https://...).
3. Then look for bare domain patterns like "github.com/username".

Rules for each URL field:
- github_url    : any URL containing "github.com"  (full URL including path)
- linkedin_url  : any URL containing "linkedin.com" (full URL including path)
- leetcode_url  : any URL containing "leetcode.com"
- other_links   : ALL other profile/portfolio URLs found
                  (LeetCode if already in leetcode_url can be omitted,
                   but include HackerRank, Kaggle, CodeChef, Codeforces,
                   Medium, StackOverflow, GitLab, portfolio sites, etc.)

NEVER return null for github_url or linkedin_url if a matching URL appears
ANYWHERE in the text, including in the HYPERLINKS section.

Always return the full URL including https:// prefix.
If a URL appears without https://, add it: "github.com/user" → "https://github.com/user"

===== OTHER EXTRACTION RULES =====

- Use null or [] when information is genuinely absent — never invent data.
- Infer total_experience_years from all work history durations combined.
- Extract skills from EVERYWHERE: bullet points, project descriptions, certifications.
- Split skills into three buckets:
    skills                → pure skills (e.g. "Machine Learning", "REST APIs")
    tools_and_technologies → tools/platforms (e.g. "Docker", "AWS S3", "PostgreSQL")
    programming_languages  → languages only (e.g. "Python", "JavaScript", "Java")
- Return ONLY the JSON object, nothing else.
"""

_JD_SYSTEM = """
You are a job-description analysis engine. Extract structured information and
return a single JSON object with EXACTLY these keys:

{
  "title": string,
  "required_skills": [string],
  "preferred_skills": [string],
  "required_experience_years": number,
  "responsibilities": [string],
  "domain_keywords": [string]
}

Rules:
- required_skills: explicitly mandatory (must-have).
- preferred_skills: nice-to-have / bonus.
- domain_keywords: business/technical domain tags (e.g. "fintech", "real-time", "ML platform").
- If experience is a range, use the minimum.
- Return ONLY the JSON object.
"""


# ──────────────────────────────────────────────
# Post-processing helpers
# ──────────────────────────────────────────────

def _ensure_https(url: str | None) -> str | None:
    """Add https:// prefix to bare domain URLs if missing."""
    if not url:
        return None
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def _fallback_extract_urls(raw_text: str) -> dict:
    """
    Last-resort regex extraction of profile URLs directly from text.
    Used to patch up any links the LLM missed.
    """
    result = {
        "github_url": None,
        "linkedin_url": None,
        "leetcode_url": None,
        "other_links": [],
    }

    # Extract all https:// URLs from the text
    all_urls = re.findall(r'https?://[^\s\)\]>,"\'<]+', raw_text)

    # Also catch bare domain URLs
    bare = re.findall(
        r'(?<!\w)((?:github|linkedin|leetcode|kaggle|hackerrank|'
        r'stackoverflow|medium|gitlab|codechef|codeforces|bitbucket)'
        r'\.(?:com|org|net|io)/[^\s\)\]>,"\'<]+)',
        raw_text,
        re.IGNORECASE,
    )
    all_urls.extend(["https://" + u for u in bare])

    seen = set()
    for url in all_urls:
        url = url.rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        url_lower = url.lower()
        if "github.com" in url_lower and not result["github_url"]:
            result["github_url"] = url
        elif "linkedin.com" in url_lower and not result["linkedin_url"]:
            result["linkedin_url"] = url
        elif "leetcode.com" in url_lower and not result["leetcode_url"]:
            result["leetcode_url"] = url
        elif any(d in url_lower for d in (
            "kaggle.com", "hackerrank.com", "stackoverflow.com",
            "medium.com", "gitlab.com", "codechef.com", "codeforces.com",
            "bitbucket.org", "behance.net", "dribbble.com",
        )):
            result["other_links"].append(url)

    return result


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def parse_resume(raw_text: str) -> ParsedResume:
    """
    Parse raw resume text (including the HYPERLINKS section) into
    a structured ParsedResume with all profile URLs properly extracted.

    Parameters
    ----------
    raw_text : str
        Plain text + hyperlinks section from file_extractor.

    Returns
    -------
    ParsedResume
    """
    logger.info("Parsing resume text (%d chars)…", len(raw_text))

    data = chat_json(
        system=_RESUME_SYSTEM,
        user=f"Parse this resume:\n\n{raw_text}",
        use_mini=True,
        max_tokens=2500,
    )

    if not data:
        logger.warning("Resume parse returned empty — using defaults.")
        return ParsedResume(candidate_name="Unknown Candidate", raw_text=raw_text)

    # ── Ensure https:// prefix on all URL fields ──────────────────────────
    data["github_url"]   = _ensure_https(data.get("github_url"))
    data["linkedin_url"] = _ensure_https(data.get("linkedin_url"))

    # ── Fallback: if LLM missed any links, grab them directly from text ───
    fallback = _fallback_extract_urls(raw_text)

    if not data.get("github_url") and fallback["github_url"]:
        logger.info("Fallback: recovered GitHub URL: %s", fallback["github_url"])
        data["github_url"] = fallback["github_url"]

    if not data.get("linkedin_url") and fallback["linkedin_url"]:
        logger.info("Fallback: recovered LinkedIn URL: %s", fallback["linkedin_url"])
        data["linkedin_url"] = fallback["linkedin_url"]

    # Log what we found
    logger.info(
        "Links extracted — GitHub: %s | LinkedIn: %s | LeetCode: %s | Other: %s",
        data.get("github_url") or "NOT FOUND",
        data.get("linkedin_url") or "NOT FOUND",
        data.get("leetcode_url") or "NOT FOUND",
        len(data.get("other_links", [])),
    )

    # ── Normalise nested objects into Pydantic models ─────────────────────
    # Replace any None values with empty strings so str fields don't fail
    def _clean(d: dict, defaults: dict) -> dict:
        return {k: (v if v is not None else defaults.get(k, "")) for k, v in d.items()}

    data["work_experience"] = [
        WorkExperience(**_clean(w, {"role": "", "company": "", "duration": ""}))
        if isinstance(w, dict) else w
        for w in data.get("work_experience", [])
    ]
    data["projects"] = [
        Project(**_clean(p, {"name": "", "description": ""}))
        if isinstance(p, dict) else p
        for p in data.get("projects", [])
    ]
    data["education"] = [
        Education(**_clean(e, {"degree": "", "institution": ""}))
        if isinstance(e, dict) else e
        for e in data.get("education", [])
    ]
    data["raw_text"] = raw_text

    # Only pass fields that exist in ParsedResume
    valid_fields = ParsedResume.model_fields.keys()
    return ParsedResume(**{k: v for k, v in data.items() if k in valid_fields})


def parse_jd(raw_text: str) -> JobDescription:
    """
    Parse raw JD text into a structured JobDescription.

    Parameters
    ----------
    raw_text : str
        Plain text extracted from the JD file.

    Returns
    -------
    JobDescription
    """
    logger.info("Parsing JD text (%d chars)…", len(raw_text))

    data = chat_json(
        system=_JD_SYSTEM,
        user=f"Parse this job description:\n\n{raw_text}",
        use_mini=True,
        max_tokens=1000,
    )

    if not data:
        logger.warning("JD parse returned empty — using defaults.")
        return JobDescription(raw_text=raw_text)

    data["raw_text"] = raw_text
    valid_fields = JobDescription.model_fields.keys()
    return JobDescription(**{k: v for k, v in data.items() if k in valid_fields})