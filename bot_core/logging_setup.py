from __future__ import annotations

import logging
import os
import sys
from typing import Iterable, Optional


class _CleanLogFilter(logging.Filter):
    """Filter out noisy third-party logs so user can share only useful lines.

    Controlled by env:
    - LOG_PRESET=clean|verbose (default: clean)
    - SHOW_THIRD_PARTY_LOGS=1 to allow third-party INFO/DEBUG
    - WEASYPRINT_LOG_ASSET_ERRORS=1 to keep "Failed to load image" errors
    """

    def __init__(self) -> None:
        super().__init__()
        self._show_third_party = (os.getenv("SHOW_THIRD_PARTY_LOGS", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
        self._keep_weasy_asset_errors = (os.getenv("WEASYPRINT_LOG_ASSET_ERRORS", "0") or "").strip().lower() in {"1", "true", "yes", "on"}

        # Loggers we almost always want to suppress unless warning/error.
        self._third_party_prefixes: tuple[str, ...] = (
            "telegram",
            "httpx",
            "urllib3",
            "aiohttp",
            "asyncio",
            "playwright",
            "websockets",
            "uvicorn.access",
            "multipart",
        )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        name = record.name or ""

        # Keep our own logs always.
        if name.startswith("dejavu") or name.startswith("bot_core") or name == "__main__":
            return True

        # WeasyPrint is extremely noisy (CSS warnings). Keep real errors but drop
        # asset 404 errors by default (they are usually non-fatal and spammy).
        if name.startswith("weasyprint"):
            msg = record.getMessage()
            if record.levelno < logging.ERROR:
                return False
            if ("Failed to load image" in msg or "Failed to load" in msg) and not self._keep_weasy_asset_errors:
                return False
            return True

        # Third-party logs: in clean mode, keep only warnings/errors (unless explicitly enabled).
        if name.startswith(self._third_party_prefixes):
            if self._show_third_party:
                return True
            return record.levelno >= logging.WARNING

        # Default: keep warnings/errors.
        return record.levelno >= logging.WARNING


def _parse_level(raw: Optional[str], default: int) -> int:
    if not raw:
        return default
    value = raw.strip().upper()
    return getattr(logging, value, default)


def configure_logging() -> None:
    """Central logging config.

    This replaces ad-hoc logging.basicConfig calls so logs are consistent and shareable.

    Env:
    - LOG_PRESET=clean|verbose (default: clean)
    - LOG_LEVEL=INFO|DEBUG|WARNING (default: INFO for verbose, INFO for clean)
    - TELEGRAM_LOG_LEVEL=... (default: DEBUG for verbose, WARNING for clean)
    - UVICORN_ACCESS_LOG_LEVEL=... (default: WARNING for clean)
    """

    preset = (os.getenv("LOG_PRESET", "clean") or "clean").strip().lower()
    verbose = preset in {"verbose", "debug"}

    root_level = _parse_level(os.getenv("LOG_LEVEL"), logging.INFO)

    # Reset existing handlers to avoid duplicates when running under reload/supervisors.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    fmt = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s %(levelname)s %(name)s:%(lineno)d | %(message)s",
    )
    handler.setFormatter(logging.Formatter(fmt))

    if not verbose:
        handler.addFilter(_CleanLogFilter())

    root.addHandler(handler)
    root.setLevel(root_level)

    # Per-library level controls.
    telegram_default = "DEBUG" if verbose else "WARNING"
    telegram_level = _parse_level(os.getenv("TELEGRAM_LOG_LEVEL", telegram_default), logging.WARNING)
    logging.getLogger("telegram").setLevel(telegram_level)
    logging.getLogger("telegram.ext").setLevel(telegram_level)

    # Silence common noisy libs.
    logging.getLogger("httpx").setLevel(_parse_level(os.getenv("HTTPX_LOG_LEVEL", "WARNING"), logging.WARNING))
    logging.getLogger("aiohttp").setLevel(_parse_level(os.getenv("AIOHTTP_LOG_LEVEL", "WARNING"), logging.WARNING))
    logging.getLogger("asyncio").setLevel(_parse_level(os.getenv("ASYNCIO_LOG_LEVEL", "WARNING"), logging.WARNING))

    # Uvicorn access logs are mostly noise for sharing.
    access_default = "INFO" if verbose else "WARNING"
    logging.getLogger("uvicorn.access").setLevel(_parse_level(os.getenv("UVICORN_ACCESS_LOG_LEVEL", access_default), logging.WARNING))

    # WeasyPrint is extremely noisy; keep only errors by default in clean preset.
    if not verbose:
        logging.getLogger("weasyprint").setLevel(logging.ERROR)


def get_recommended_clean_log_env() -> dict[str, str]:
    """Convenience helper for documentation/debug."""

    return {
        "LOG_PRESET": "clean",
        "LOG_LEVEL": "INFO",
        "TELEGRAM_LOG_LEVEL": "WARNING",
        "SHOW_THIRD_PARTY_LOGS": "0",
        "WEASYPRINT_LOG_ASSET_ERRORS": "0",
    }
