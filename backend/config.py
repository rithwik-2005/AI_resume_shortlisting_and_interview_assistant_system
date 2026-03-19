"""
config.py — Centralised configuration for the AI Resume Shortlisting System.

All environment variables and runtime constants live here.
Import `settings` anywhere in the project; never read os.getenv() elsewhere.

IMPORTANT: load_dotenv() is called HERE, before Settings() is instantiated,
so that os.getenv() always sees the values from the .env file regardless of
the order in which modules are imported.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env from the backend/ folder (where this file lives) ────────────
# override=False means real environment variables always win over .env values.
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------ #
    # OpenAI (required)
    # ------------------------------------------------------------------ #
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    # Primary model for scoring, question generation, and summary
    primary_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o")
    )
    # Cheaper model for bulk extraction / parsing
    extraction_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_EXTRACT_MODEL", "gpt-4o-mini")
    )

    # ------------------------------------------------------------------ #
    # GitHub token (OPTIONAL)
    #
    # Without a token : GitHub API allows 60  requests / hour.
    # With a token    : GitHub API allows 5000 requests / hour.
    #
    # The system works perfectly without it — the verification engine
    # automatically skips adding the Authorization header when this is empty.
    #
    # To get a free token (no special permissions needed):
    #   https://github.com/settings/tokens  → Generate new token (classic)
    #   → set any expiry → check NO scopes → Generate
    # Then add:  GITHUB_TOKEN=ghp_...  to backend/.env
    # ------------------------------------------------------------------ #
    github_token: str = field(
        default_factory=lambda: os.getenv("GITHUB_TOKEN", "")
    )

    # ------------------------------------------------------------------ #
    # Scoring weights  (must sum to 1.0)
    # ------------------------------------------------------------------ #
    weight_exact_match: float = 0.25
    weight_semantic_similarity: float = 0.35
    weight_achievement_impact: float = 0.25
    weight_ownership_leadership: float = 0.15

    # ------------------------------------------------------------------ #
    # Tier thresholds
    # ------------------------------------------------------------------ #
    tier_a_threshold: float = 75.0
    tier_b_threshold: float = 50.0

    # ------------------------------------------------------------------ #
    # File handling
    # ------------------------------------------------------------------ #
    max_file_size_mb: int = 10
    allowed_extensions: tuple = (".pdf", ".docx", ".doc", ".txt")

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    cors_origins: list = field(default_factory=lambda: ["*"])


settings = Settings()

# ── Startup checks ─────────────────────────────────────────────────────────
import warnings   # noqa: E402 (import after settings instantiation is intentional)

if not settings.openai_api_key:
    warnings.warn(
        "\n\n"
        "  ⚠️  OPENAI_API_KEY is not set!\n"
        f"  Expected .env file at: {_env_path}\n"
        "  Create it and add:  OPENAI_API_KEY=sk-...\n",
        stacklevel=2,
    )

if not settings.github_token:
    # This is normal — just informational, not a warning
    import logging
    logging.getLogger(__name__).info(
        "GITHUB_TOKEN not set — GitHub API rate limit: 60 req/hr. "
        "Add GITHUB_TOKEN to backend/.env to raise it to 5000 req/hr."
    )