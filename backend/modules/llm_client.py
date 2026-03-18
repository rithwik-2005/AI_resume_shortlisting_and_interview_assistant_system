"""
modules/llm_client.py — OpenAI client wrapper.

Single place for all OpenAI API calls. Every module imports
`chat_json()` or `chat_text()` from here — no module ever
instantiates its own OpenAI client.

Features
--------
- Text sanitization: strips null bytes and control characters that cause
  HTTP 400 "could not parse JSON body" errors from the OpenAI API.
- Automatic JSON extraction (strips markdown fences, validates).
- Graceful fallback: returns {} or "" on parse failure instead of crashing.
- Model routing: pass `use_mini=True` for cheaper extraction tasks.
- Retry with exponential backoff on rate-limit (429) and transient (5xx) errors.
- Hard token-budget cap: truncates excessively long prompts before sending.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import Any

from openai import OpenAI, RateLimitError, APIStatusError, BadRequestError

from config import settings

logger = logging.getLogger(__name__)

# ── Retry config ──────────────────────────────────────────────────────────────
_MAX_RETRIES     = 3       # attempts before giving up
_RETRY_BASE_S    = 1.5     # base sleep in seconds (doubles each retry)
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

# ── Safety limits ─────────────────────────────────────────────────────────────
# Rough character budget per message (1 token ≈ 4 chars for English).
# gpt-4o context window is 128k tokens ≈ 512k chars.
# We stay well under that to leave room for the response.
_MAX_USER_CHARS   = 60_000   # ~15k tokens for the user message
_MAX_SYSTEM_CHARS = 8_000    # ~2k tokens for the system prompt

# Initialise once at import time; raises clearly if key is missing.
_client: OpenAI | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is not set. "
                "Export it with: export OPENAI_API_KEY=sk-..."
            )
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


def _sanitize(text: str, max_chars: int | None = None) -> str:
    """
    Remove characters that cause OpenAI HTTP 400 errors.

    The OpenAI API serializes messages to JSON internally. Any character
    that is not valid in a JSON string — null bytes (\\x00), lone surrogates,
    and most C0/C1 control characters — triggers the cryptic
    "could not parse JSON body" 400 error.

    Steps:
    1. Ensure the string is valid Python str (catches surrogate chars).
    2. Remove null bytes (most common offender from PDF extraction).
    3. Remove other C0 control characters except \\t, \\n, \\r.
    4. Normalize unicode to NFC (reduces exotic sequences).
    5. Truncate to max_chars if specified (prevents context-window errors).
    """
    if not isinstance(text, str):
        text = str(text)

    # Encode to UTF-8 replacing surrogates, then decode back
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    # Remove null bytes — the #1 cause of the 400 error from PDF extraction
    text = text.replace("\x00", "")

    # Remove C0 control characters except tab, newline, carriage return
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    # Remove C1 control characters (Windows-1252 artifacts from DOCX/PDF)
    text = re.sub(r"[\x80-\x9f]", "", text)

    # Normalize unicode (NFC) — reduces unusual character sequences
    text = unicodedata.normalize("NFC", text)

    # Truncate if needed
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
        logger.warning("Prompt truncated to %d chars to stay within token budget.", max_chars)

    return text


def _clean_json(raw: str) -> str:
    """Strip markdown code fences and leading/trailing whitespace."""
    cleaned = re.sub(r"```(?:json)?", "", raw)
    return cleaned.strip().strip("`").strip()


def _call_with_retry(fn, *args, **kwargs):
    """
    Call fn() with exponential backoff on retryable errors.

    Retries on:
      - openai.RateLimitError        (429)
      - openai.APIStatusError        (500, 502, 503, 504)

    Does NOT retry on:
      - openai.BadRequestError (400) — these are caller errors (bad prompt),
        retrying won't help. The error is logged with the first 200 chars of
        the message to aid debugging.
    """
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as exc:
            last_exc = exc
            wait = _RETRY_BASE_S * (2 ** (attempt - 1))
            logger.warning(
                "OpenAI rate-limit hit (attempt %d/%d) — sleeping %.1f s",
                attempt, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
        except BadRequestError as exc:
            # 400 errors are not transient — log the detail and raise immediately
            logger.error(
                "OpenAI 400 BadRequest (not retrying): %s",
                str(exc)[:300],
            )
            raise
        except APIStatusError as exc:
            if exc.status_code in _RETRYABLE_CODES:
                last_exc = exc
                wait = _RETRY_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "OpenAI transient error %s (attempt %d/%d) — sleeping %.1f s",
                    exc.status_code, attempt, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(
        f"OpenAI call failed after {_MAX_RETRIES} attempts."
    ) from last_exc


# ── Public API ────────────────────────────────────────────────────────────────

def chat_json(
    system: str,
    user: str,
    use_mini: bool = False,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """
    Call OpenAI and parse the response as JSON.

    Uses response_format=json_object which forces the model to return
    valid JSON — no markdown fences, no explanation text.

    Both system and user messages are sanitized before sending to prevent
    HTTP 400 errors caused by null bytes or control characters in PDF/DOCX
    extracted text.

    Parameters
    ----------
    system    : str   System prompt.
    user      : str   User message (may contain extracted resume text).
    use_mini  : bool  Use gpt-4o-mini instead of gpt-4o (cheaper).
    max_tokens: int   Max response tokens.

    Returns
    -------
    dict  Parsed JSON. Returns {} on parse failure (logs a warning).
    """
    model  = settings.extraction_model if use_mini else settings.primary_model
    client = _get_client()

    # Sanitize both messages before sending
    clean_system = _sanitize(system, max_chars=_MAX_SYSTEM_CHARS)
    clean_user   = _sanitize(user,   max_chars=_MAX_USER_CHARS)

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": clean_system},
                {"role": "user",   "content": clean_user},
            ],
        )

    try:
        response = _call_with_retry(_call)
        raw = response.choices[0].message.content or ""
        return json.loads(_clean_json(raw))
    except json.JSONDecodeError as exc:
        logger.warning("JSON decode failed from model %s: %s", model, exc)
        return {}
    except Exception as exc:
        logger.error("OpenAI API error (%s): %s", model, exc)
        raise


def chat_text(
    system: str,
    user: str,
    use_mini: bool = False,
    max_tokens: int = 500,
) -> str:
    """
    Call OpenAI and return the raw text response (no JSON parsing).

    Returns
    -------
    str  Model response text.
    """
    model  = settings.extraction_model if use_mini else settings.primary_model
    client = _get_client()

    clean_system = _sanitize(system, max_chars=_MAX_SYSTEM_CHARS)
    clean_user   = _sanitize(user,   max_chars=_MAX_USER_CHARS)

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": clean_system},
                {"role": "user",   "content": clean_user},
            ],
        )

    response = _call_with_retry(_call)
    return (response.choices[0].message.content or "").strip()