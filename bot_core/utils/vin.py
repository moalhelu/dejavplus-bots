"""Utility helpers for VIN parsing and presentation."""
from __future__ import annotations

import math
import re
from typing import Optional

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
# Normalize Arabic/Persian digits and strip bidi/zero-width controls to accept RTL input.
_DIGIT_TRANSLATE = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CONTROL_RE = re.compile(r"[\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def normalize_vin(value: Optional[str]) -> Optional[str]:
    """Return a cleaned VIN (17 chars) or ``None`` if invalid."""
    if not value:
        return None
    # Remove bidi/zero-width controls and map non-ASCII digits to ASCII before validation.
    sanitized = _CONTROL_RE.sub("", value)
    sanitized = sanitized.translate(_DIGIT_TRANSLATE)
    candidate = re.sub(r"[\s-]", "", sanitized).upper()
    return candidate if VIN_RE.match(candidate) else None


def is_valid_vin(value: Optional[str]) -> bool:
    """Fast validity check for VIN strings."""
    return normalize_vin(value) is not None


def make_progress_bar(percent: int, width_blocks: int = 10) -> str:
    """Draw a simple progress bar using unicode blocks."""
    percent = max(0, min(100, percent))
    width_blocks = max(1, width_blocks)
    filled = math.floor((percent / 100) * width_blocks)
    empty = width_blocks - filled
    return f"{'🟩' * filled}{'⬜' * empty} {percent}%"


__all__ = ["VIN_RE", "normalize_vin", "is_valid_vin", "make_progress_bar"]
