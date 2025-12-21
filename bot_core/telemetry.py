"""Lightweight, opt-in timing + correlation-id helpers.

Design goals:
- No behavioral changes (logging only)
- No secrets in logs
- Works across asyncio tasks via contextvars
- Easily disabled via env

Enable by setting `ENABLE_TIMING_LOGS=1`.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, AsyncIterator, Optional


_TIMING_LOGGER = logging.getLogger("dejavu.timing")

_rid_var: ContextVar[Optional[str]] = ContextVar("dejavu_rid", default=None)


def timing_enabled() -> bool:
    return os.getenv("ENABLE_TIMING_LOGS", "0").strip().lower() not in {"", "0", "false", "off", "no"}


def new_rid(prefix: str = "") -> str:
    rid = uuid.uuid4().hex[:12]
    return f"{prefix}{rid}" if prefix else rid


def get_rid() -> Optional[str]:
    return _rid_var.get()


@contextmanager
def set_rid(rid: Optional[str]) -> Iterator[None]:
    token = _rid_var.set(rid)
    try:
        yield
    finally:
        _rid_var.reset(token)


def _clean_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    # Defensive: never emit potentially huge values
    cleaned: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, bytes)) and len(value) > 256:
            cleaned[key] = f"<{type(value).__name__}:{len(value)}>"
        else:
            cleaned[key] = value
    return cleaned


def log_timing(event: str, duration_ms: float, **fields: Any) -> None:
    if not timing_enabled():
        return
    payload = {
        "event": event,
        "ms": round(float(duration_ms), 2),
    }
    rid = get_rid()
    if rid:
        payload["rid"] = rid
    payload.update(_clean_fields(fields))
    _TIMING_LOGGER.info("timing", extra=payload)


@contextmanager
def timed(event: str, **fields: Any) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        log_timing(event, (time.perf_counter() - start) * 1000.0, **fields)


@asynccontextmanager
async def atimed(event: str, **fields: Any) -> AsyncIterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        log_timing(event, (time.perf_counter() - start) * 1000.0, **fields)
