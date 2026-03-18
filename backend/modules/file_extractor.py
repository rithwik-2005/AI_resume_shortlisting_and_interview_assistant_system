"""
modules/file_extractor.py — Universal file-to-text extractor.

Supports: PDF · DOCX · DOC · TXT
Returns plain-text strings consumed by the parser module.

Strategy per format
--------------------
PDF  → PyMuPDF (fitz): page-by-page text extraction, preserves reading order.
DOCX → python-docx: iterates paragraphs + table cells for maximum coverage.
DOC  → python-docx fallback (works if saved as docx); else extract raw text.
TXT  → direct UTF-8 decode with Latin-1 fallback.
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# PDF
# ──────────────────────────────────────────────

def _extract_pdf(data: bytes) -> str:
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text("text"))
        text = "\n".join(pages).strip()
        if not text:
            logger.warning("PyMuPDF returned empty text — PDF may be image-only.")
        return text
    except ImportError:
        raise RuntimeError(
            "PyMuPDF is not installed. Run: pip install PyMuPDF"
        )
    except Exception as exc:
        raise RuntimeError(f"PDF extraction failed: {exc}") from exc


# ──────────────────────────────────────────────
# DOCX
# ──────────────────────────────────────────────

def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts: list[str] = []

        # Paragraphs (main body, headers, footers)
        for para in doc.paragraphs:
            if para.text.strip():
                parts.append(para.text.strip())

        # Tables (skills grids, education tables, etc.)
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(
                    cell.text.strip() for cell in row.cells if cell.text.strip()
                )
                if row_text:
                    parts.append(row_text)

        return "\n".join(parts).strip()
    except ImportError:
        raise RuntimeError(
            "python-docx is not installed. Run: pip install python-docx"
        )
    except Exception as exc:
        raise RuntimeError(f"DOCX extraction failed: {exc}") from exc


# ──────────────────────────────────────────────
# TXT
# ──────────────────────────────────────────────

def _extract_txt(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    raise RuntimeError("Could not decode text file with any supported encoding.")


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def extract_text(filename: str, data: bytes) -> str:
    """
    Extract plain text from an uploaded file.

    Parameters
    ----------
    filename : str
        Original filename including extension (used for format detection).
    data : bytes
        Raw file bytes.

    Returns
    -------
    str
        Extracted plain text, ready for the parser module.

    Raises
    ------
    ValueError
        If the file extension is not supported.
    RuntimeError
        If extraction fails (missing library, corrupted file, etc.).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    logger.info("Extracting text from file: %s (ext=%s, size=%d bytes)", filename, ext, len(data))

    if ext == "pdf":
        text = _extract_pdf(data)
    elif ext in ("docx",):
        text = _extract_docx(data)
    elif ext == "doc":
        # Try as DOCX first (modern .doc saved as docx); fall back to raw bytes
        try:
            text = _extract_docx(data)
        except Exception:
            logger.warning(".doc extraction via python-docx failed; falling back to raw text.")
            text = _extract_txt(data)
    elif ext == "txt":
        text = _extract_txt(data)
    else:
        raise ValueError(
            f"Unsupported file type '.{ext}'. "
            "Allowed: .pdf · .docx · .doc · .txt"
        )

    if not text:
        raise RuntimeError(
            f"No text could be extracted from '{filename}'. "
            "The file may be empty, image-only, or password-protected."
        )

    logger.info("Extracted %d characters from %s", len(text), filename)
    return text