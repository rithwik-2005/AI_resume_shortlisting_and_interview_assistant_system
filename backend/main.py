"""
main.py — FastAPI application entry point.

Routes
------
GET  /                      Serves frontend index.html
GET  /api/health            Liveness probe
POST /api/evaluate          Full pipeline  (multipart file upload)
POST /api/evaluate/stream   Full pipeline, SSE streaming (multipart file upload)
POST /api/batch             Batch evaluate N resumes vs 1 JD (multipart)
POST /api/parse             Parse files only
POST /api/score             Score pre-parsed resume vs JD (JSON)
POST /api/verify            Verify social claims (JSON)
POST /api/questions         Generate interview plan (JSON)

Frontend is served directly by FastAPI — no nginx or separate
container needed. This makes Docker deployment a single container.

All file-upload endpoints accept multipart/form-data.
Accepted file formats: PDF · DOCX · DOC · TXT  (max 10 MB each)
"""

from __future__ import annotations

# ── MUST come right after __future__: load .env before any module reads
# os.getenv(). config.py instantiates Settings() at import time, so
# the env vars must already be in os.environ by the time it is imported.
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)
# ──────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from models import (
    InterviewPlan,
    JobDescription,
    ParsedResume,
    PipelineResult,
    ScoringResult,
    VerificationResult,
)
from modules.batch_evaluator import BatchResult, batch_evaluate
from modules.file_extractor import extract_text
from modules.parser import parse_jd, parse_resume
from modules.question_generator import generate_interview_plan
from modules.scoring_engine import score_candidate
from modules.streamer import pipeline_stream
from modules.verification_engine import verify_candidate

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────

