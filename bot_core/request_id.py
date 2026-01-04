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
    request_key: Optional[str] = None,
) -> str:
    """Deterministic request id for idempotency.

    rid = sha256(platform|user|vin|language|options_json|request_key)

    IMPORTANT: request_key should be stable for a single inbound request
    (e.g., Telegram message_id / WhatsApp message id) so that retries and
    webhook duplicates do not double-charge. When a user sends a new request,
    request_key must differ so credits are deducted again.
    """

    payload = {
        "platform": (platform or "").strip().lower(),
        "user_id": str(user_id),
        "vin": (vin or "").strip().upper(),
        "language": (language or "en").strip().lower(),
        "options": options or {},
        "request_key": (request_key or "").strip() or None,
    }
    packed = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8", errors="ignore")).hexdigest()[:24]
