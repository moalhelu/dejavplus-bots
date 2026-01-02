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


# Hard prohibitions (never allowed)
_FORBIDDEN_PHRASES = [
    "history-based value report",
    "this is a fast/light pdf",
    "strict time budget",
    "vin report",  # placeholder header used by legacy template
]


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
    """Validate that a PDF looks like the official CARFAX VHR report.

    Non-negotiables:
    - Never allow placeholder / value-report PDFs
    - For base delivery, require tokens "CARFAX" + "Vehicle History Report" on first page.
    """

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return PdfFormatCheck(False, "not_pdf")

    head_text = _extract_first_page_text(pdf_bytes)
    low = _normalize_text(head_text)

    # If we can't extract text at all, be conservative and fail.
    if not low:
        return PdfFormatCheck(False, "no_text_extracted", extracted_head=head_text)

    for bad in _FORBIDDEN_PHRASES:
        if bad and bad in low:
            return PdfFormatCheck(False, f"forbidden:{bad}", extracted_head=head_text)

    # Official tokens required for base report correctness.
    if require_official_tokens:
        if "carfax" not in low:
            return PdfFormatCheck(False, "missing:carfax", extracted_head=head_text)
        if "vehicle history report" not in low:
            return PdfFormatCheck(False, "missing:vehicle_history_report", extracted_head=head_text)

    if expected_vin:
        # VIN may appear with/without spaces; do a loose contains.
        vin = re.sub(r"\s+", "", expected_vin).upper()
        compact = re.sub(r"\s+", "", head_text).upper()
        if vin and vin not in compact:
            return PdfFormatCheck(False, "missing:vin", extracted_head=head_text)

    return PdfFormatCheck(True, "ok", extracted_head=head_text)