app = FastAPI(
    title="AI Resume Shortlisting & Interview Assistant",
    description=(
        "Automates candidate evaluation with multi-dimensional scoring, "
        "claim verification, and tailored interview generation. "
        "Supports single evaluation, SSE streaming, and batch ranking."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_BYTES = settings.max_file_size_mb * 1024 * 1024

# ──────────────────────────────────────────────
# Frontend — served directly by FastAPI
# Works in Docker (one container) and locally.
# ──────────────────────────────────────────────

# Path to frontend folder — works both locally and inside Docker container
_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if _FRONTEND_DIR.exists():
    # Serve index.html at "/"
    @app.get("/", include_in_schema=False)
    def serve_index():
        return FileResponse(str(_FRONTEND_DIR / "index.html"))

    # Serve all other static files (CSS, JS, images if any)
    app.mount(
        "/static",
        StaticFiles(directory=str(_FRONTEND_DIR)),
        name="static",
    )
    logger.info("Frontend served from: %s", _FRONTEND_DIR)
else:
    logger.warning(
        "Frontend folder not found at %s — UI will not be available. "
        "Open frontend/index.html directly in your browser instead.",
        _FRONTEND_DIR,
    )


# ──────────────────────────────────────────────
# File helpers
# ──────────────────────────────────────────────

async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File '{file.filename}' exceeds the {settings.max_file_size_mb} MB limit.",
        )
    return data


async def _file_to_text(file: UploadFile) -> str:
    data = await _read_upload(file)
    try:
        return extract_text(file.filename or "file", data)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _parse_form_bool(value: str | bool) -> bool:
    """
    Normalise form-submitted booleans.
    HTML forms always submit strings ("true"/"false"/"on").
    This converts them to proper Python booleans.
    """
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


# ──────────────────────────────────────────────
# JSON-body request schemas
# ──────────────────────────────────────────────

class ScoreRequest(BaseModel):
    resume: ParsedResume
    jd: JobDescription


class VerifyRequest(BaseModel):
    resume: ParsedResume


class QuestionsRequest(BaseModel):
    resume: ParsedResume
    jd: JobDescription
    scoring: ScoringResult
    verification_summary: Optional[str] = ""
    red_flags: Optional[list[str]] = None


# ──────────────────────────────────────────────
# Batch response schema
# ──────────────────────────────────────────────

class CandidateRankResponse(BaseModel):
    rank: int
    candidate_name: str
    composite_score: float
    tier: str
    tier_rationale: str
    top_signal: str
    biggest_gap: str
    scoring: ScoringResult


class BatchResponse(BaseModel):
    jd_title: str
    total_candidates: int
    tier_a_count: int
    tier_b_count: int
    tier_c_count: int
    leaderboard: list[CandidateRankResponse]
    failed_candidates: list[str]


def _to_batch_response(br: BatchResult) -> BatchResponse:
    return BatchResponse(
        jd_title=br.jd_title,
        total_candidates=br.total_candidates,
        tier_a_count=br.tier_a_count,
        tier_b_count=br.tier_b_count,
        tier_c_count=br.tier_c_count,
        leaderboard=[
            CandidateRankResponse(
                rank=r.rank,
                candidate_name=r.candidate_name,
                composite_score=r.composite_score,
                tier=r.tier.value,
                tier_rationale=r.tier_rationale,
                top_signal=r.top_signal,
                biggest_gap=r.biggest_gap,
                scoring=r.scoring,
            )
            for r in br.leaderboard
        ],
        failed_candidates=br.failed_candidates,
    )


# ══════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


@app.post("/api/parse")
async def parse_files(
    resume_file: UploadFile = File(...),
    jd_file:     UploadFile = File(...),
):
    """Extract & parse both files. Returns structured ParsedResume + JobDescription."""
    resume = parse_resume(await _file_to_text(resume_file))
    jd     = parse_jd(await _file_to_text(jd_file))
    return {"resume": resume.model_dump(), "jd": jd.model_dump()}


@app.post("/api/score", response_model=ScoringResult)
def score_endpoint(req: ScoreRequest):
    """Run 4-dimensional parallel scoring on pre-parsed data."""
    return score_candidate(req.resume, req.jd)


@app.post("/api/verify", response_model=VerificationResult)
def verify_endpoint(req: VerifyRequest):
    """Verify GitHub / LinkedIn claims."""
    return verify_candidate(req.resume)


@app.post("/api/questions", response_model=InterviewPlan)
def questions_endpoint(req: QuestionsRequest):
    """Generate a tailored interview plan."""
    return generate_interview_plan(
        resume=req.resume,
        jd=req.jd,
        scoring=req.scoring,
        verification_summary=req.verification_summary or "",
        red_flags=req.red_flags,
    )


@app.post("/api/evaluate", response_model=PipelineResult)
async def evaluate_endpoint(
    resume_file:      UploadFile = File(...),
    jd_file:          UploadFile = File(...),
    run_verification: str = Form("true"),
    run_questions:    str = Form("true"),
):
    """
    Full evaluation pipeline from raw file uploads.
    Steps: extract → parse → score (parallel) → verify → interview plan.
    """
    do_verify    = _parse_form_bool(run_verification)
    do_questions = _parse_form_bool(run_questions)
    logger.info("=== Pipeline START | verify=%s questions=%s ===", do_verify, do_questions)

    resume  = parse_resume(await _file_to_text(resume_file))
    jd      = parse_jd(await _file_to_text(jd_file))
    scoring = score_candidate(resume, jd)

    verification   = verify_candidate(resume) if do_verify else None
    interview_plan = None
    if do_questions:
        interview_plan = generate_interview_plan(
            resume=resume, jd=jd, scoring=scoring,
            verification_summary=verification.verification_summary if verification else "",
            red_flags=verification.red_flags if verification else [],
        )

    logger.info("=== Pipeline DONE | %s | %.1f | Tier %s ===",
                scoring.candidate_name, scoring.composite_score, scoring.tier.value)
    return PipelineResult(resume=resume, jd=jd, scoring=scoring,
                          verification=verification, interview_plan=interview_plan)


@app.post("/api/evaluate/stream")
async def evaluate_stream(
    resume_file:      UploadFile = File(...),
    jd_file:          UploadFile = File(...),
    run_verification: str = Form("true"),
    run_questions:    str = Form("true"),
):
    """
    Full pipeline with Server-Sent Events streaming.
    The client receives live JSON progress events as each step completes.
    Event types: progress | result | done | error
    """
    resume_text = await _file_to_text(resume_file)
    jd_text     = await _file_to_text(jd_file)

    return StreamingResponse(
        pipeline_stream(
            resume_text=resume_text,
            jd_text=jd_text,
            run_verification=_parse_form_bool(run_verification),
            run_questions=_parse_form_bool(run_questions),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/batch", response_model=BatchResponse)
async def batch_endpoint(
    resume_files: list[UploadFile] = File(...),
    jd_file:      UploadFile       = File(...),
    max_workers:  int              = Form(5),
):
    """
    Rank multiple candidates against one Job Description.
    - Accepts 1–50 resume files in a single request.
    - All resumes are scored in parallel (max_workers threads).
    - Returns a leaderboard sorted by composite score.
    """
    if not resume_files:
        raise HTTPException(422, "At least one resume file is required.")
    if len(resume_files) > 50:
        raise HTTPException(422, "Maximum 50 resumes per batch.")
    if not 1 <= max_workers <= 20:
        raise HTTPException(422, "max_workers must be 1–20.")

    jd = parse_jd(await _file_to_text(jd_file))

    resumes: list[ParsedResume] = []
    for rf in resume_files:
        try:
            resumes.append(parse_resume(await _file_to_text(rf)))
        except HTTPException as exc:
            logger.warning("Skipping '%s': %s", rf.filename, exc.detail)

    if not resumes:
        raise HTTPException(422, "No resumes could be parsed successfully.")

    return _to_batch_response(batch_evaluate(resumes, jd, max_workers=max_workers))