from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class PdfFormatCheck:
    ok: bool
    reason: str
    extracted_head: str = ""

    def __iter__(self):
        # Backward-compatible tuple-unpacking: ok, reason = validate_pdf_format(...)
        yield self.ok
        yield self.reason


def _normalize_text(text: str) -> str:
    # Normalize whitespace + case for robust token checks.
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _extract_first_page_text(pdf_bytes: bytes, *, max_chars: int = 20000) -> str:
    """Best-effort first-page text extraction.

    Uses pypdf (lightweight, pure-python). If text extraction fails, returns "".
    """

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return ""
        text = reader.pages[0].extract_text() or ""
        if len(text) > max_chars:
            text = text[:max_chars]
        return text
    except Exception:
        return ""


def validate_pdf_format(
    pdf_bytes: bytes,
    *,
    expected_vin: Optional[str] = None,
    require_official_tokens: bool = True,
) -> PdfFormatCheck:
    """Best-effort PDF sanity checks.

    Note: Upstream PDFs must never be blocked at delivery-time.
    This helper is intended for optional offline tooling only.
    """

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return PdfFormatCheck(False, "not_pdf")

    head_text = _extract_first_page_text(pdf_bytes)
    low = _normalize_text(head_text)

    # Fallback: when text extraction fails (missing pypdf, image-only, etc.),
    # use raw-byte token checks to avoid false negatives that would break production.
    raw_head = pdf_bytes[:400_000]
    raw_low: Optional[bytes]
    try:
        raw_low = raw_head.lower()
    except Exception:
        raw_low = None

    # Official tokens required for base report correctness.
    # Prefer extracted text; fallback to raw bytes if extraction fails.
    def _has_token(token: str) -> bool:
        if not token:
            return False
        t = token.lower().strip()
        if low and t in low:
            return True
        if raw_low is not None:
            try:
                return t.encode("utf-8", errors="ignore") in raw_low
            except Exception:
                return False
        return False

    # Official tokens required for base report correctness.
    if require_official_tokens:
        if not _has_token("carfax"):
            return PdfFormatCheck(False, "missing:carfax", extracted_head=head_text)
        if not _has_token("vehicle history report"):
            return PdfFormatCheck(False, "missing:vehicle_history_report", extracted_head=head_text)

    if expected_vin:
        # VIN may appear with/without spaces; check text if available, else fall back to raw bytes.
        vin = re.sub(r"\s+", "", expected_vin).upper()
        if vin:
            ok_vin = False
            if head_text:
                compact = re.sub(r"\s+", "", head_text).upper()
                if vin in compact:
                    ok_vin = True
            if not ok_vin:
                try:
                    if vin.encode("ascii", errors="ignore") in raw_head.upper():
                        ok_vin = True
                except Exception:
                    pass
            if not ok_vin:
                return PdfFormatCheck(False, "missing:vin", extracted_head=head_text)

    return PdfFormatCheck(True, "ok", extracted_head=head_text)
