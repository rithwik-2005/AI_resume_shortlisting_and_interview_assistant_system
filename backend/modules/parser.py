"""
modules/parser.py — Resume & Job Description parser.

Takes raw extracted text (from file_extractor) and uses OpenAI to
convert it into structured ParsedResume / JobDescription objects.

Uses gpt-4o-mini for cost efficiency since extraction is straightforward.
The `response_format=json_object` mode in llm_client guarantees valid JSON.
"""

from __future__ import annotations

import logging

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
  "linkedin_url": string or null
}

Rules:
- Use null or [] when information is absent — never invent data.
- Infer total_experience_years from all work history durations combined.
- Extract skills from EVERYWHERE: bullet points, project descriptions, certifications.
- Split skills into the three buckets: pure skills (e.g. "Machine Learning"),
  tools/tech (e.g. "Docker", "AWS S3"), programming languages (e.g. "Python").
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
# Public API
# ──────────────────────────────────────────────

def parse_resume(raw_text: str) -> ParsedResume:
    """
    Parse raw resume text into a structured ParsedResume.

    Parameters
    ----------
    raw_text : str
        Plain text extracted from the resume file.

    Returns
    -------
    ParsedResume
    """
    logger.info("Parsing resume text (%d chars)…", len(raw_text))

    data = chat_json(
        system=_RESUME_SYSTEM,
        user=f"Parse this resume:\n\n{raw_text}",
        use_mini=True,    # extraction task → use cheaper model
        max_tokens=2000,
    )

    if not data:
        logger.warning("Resume parse returned empty — using defaults.")
        return ParsedResume(candidate_name="Unknown Candidate", raw_text=raw_text)

    # --- Normalise nested objects into Pydantic models ---
    data["work_experience"] = [
        WorkExperience(**w) if isinstance(w, dict) else w
        for w in data.get("work_experience", [])
    ]
    data["projects"] = [
        Project(**p) if isinstance(p, dict) else p
        for p in data.get("projects", [])
    ]
    data["education"] = [
        Education(**e) if isinstance(e, dict) else e
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