#!/usr/bin/env python3
"""
cli.py — Command-line interface for the AI Resume Shortlisting System.

Run the full evaluation pipeline directly without starting the API server.
Useful for quick testing, CI pipelines, and batch scripting.

Usage
-----
Single evaluation (files):
    python cli.py evaluate --resume path/to/resume.pdf --jd path/to/jd.docx

Single evaluation (inline text — great for quick smoke tests):
    python cli.py evaluate --resume-text "Jane Doe, Python dev..." --jd-text "We need a Python engineer..."

Batch evaluation:
    python cli.py batch --jd path/to/jd.txt resume1.pdf resume2.docx resume3.txt

Just parse (no scoring):
    python cli.py parse --resume path/to/resume.pdf --jd path/to/jd.txt

Environment
-----------
Requires OPENAI_API_KEY to be set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Make sure backend/ is on the path ─────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── Load .env if present ───────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

# ─── Colour helpers ────────────────────────────────────────────────────────

def _c(text: str, code: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

def green(t):  return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t):    return _c(t, "31")
def cyan(t):   return _c(t, "36")
def bold(t):   return _c(t, "1")
def dim(t):    return _c(t, "2")


# ─── Printing helpers ──────────────────────────────────────────────────────

def _header(title: str) -> None:
    print(f"\n{bold('═' * 60)}")
    print(f"  {bold(title)}")
    print(bold('═' * 60))


def _section(title: str) -> None:
    print(f"\n{cyan('──')} {bold(title)} {cyan('──')}")


def _bar(score: float, width: int = 30) -> str:
    filled = int(score / 100 * width)
    colour = green if score >= 70 else yellow if score >= 45 else red
    return colour("█" * filled) + dim("░" * (width - filled)) + f" {score:.0f}"


def _tier_label(tier: str) -> str:
    labels = {"A": "Fast-track", "B": "Technical Screen", "C": "Needs Evaluation"}
    colours = {"A": green, "B": yellow, "C": red}
    return colours.get(tier, str)(f"Tier {tier} — {labels.get(tier, '')}")


def _print_scoring(scoring: dict) -> None:
    _section("Scoring Results")
    composite = scoring["composite_score"]
    tier      = scoring["tier"]
    print(f"  Candidate : {bold(scoring['candidate_name'])}")
    print(f"  Composite : {_bar(composite)}")
    print(f"  Tier      : {_tier_label(tier)}")
    print()

    dims = [
        ("exact_match",          "Exact Match       (25%)"),
        ("semantic_similarity",  "Semantic Sim.     (35%)"),
        ("achievement_impact",   "Achievement       (25%)"),
        ("ownership_leadership", "Ownership         (15%)"),
    ]
    for key, label in dims:
        d = scoring[key]
        print(f"  {label}  {_bar(d['score'])}")
        print(f"    {dim(d['explanation'])}")
        if d.get("evidence"):
            print(f"    Evidence: {dim(', '.join(d['evidence'][:3]))}")
        print()

    _section("Summary")
    print(f"  {scoring['scoring_summary']}")
    print(f"\n  {dim(scoring['tier_rationale'])}")


def _print_verification(v: dict) -> None:
    _section("Claim Verification")
    score = v["overall_authenticity_score"]
    colour = green if score >= 70 else yellow if score >= 45 else red
    print(f"  Authenticity Score: {colour(f'{score}/100')}")
    if v["red_flags"]:
        for flag in v["red_flags"]:
            print(f"  {yellow('⚠')}  {flag}")
    else:
        print(f"  {green('✓')}  No red flags")

    g = v.get("github")
    if g and g.get("profile_found"):
        print(f"\n  GitHub @{g['username']}: {g['public_repos']} repos · "
              f"{g['followers']} followers · {g['recent_commits_30d']} commits/30d · "
              f"Activity {g['activity_score']}/100")


def _print_interview(plan: dict) -> None:
    _section(f"Interview Plan — Tier {plan['tier']} ({plan['total_duration_minutes']} min)")
    print(f"\n  {bold('Briefing')}")
    print(f"  {plan['opening_context']}")

    print(f"\n  {bold('Structure')}")
    for s in plan["interview_sections"]:
        print(f"    {s['duration_min']:>3} min  {s['section']}")
        print(f"           {dim(s['focus'])}")

    print(f"\n  {bold('Questions')} ({len(plan['questions'])} total)")
    for i, q in enumerate(plan["questions"], 1):
        diff_colour = green if q["difficulty"] == "Warm-up" else yellow if q["difficulty"] == "Stretch" else cyan
        print(f"\n  {bold(str(i))}. [{diff_colour(q['difficulty'])}] {cyan(q['category'])}")
        print(f"     {q['question']}")
        if q.get("follow_up"):
            print(f"     {dim('↳ ' + q['follow_up'])}")


def _print_batch(batch: dict) -> None:
    _header(f"Batch Results — {batch['jd_title']}")
    a_str = green("A: " + str(batch["tier_a_count"]))
    b_str = yellow("B: " + str(batch["tier_b_count"]))
    c_str = red("C: " + str(batch["tier_c_count"]))
    print(f"  {batch['total_candidates']} candidates evaluated  |  {a_str}  {b_str}  {c_str}")
    print()
    print(f"  {'Rank':<5} {'Candidate':<28} {'Score':>6}  {'Tier':<22}  Top Signal")
    print(f"  {'─'*4}  {'─'*27} {'─'*6}  {'─'*21}  {'─'*40}")
    tier_labels = {"A": "Fast-track", "B": "Tech Screen", "C": "Needs Eval"}
    for r in batch["leaderboard"]:
        score_c = green if r["composite_score"] >= 75 else yellow if r["composite_score"] >= 50 else red
        tier_c  = green if r["tier"] == "A" else yellow if r["tier"] == "B" else red
        signal  = r["top_signal"][:45] + "…" if len(r["top_signal"]) > 45 else r["top_signal"]
        score_str = score_c(f"{r['composite_score']:>5.1f}")
        tier_str  = tier_c(f"Tier {r['tier']} — {tier_labels[r['tier']]}")
        print(f"  #{r['rank']:<4} {r['candidate_name']:<28} {score_str}  {tier_str:<22}  {dim(signal)}")

    if batch.get("failed_candidates"):
        print(f"\n  {red('Failed:')}")
        for f in batch["failed_candidates"]:
            print(f"    • {f}")


# ─── Commands ─────────────────────────────────────────────────────────────

def cmd_evaluate(args: argparse.Namespace) -> None:
    from modules.file_extractor import extract_text
    from modules.parser import parse_resume, parse_jd
    from modules.scoring_engine import score_candidate
    from modules.verification_engine import verify_candidate
    from modules.question_generator import generate_interview_plan

    # ── Get text ──────────────────────────────────────────────────────────
    if args.resume:
        path = Path(args.resume)
        resume_text = extract_text(path.name, path.read_bytes())
    else:
        resume_text = args.resume_text

    if args.jd:
        path = Path(args.jd)
        jd_text = extract_text(path.name, path.read_bytes())
    else:
        jd_text = args.jd_text

    _header("AI Resume Evaluation")

    t0 = time.time()

    print(f"  Parsing documents…", end="", flush=True)
    resume = parse_resume(resume_text)
    jd     = parse_jd(jd_text)
    print(f"  {green('✓')}  {resume.candidate_name} → {jd.title}")

    print(f"  Scoring (4 parallel dimensions)…", end="", flush=True)
    scoring = score_candidate(resume, jd)
    print(f"  {green('✓')}  {scoring.composite_score}/100 · Tier {scoring.tier.value}")

    verification = None
    if not args.no_verify:
        print(f"  Verifying profiles…", end="", flush=True)
        verification = verify_candidate(resume)
        print(f"  {green('✓')}  Authenticity {verification.overall_authenticity_score}/100")

    plan = None
    if not args.no_questions:
        print(f"  Generating interview plan…", end="", flush=True)
        plan = generate_interview_plan(
            resume=resume, jd=jd, scoring=scoring,
            verification_summary=verification.verification_summary if verification else "",
            red_flags=verification.red_flags if verification else [],
        )
        print(f"  {green('✓')}  {len(plan.questions)} questions · Tier {plan.tier.value}")

    elapsed = time.time() - t0
    print(f"\n  {dim(f'Completed in {elapsed:.1f}s')}")

    # ── Print results ─────────────────────────────────────────────────────
    _print_scoring(scoring.model_dump())
    if verification:
        _print_verification(verification.model_dump())
    if plan:
        _print_interview(plan.model_dump())

    # ── Save JSON ─────────────────────────────────────────────────────────
    if args.output:
        from models import PipelineResult
        result = PipelineResult(
            resume=resume, jd=jd, scoring=scoring,
            verification=verification, interview_plan=plan,
        )
        Path(args.output).write_text(
            json.dumps(result.model_dump(), indent=2, default=str)
        )
        print(f"\n  {green('✓')} Saved to {args.output}")


def cmd_parse(args: argparse.Namespace) -> None:
    from modules.file_extractor import extract_text
    from modules.parser import parse_resume, parse_jd

    rp = Path(args.resume)
    jp = Path(args.jd)

    resume = parse_resume(extract_text(rp.name, rp.read_bytes()))
    jd     = parse_jd(extract_text(jp.name, jp.read_bytes()))

    _header("Parsed Documents")
    _section("Resume")
    print(json.dumps(resume.model_dump(), indent=2, default=str))
    _section("Job Description")
    print(json.dumps(jd.model_dump(), indent=2, default=str))


def cmd_batch(args: argparse.Namespace) -> None:
    from modules.file_extractor import extract_text
    from modules.parser import parse_resume, parse_jd
    from modules.batch_evaluator import batch_evaluate

    jp = Path(args.jd)
    jd = parse_jd(extract_text(jp.name, jp.read_bytes()))

    resumes = []
    for rp_str in args.resumes:
        rp = Path(rp_str)
        try:
            resumes.append(parse_resume(extract_text(rp.name, rp.read_bytes())))
            print(f"  Parsed: {rp.name}")
        except Exception as exc:
            print(f"  {yellow('⚠')}  Skipping {rp.name}: {exc}")

    if not resumes:
        print(red("No resumes parsed. Aborting."))
        sys.exit(1)

    print(f"\n  Evaluating {len(resumes)} candidates against '{jd.title}'…")
    t0 = time.time()
    result = batch_evaluate(resumes, jd, max_workers=args.workers)
    elapsed = time.time() - t0
    print(f"  {green('✓')} Done in {elapsed:.1f}s")

    _print_batch({
        "jd_title": result.jd_title,
        "total_candidates": result.total_candidates,
        "tier_a_count": result.tier_a_count,
        "tier_b_count": result.tier_b_count,
        "tier_c_count": result.tier_c_count,
        "leaderboard": [
            {
                "rank": r.rank,
                "candidate_name": r.candidate_name,
                "composite_score": r.composite_score,
                "tier": r.tier.value,
                "top_signal": r.top_signal,
            }
            for r in result.leaderboard
        ],
        "failed_candidates": result.failed_candidates,
    })

    if args.output:
        out = {
            "jd_title": result.jd_title,
            "leaderboard": [
                {
                    "rank": r.rank,
                    "candidate_name": r.candidate_name,
                    "composite_score": r.composite_score,
                    "tier": r.tier.value,
                    "scoring": r.scoring.model_dump(),
                }
                for r in result.leaderboard
            ],
            "failed": result.failed_candidates,
        }
        Path(args.output).write_text(json.dumps(out, indent=2, default=str))
        print(f"\n  {green('✓')} Saved to {args.output}")


# ─── Argument parser ───────────────────────────────────────────────────────

def main():
    if not os.getenv("OPENAI_API_KEY"):
        print(red("Error: OPENAI_API_KEY is not set."))
        print("  Export it with: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="python cli.py",
        description="AI Resume Shortlisting CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── evaluate ──────────────────────────────────────────────────────────
    ev = sub.add_parser("evaluate", help="Evaluate a single candidate")
    src = ev.add_mutually_exclusive_group(required=True)
    src.add_argument("--resume",      metavar="FILE", help="Resume file (PDF/DOCX/TXT)")
    src.add_argument("--resume-text", metavar="TEXT", help="Resume as raw text")
    jd_src = ev.add_mutually_exclusive_group(required=True)
    jd_src.add_argument("--jd",      metavar="FILE", help="JD file (PDF/DOCX/TXT)")
    jd_src.add_argument("--jd-text", metavar="TEXT", help="JD as raw text")
    ev.add_argument("--no-verify",    action="store_true", help="Skip GitHub/LinkedIn verification")
    ev.add_argument("--no-questions", action="store_true", help="Skip interview plan generation")
    ev.add_argument("--output",       metavar="FILE", help="Save full JSON result to file")

    # ── parse ─────────────────────────────────────────────────────────────
    pa = sub.add_parser("parse", help="Parse files and print structured JSON")
    pa.add_argument("--resume", required=True, metavar="FILE")
    pa.add_argument("--jd",     required=True, metavar="FILE")

    # ── batch ─────────────────────────────────────────────────────────────
    ba = sub.add_parser("batch", help="Rank multiple candidates against one JD")
    ba.add_argument("--jd",      required=True, metavar="FILE")
    ba.add_argument("--workers", type=int, default=5, metavar="N", help="Parallel workers (default 5)")
    ba.add_argument("--output",  metavar="FILE", help="Save leaderboard JSON to file")
    ba.add_argument("resumes",   nargs="+", metavar="RESUME_FILE", help="One or more resume files")

    args = parser.parse_args()

    if args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "parse":
        cmd_parse(args)
    elif args.command == "batch":
        cmd_batch(args)


if __name__ == "__main__":
    main()