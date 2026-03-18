"""
modules/streamer.py — Server-Sent Events (SSE) helper for real-time progress.

Yields JSON-encoded SSE events while the pipeline runs.
The frontend listens on a single EventSource connection and renders
each step as it completes — no polling required.

SSE format
----------
Each event is two lines:
    data: <json>\n\n

Event types (the `event` field in the JSON payload)
------------------------------------------------------
progress  — step started or completed
result    — a pipeline sub-result is ready (resume, jd, scoring, etc.)
done      — pipeline finished; payload contains the full PipelineResult
error     — an error occurred; payload contains the message
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)


def _sse(event: str, data: Any) -> str:
    """Format a single SSE message."""
    payload = json.dumps({"event": event, "data": data}, default=str)
    return f"data: {payload}\n\n"


def _progress(step: str, status: str, message: str) -> str:
    """Emit a progress event."""
    return _sse("progress", {"step": step, "status": status, "message": message})


def pipeline_stream(
    resume_text: str,
    jd_text: str,
    run_verification: bool = True,
    run_questions: bool = True,
) -> Generator[str, None, None]:
    """
    Run the full evaluation pipeline and yield SSE strings at each step.

    Designed to be used as a FastAPI StreamingResponse generator:

        return StreamingResponse(
            pipeline_stream(resume_text, jd_text),
            media_type="text/event-stream",
        )

    Parameters
    ----------
    resume_text : str       Extracted resume plain text.
    jd_text : str           Extracted JD plain text.
    run_verification : bool
    run_questions : bool

    Yields
    ------
    str
        SSE-formatted event strings.
    """
    from modules.parser import parse_jd, parse_resume
    from modules.scoring_engine import score_candidate
    from modules.verification_engine import verify_candidate
    from modules.question_generator import generate_interview_plan
    from models import PipelineResult

    try:
        # ── Step 1: Parse ─────────────────────────────────────────────────
        yield _progress("parse", "running", "Parsing resume & job description with GPT-4o-mini…")
        resume = parse_resume(resume_text)
        jd     = parse_jd(jd_text)
        yield _progress("parse", "done", f"Parsed: {resume.candidate_name} → {jd.title}")
        yield _sse("result", {"key": "resume", "value": resume.model_dump()})
        yield _sse("result", {"key": "jd",     "value": jd.model_dump()})

        # ── Step 2: Score ─────────────────────────────────────────────────
        yield _progress("score", "running", "Running 4 scoring dimensions in parallel…")
        scoring = score_candidate(resume, jd)
        yield _progress(
            "score", "done",
            f"Score: {scoring.composite_score}/100 → Tier {scoring.tier.value}",
        )
        yield _sse("result", {"key": "scoring", "value": scoring.model_dump()})

        # ── Step 3: Verify ────────────────────────────────────────────────
        verification = None
        if run_verification:
            yield _progress("verify", "running", "Checking GitHub & LinkedIn profiles…")
            verification = verify_candidate(resume)
            flag_count = len(verification.red_flags)
            yield _progress(
                "verify", "done",
                f"Authenticity: {verification.overall_authenticity_score}/100 "
                f"({flag_count} flag{'s' if flag_count != 1 else ''})",
            )
            yield _sse("result", {"key": "verification", "value": verification.model_dump()})
        else:
            yield _progress("verify", "skipped", "Verification skipped")

        # ── Step 4: Interview plan ─────────────────────────────────────────
        interview_plan = None
        if run_questions:
            yield _progress("questions", "running", "Generating tailored interview plan…")
            interview_plan = generate_interview_plan(
                resume=resume,
                jd=jd,
                scoring=scoring,
                verification_summary=verification.verification_summary if verification else "",
                red_flags=verification.red_flags if verification else [],
            )
            yield _progress(
                "questions", "done",
                f"Generated {len(interview_plan.questions)} questions for Tier {interview_plan.tier.value}",
            )
            yield _sse("result", {"key": "interview_plan", "value": interview_plan.model_dump()})
        else:
            yield _progress("questions", "skipped", "Interview plan skipped")

        # ── Done ──────────────────────────────────────────────────────────
        final = PipelineResult(
            resume=resume,
            jd=jd,
            scoring=scoring,
            verification=verification,
            interview_plan=interview_plan,
        )
        yield _sse("done", final.model_dump())

    except Exception as exc:
        logger.exception("Pipeline stream failed: %s", exc)
        yield _sse("error", {"message": str(exc)})