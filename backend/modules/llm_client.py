"""
modules/llm_client.py — OpenAI client wrapper.

Single place for all OpenAI API calls. Every module imports
`chat_json()` or `chat_text()` from here — no module ever
instantiates its own OpenAI client.

Features
--------
- Automatic JSON extraction (strips markdown fences, validates).
- Graceful fallback: returns {} or "" on parse failure instead of crashing.
- Model routing: pass `use_mini=True` for cheaper extraction tasks.
- Retry with exponential backoff on rate-limit (429) and transient (5xx) errors.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import OpenAI, RateLimitError, APIStatusError

from config import settings

logger = logging.getLogger(__name__)

# ── Retry config ──────────────────────────────────────────────────────────────
_MAX_RETRIES   = 3          # attempts before giving up
_RETRY_BASE_S  = 1.5        # base sleep in seconds (doubles each retry)
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

# Initialise once at import time; raises clearly if key is missing.
_client: OpenAI | None = None


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


def _clean_json(raw: str) -> str:
    """Strip markdown code fences and leading/trailing whitespace."""
    cleaned = re.sub(r"```(?:json)?", "", raw)
    return cleaned.strip().strip("`").strip()


def _call_with_retry(fn, *args, **kwargs):
    """
    Call `fn(*args, **kwargs)` with exponential backoff on retryable errors.

    Retries on:
      - openai.RateLimitError (429)
      - openai.APIStatusError with a 5xx status code
    Raises immediately on all other exceptions.
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

    Uses `response_format=json_object` which forces the model to return
    valid JSON — no markdown fences, no explanation text.

    Parameters
    ----------
    system : str    System prompt.
    user : str      User message.
    use_mini : bool Use gpt-4o-mini (cheaper) instead of gpt-4o.
    max_tokens : int

    Returns
    -------
    dict  Parsed JSON. Returns {} on parse failure (logs a warning).
    """
    model  = settings.extraction_model if use_mini else settings.primary_model
    client = _get_client()

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
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

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )

    response = _call_with_retry(_call)
    return (response.choices[0].message.content or "").strip()