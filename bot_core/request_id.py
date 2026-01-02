from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


def compute_request_id(
    *,
    platform: str,
    user_id: str,
    vin: str,
    language: str,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    """Deterministic request id for idempotency.

    rid = sha256(platform|user|vin|language|options_json)
    """

    payload = {
        "platform": (platform or "").strip().lower(),
        "user_id": str(user_id),
        "vin": (vin or "").strip().upper(),
        "language": (language or "en").strip().lower(),
        "options": options or {},
    }
    packed = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8", errors="ignore")).hexdigest()[:24]
