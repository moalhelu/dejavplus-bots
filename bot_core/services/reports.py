# -*- coding: utf-8 -*-
"""Carfax/VIN report generation helpers extracted from the Telegram monolith."""
from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from html import escape, unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import aiohttp

from bot_core.config import get_env
from bot_core.services.pdf import html_to_pdf_bytes_chromium, fetch_page_html_chromium, PdfBusyError
from bot_core.services.translation import inject_rtl, translate_html, translate_html_google_free, _latin_ku_to_arabic  # type: ignore
from bot_core.telemetry import atimed, get_rid

try:  # optional dependency (used for quick HTML translation fallback)
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional
    BeautifulSoup = None
from bot_core.utils.vin import normalize_vin


LOGGER = logging.getLogger(__name__)


def _empty_errors() -> List[str]:
    return []


_HTTP_SESSION: Optional[aiohttp.ClientSession] = None
_HTTP_SESSION_LOCK = asyncio.Lock()
_CARFAX_SEM = asyncio.Semaphore(15)
_CARFAX_TIMEOUT = float(os.getenv("CARFAX_HTTP_TIMEOUT", "20") or 20)

_REPORT_TOTAL_TIMEOUT_SEC = float(os.getenv("REPORT_TOTAL_TIMEOUT_SEC", "25") or 25)
_REPORT_TOTAL_TIMEOUT_SEC = max(3.0, min(_REPORT_TOTAL_TIMEOUT_SEC, 60.0))

# Backpressure: bound concurrent end-to-end report generation so heavy load doesn't
# push all requests into timeouts after partial progress.
_REPORT_MAX_CONCURRENCY = int(os.getenv("REPORT_MAX_CONCURRENCY", "6") or 6)
_REPORT_MAX_CONCURRENCY = max(1, min(_REPORT_MAX_CONCURRENCY, 50))
_REPORT_GEN_SEM = asyncio.Semaphore(_REPORT_MAX_CONCURRENCY)

_REPORT_QUEUE_TIMEOUT_SEC = float(os.getenv("REPORT_QUEUE_TIMEOUT_SEC", "15.0") or 15.0)
_REPORT_QUEUE_TIMEOUT_SEC = max(0.05, min(_REPORT_QUEUE_TIMEOUT_SEC, 10.0))

# Success-cache + in-flight de-dupe to avoid repeated expensive fetch/render when users retry
# (e.g., due to impatience or webhook retries).
_REPORT_CACHE_DIR = (os.getenv("REPORT_CACHE_DIR", "temp_static/report_cache") or "temp_static/report_cache").strip() or "temp_static/report_cache"
_REPORT_CACHE_TTL_SEC = float(os.getenv("REPORT_CACHE_TTL_SEC", "86400") or 86400)  # 24h
_REPORT_CACHE_TTL_SEC = max(60.0, min(_REPORT_CACHE_TTL_SEC, 7 * 86400.0))
_REPORT_CACHE_MAX_BYTES = int(os.getenv("REPORT_CACHE_MAX_BYTES", str(250 * 1024 * 1024)) or (250 * 1024 * 1024))
_REPORT_CACHE_MAX_BYTES = max(10 * 1024 * 1024, min(_REPORT_CACHE_MAX_BYTES, 5 * 1024 * 1024 * 1024))

# Cache schema version (bump to invalidate previously cached PDFs).
# This prevents serving any legacy/placeholder outputs.
_REPORT_CACHE_SCHEMA = os.getenv("REPORT_CACHE_SCHEMA", "2") or "2"

_INFLIGHT_LOCK = asyncio.Lock()
_INFLIGHT: Dict[str, asyncio.Task[ReportResult]] = {}


def _cache_key(vin: str, lang_code: str, variant: str) -> str:
    v = (variant or "fast").strip().lower()
    if v not in {"fast", "full"}:
        v = "fast"
    base = f"{_REPORT_CACHE_SCHEMA}:{normalize_vin(vin) or vin}:{(lang_code or 'en').lower()}:{v}"
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def _cache_paths(key: str) -> tuple[Path, Path]:
    base = Path(_REPORT_CACHE_DIR)
    return base / f"{key}.pdf", base / f"{key}.json"


def _cache_cleanup_best_effort() -> None:
    try:
        base = Path(_REPORT_CACHE_DIR)
        if not base.exists() or not base.is_dir():
            return

        entries: List[tuple[float, int, Path, Path]] = []
        total = 0
        now = time.time()
        for meta_path in base.glob("*.json"):
            try:
                pdf_path = meta_path.with_suffix(".pdf")
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                ts = float(meta.get("ts") or 0.0)
                if ts and (now - ts) > _REPORT_CACHE_TTL_SEC:
                    try:
                        meta_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        pdf_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                size = int(meta.get("bytes") or (pdf_path.stat().st_size if pdf_path.exists() else 0))
                total += size
                entries.append((ts, size, pdf_path, meta_path))
            except Exception:
                continue

        if total <= _REPORT_CACHE_MAX_BYTES:
            return
        # Evict oldest first.
        entries.sort(key=lambda x: x[0] or 0.0)
        for ts, size, pdf_path, meta_path in entries:
            if total <= _REPORT_CACHE_MAX_BYTES:
                break
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception:
                pass
            total -= max(0, int(size))
    except Exception:
        return


def _cache_get(vin: str, lang_code: str, variant: str) -> Optional[bytes]:
    try:
        key = _cache_key(vin, lang_code, variant)
        pdf_path, meta_path = _cache_paths(key)
        if not pdf_path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ts = float(meta.get("ts") or 0.0)
        if not ts or (time.time() - ts) > _REPORT_CACHE_TTL_SEC:
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        data = pdf_path.read_bytes()
        if not _pdf_bytes_looks_ok(data):
            return None
        return data
    except Exception:
        return None


def _cache_put(vin: str, lang_code: str, variant: str, pdf_bytes: bytes) -> None:
    if not _pdf_bytes_looks_ok(pdf_bytes):
        return
    try:
        base = Path(_REPORT_CACHE_DIR)
        base.mkdir(parents=True, exist_ok=True)
        key = _cache_key(vin, lang_code, variant)
        pdf_path, meta_path = _cache_paths(key)
        pdf_path.write_bytes(pdf_bytes)
        meta_path.write_text(
            json.dumps({"ts": time.time(), "bytes": len(pdf_bytes)}, ensure_ascii=False),
            encoding="utf-8",
        )
        _cache_cleanup_best_effort()
    except Exception:
        return

_CARFAX_QUEUE_TIMEOUT_SEC = float(os.getenv("CARFAX_QUEUE_TIMEOUT_SEC", "1.0") or 1.0)
_CARFAX_QUEUE_TIMEOUT_SEC = max(0.05, min(_CARFAX_QUEUE_TIMEOUT_SEC, 10.0))

_PDF_QUEUE_TIMEOUT_MS = int(os.getenv("PDF_QUEUE_TIMEOUT_MS", "1500") or 1500)
_PDF_QUEUE_TIMEOUT_MS = max(50, min(_PDF_QUEUE_TIMEOUT_MS, 30_000))

# ---------------------------------------------------------------------------
# Fast Mode budgets (strict per-stage timeboxes)
# ---------------------------------------------------------------------------

_FAST_MODE_ENABLED = (os.getenv("FAST_MODE_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "off"}

_FETCH_BUDGET_SEC = float(os.getenv("FETCH_BUDGET_SEC", "8") or 8)
_PDF_BUDGET_SEC = float(os.getenv("PDF_BUDGET_SEC", "3") or 3)
_TRANSLATE_BUDGET_SEC = float(os.getenv("TRANSLATE_BUDGET_SEC", "2") or 2)
_TOTAL_BUDGET_SEC = float(os.getenv("TOTAL_BUDGET_SEC", "10") or 10)

_FETCH_BUDGET_SEC = max(0.5, min(_FETCH_BUDGET_SEC, 60.0))
_PDF_BUDGET_SEC = max(0.5, min(_PDF_BUDGET_SEC, 60.0))
_TRANSLATE_BUDGET_SEC = max(0.25, min(_TRANSLATE_BUDGET_SEC, 30.0))
_TOTAL_BUDGET_SEC = max(1.0, min(_TOTAL_BUDGET_SEC, 120.0))


def _budget_s(remaining_s: float, stage_budget_s: float) -> float:
    return max(0.0, min(float(remaining_s), float(stage_budget_s)))


def _carfax_parallel_primary_enabled() -> bool:
    return (os.getenv("CARFAX_PARALLEL_PRIMARY", "0") or "").strip().lower() in {"1", "true", "yes", "on"}


def _prefer_http_fetch_for_en_enabled() -> bool:
    # Default enabled because it's a safe fast-path: short HTTP timeout, renderability
    # heuristics, and fallback to Chromium URL rendering.
    return (os.getenv("PREFER_HTTP_FETCH_FOR_EN", "1") or "").strip().lower() in {"1", "true", "yes", "on"}


def _html_looks_renderable(html: str) -> bool:
    """Heuristic to decide whether a raw HTTP HTML response is worth rendering.

    If it looks incomplete (JS-required splash / too small), we fall back to Playwright URL.
    """

    if not html:
        return False
    low = html.lower()
    # Very small HTML often means a redirect/splash or JS boot page.
    if len(html) < 8_000:
        return False
    # Common JS-required placeholders.
    if "enable javascript" in low or "please enable javascript" in low:
        return False
    if "checking your browser" in low or "cloudflare" in low and "challenge" in low:
        return False
    return True


def _pdf_bytes_looks_ok(pdf_bytes: Optional[bytes]) -> bool:
    if not pdf_bytes:
        return False
    if not pdf_bytes.startswith(b"%PDF"):
        return False
    raw_min = (os.getenv("PDF_MIN_BYTES_OK", "12000") or "12000").strip()
    try:
        min_bytes = int(raw_min)
    except Exception:
        min_bytes = 12000
    min_bytes = max(4000, min(min_bytes, 200_000))
    if len(pdf_bytes) < min_bytes:
        return False
    head = pdf_bytes[:200_000]
    if b"/Type /Page" in head or b"/Type/Pages" in head or b"/Pages" in head:
        return True
    return True


def _en_hedged_render_enabled() -> bool:
    return (os.getenv("EN_HEDGED_RENDER", "1") or "").strip().lower() in {"1", "true", "yes", "on"}


def _en_hedge_delay_ms() -> int:
    raw = (os.getenv("EN_HEDGE_DELAY_MS", "700") or "").strip()
    try:
        val = int(raw)
    except Exception:
        val = 700
    return max(0, min(val, 5_000))


def _translate_hedge_enabled() -> bool:
    return (os.getenv("TRANSLATE_HEDGE", "1") or "").strip().lower() in {"1", "true", "yes", "on"}


def _translate_hedge_delay_ms() -> int:
    raw = (os.getenv("TRANSLATE_HEDGE_DELAY_MS", "250") or "").strip()
    try:
        val = int(raw)
    except Exception:
        val = 250
    return max(0, min(val, 3_000))


def _t(key: str, lang: str, _fallback: Optional[str] = None, **kwargs: Any) -> str:
    """Lazy translation helper to avoid hardcoded strings."""

    try:
        from bot_core import bridge as _bridge  # Lazy import to avoid circular on module load

        return _bridge.t(key, lang, **kwargs)
    except Exception:
        if _fallback is not None:
            try:
                return _fallback.format(**kwargs)
            except Exception:
                return _fallback
        return key


async def _get_http_session() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            return _HTTP_SESSION
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=0, enable_cleanup_closed=True, ttl_dns_cache=60)
        _HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_CARFAX_TIMEOUT),
            connector=connector,
        )
        return _HTTP_SESSION


async def close_http_session() -> None:
    """Close the shared reports ClientSession on shutdown."""

    global _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            await _HTTP_SESSION.close()
        _HTTP_SESSION = None


@dataclass(slots=True)
class ReportResult:
    """Structured outcome for VIN report generation."""

    success: bool
    user_message: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    pdf_filename: Optional[str] = None
    vin: Optional[str] = None
    errors: List[str] = field(default_factory=_empty_errors)
    raw_response: Optional[Dict[str, Any]] = None
    error_class: Optional[str] = None


ERROR_UPSTREAM_FETCH_FAILED = "UPSTREAM_FETCH_FAILED"
ERROR_PDF_RENDER_FAILED = "PDF_RENDER_FAILED"


class UpstreamFetchFailed(RuntimeError):
    pass


class PdfRenderFailed(RuntimeError):
    pass


async def generate_vin_report(vin: str, *, language: str = "en", fast_mode: bool = True) -> ReportResult:
    """Fetch a VIN report from the upstream API and return a PDF (if possible)."""

    requested_lang = (language or "en").strip().lower()
    normalized_vin = normalize_vin(vin)
    if not normalized_vin:
        return ReportResult(success=False, user_message=_t("report.invalid_vin", requested_lang, "❌ رقم VIN غير صالح."), errors=["invalid_vin"])

    effective_fast = bool(fast_mode) and _FAST_MODE_ENABLED

    # FAST/base delivery must always be the official upstream PDF.
    # Language is handled as optional derived output.
    cache_lang = "en" if effective_fast else requested_lang

    cached = _cache_get(normalized_vin, cache_lang, "fast" if effective_fast else "full")
    if cached:
        return ReportResult(
            success=True,
            pdf_bytes=cached,
            pdf_filename=f"{normalized_vin}.pdf",
            vin=normalized_vin,
        )

    inflight_key = f"{normalized_vin}:{cache_lang}:{'fast' if effective_fast else 'full'}"
    async with _INFLIGHT_LOCK:
        task = _INFLIGHT.get(inflight_key)
        if task is None:
            async def _runner() -> ReportResult:
                try:
                    if effective_fast:
                        result = await asyncio.wait_for(
                            _generate_vin_report_inner(normalized_vin, requested_lang, True),
                            timeout=float(_TOTAL_BUDGET_SEC),
                        )
                    else:
                        result = await _generate_vin_report_inner(normalized_vin, requested_lang, False)
                    if result.success and result.pdf_bytes:
                        _cache_put(normalized_vin, cache_lang, "fast" if effective_fast else "full", result.pdf_bytes)
                    return result
                finally:
                    async with _INFLIGHT_LOCK:
                        existing = _INFLIGHT.get(inflight_key)
                        if existing is not None and existing.done():
                            _INFLIGHT.pop(inflight_key, None)

            task = asyncio.create_task(_runner())
            _INFLIGHT[inflight_key] = task

    # Do not let a single waiter cancellation cancel the shared generation.
    return await asyncio.shield(task)


async def _generate_vin_report_inner(normalized_vin: str, requested_lang: str, fast_mode: bool) -> ReportResult:
    """Inner implementation for generate_vin_report (no caching)."""

    from bot_core.utils.pdf_format import validate_pdf_format

    start_t = time.perf_counter()
    total_budget = _TOTAL_BUDGET_SEC if fast_mode else _REPORT_TOTAL_TIMEOUT_SEC
    deadline = start_t + float(total_budget)

    def _remaining_s() -> float:
        return max(0.0, deadline - time.perf_counter())

    # Fail fast under load instead of timing out late after progress reaches ~90%.
    try:
        queue_cap = 1.0 if fast_mode else _REPORT_QUEUE_TIMEOUT_SEC
        acquire_s = min(queue_cap, max(0.05, _remaining_s()))
        await asyncio.wait_for(_REPORT_GEN_SEM.acquire(), timeout=acquire_s)
    except Exception:
        return ReportResult(
            success=False,
            user_message=_t("report.error.timeout", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن الوقت المحدد."),
            errors=["queue_timeout"],
            vin=normalized_vin,
        )

    api_response: Dict[str, Any] = {}
    pdf_bytes: Optional[bytes] = None
    skipped_translation = False
    delivered_lang = requested_lang
    fetch_sec = 0.0
    pdf_sec = 0.0
    format_ok = False
    cache_hit_base = False
    cache_hit_lang = False

    try:
        # Primary path (ALL modes): fetch official upstream PDF bytes whenever possible.
        # Playwright rendering is a last resort.
        prefer_non_pdf = False
        fetch_budget = _budget_s(_remaining_s(), _FETCH_BUDGET_SEC) if fast_mode else _remaining_s()
        t_fetch0 = time.perf_counter()
        async with atimed("report.fetch", vin=normalized_vin, lang=requested_lang, prefer_non_pdf=prefer_non_pdf, budget_s=fetch_budget, fast=bool(fast_mode)):
            # First: try official PDF bytes.
            pdf_primary = await fetch_report_pdf_bytes(
                normalized_vin,
                options=None,
                lang=(requested_lang or "en"),
                total_timeout_s=fetch_budget,
                deadline=deadline,
                force_fresh=False,
            )

            if pdf_primary:
                api_response = {"ok": True, "pdf_bytes": pdf_primary, "filename": f"{normalized_vin}.pdf"}
            else:
                # Fallback: fetch JSON/HTML metadata from upstream.
                prefer_non_pdf = (not fast_mode) and ((requested_lang or "en") != "en")
                api_response = await _call_carfax_api(
                    normalized_vin,
                    prefer_non_pdf=prefer_non_pdf,
                    total_timeout_s=fetch_budget,
                    deadline=deadline,
                )
        fetch_sec = max(0.0, time.perf_counter() - t_fetch0)

        LOGGER.info(
            "report.fastmode fetch_done vin=%s lang=%s fast=%s remaining=%.2fs",
            normalized_vin,
            requested_lang,
            bool(fast_mode),
            _remaining_s(),
        )

        if not api_response.get("ok"):
            err = api_response.get("error") or f"HTTP_{api_response.get('status','NA')}"
            if str(err).lower() in {"busy", "queue_timeout", "deadline_exceeded"}:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.timeout", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن الوقت المحدد."),
                    errors=[str(err)],
                    vin=normalized_vin,
                    raw_response=api_response,
                    error_class=ERROR_UPSTREAM_FETCH_FAILED,
                )
            return ReportResult(
                success=False,
                user_message=_t("report.error.fetch_detailed", requested_lang, "⚠️ فشل جلب تقرير VIN: {error}", error=err),
                errors=[str(err)],
                vin=normalized_vin,
                raw_response=api_response,
                error_class=ERROR_UPSTREAM_FETCH_FAILED,
            )

        # If we have official upstream PDF bytes, use them immediately for ALL modes.
        pdf_bytes = api_response.get("pdf_bytes")
        if pdf_bytes:
            # Validate official format gate BEFORE returning.
            chk = validate_pdf_format(bytes(pdf_bytes), expected_vin=normalized_vin, require_official_tokens=True)
            if not chk.ok:
                # One fresh retry (best-effort) to avoid cached/bad upstream variants.
                retry_budget = max(0.25, _remaining_s())
                try:
                    pdf2 = await fetch_report_pdf_bytes(
                        normalized_vin,
                        options=None,
                        lang=(requested_lang or "en"),
                        total_timeout_s=retry_budget,
                        deadline=deadline,
                        force_fresh=True,
                    )
                    if pdf2:
                        chk2 = validate_pdf_format(bytes(pdf2), expected_vin=normalized_vin, require_official_tokens=True)
                        if chk2.ok:
                            pdf_bytes = pdf2
                            chk = chk2
                except Exception:
                    pass

            if not chk.ok:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.generic", requested_lang, "⚠️ تعذّر إكمال الطلب."),
                    errors=[f"format_mismatch:{chk.reason}"],
                    vin=normalized_vin,
                    raw_response=api_response,
                    error_class=ERROR_UPSTREAM_FETCH_FAILED,
                )

            # If requested language != EN, deliver official report (EN) and mark translation as skipped.
            if (requested_lang or "en") != "en":
                skipped_translation = True
                delivered_lang = "en"

            try:
                api_response.setdefault("_dv_fast", {})
                api_response["_dv_fast"].update(
                    {
                        "fast_mode": bool(fast_mode),
                        "skipped_translation": bool(skipped_translation),
                        "requested_lang": (requested_lang or "en"),
                        "delivered_lang": delivered_lang,
                        "format_ok": True,
                        "fetch_sec": round(float(fetch_sec), 3),
                        "pdf_sec": 0.0,
                        "total_sec": round(float(time.perf_counter() - start_t), 3),
                    }
                )
            except Exception:
                pass

            filename = api_response.get("filename", f"{normalized_vin}.pdf")
            return ReportResult(
                success=True,
                user_message=_t("report.success.pdf_direct", requested_lang, "✅ Report ready."),
                pdf_bytes=bytes(pdf_bytes),
                pdf_filename=filename,
                vin=normalized_vin,
                raw_response=api_response,
            )

        # FAST SLA: if upstream did not provide PDF bytes, treat as upstream fetch failure.
        if fast_mode:
            return ReportResult(
                success=False,
                user_message=_t("report.error.fetch", requested_lang, "⚠️ Failed to fetch report from upstream."),
                errors=["no_pdf"],
                vin=normalized_vin,
                raw_response=api_response,
                error_class=ERROR_UPSTREAM_FETCH_FAILED,
            )

            # Validate official format gate BEFORE returning.
            chk = validate_pdf_format(bytes(pdf_bytes), expected_vin=normalized_vin, require_official_tokens=True)
            if not chk.ok:
                # One fresh retry (best-effort) to avoid cached/bad upstream variants.
                retry_budget = max(0.25, _remaining_s())
                try:
                    api_response2 = await _call_carfax_api(
                        normalized_vin,
                        prefer_non_pdf=False,
                        total_timeout_s=retry_budget,
                        deadline=deadline,
                        force_fresh=True,
                    )
                    pdf2 = api_response2.get("pdf_bytes")
                    if pdf2:
                        chk2 = validate_pdf_format(bytes(pdf2), expected_vin=normalized_vin, require_official_tokens=True)
                        if chk2.ok:
                            api_response = api_response2
                            pdf_bytes = pdf2
                            chk = chk2
                except Exception:
                    pass

            format_ok = bool(chk.ok)
            if not format_ok:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.generic", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن SLA الوقت."),
                    errors=[f"format_mismatch:{chk.reason}"],
                    vin=normalized_vin,
                    raw_response=api_response,
                )

            # Requested language != EN: deliver base official report now; localized can follow asynchronously.
            if (requested_lang or "en") != "en":
                skipped_translation = True
                delivered_lang = "en"

            try:
                api_response["_dv_fast"] = {
                    "fast_mode": True,
                    "skipped_translation": bool(skipped_translation),
                    "requested_lang": (requested_lang or "en"),
                    "delivered_lang": delivered_lang,
                    "format_ok": True,
                    "fetch_sec": round(float(fetch_sec), 3),
                    "pdf_sec": 0.0,
                    "total_sec": round(float(time.perf_counter() - start_t), 3),
                }
            except Exception:
                pass

            try:
                LOGGER.info(
                    "sla.base rid=%s vin=%s req_lang=%s del_lang=%s cache_hit_base=%s format_ok=%s fetch_sec=%.3f total_sec=%.3f outcome=success",
                    get_rid() or "-",
                    normalized_vin,
                    requested_lang,
                    delivered_lang,
                    bool(cache_hit_base),
                    True,
                    float(fetch_sec),
                    float(time.perf_counter() - start_t),
                )
            except Exception:
                pass

            filename = api_response.get("filename", f"{normalized_vin}.pdf")
            return ReportResult(
                success=True,
                user_message=_t("report.success.pdf_direct", requested_lang, "✅ Report ready."),
                pdf_bytes=bytes(pdf_bytes),
                pdf_filename=filename,
                vin=normalized_vin,
                raw_response=api_response,
            )

        # In Fast Mode, prefer using the full remaining SLA time for PDF rendering.
        pdf_budget = _remaining_s()
        t_pdf0 = time.perf_counter()
        pdf_timed_out = False
        try:
            async with atimed("report.render_pdf", vin=normalized_vin, lang=lang_code, budget_s=pdf_budget, fast=bool(fast_mode)):
                if fast_mode:
                    pdf_bytes, skipped_translation = await asyncio.wait_for(
                        _render_pdf_from_response(api_response, normalized_vin, lang_code, deadline=deadline, fast_mode=True),
                        timeout=max(0.25, pdf_budget),
                    )
                else:
                    pdf_bytes, _ = await _render_pdf_from_response(api_response, normalized_vin, lang_code, deadline=deadline, fast_mode=False)
        except UpstreamFetchFailed as exc:
            try:
                LOGGER.warning(
                    "report upstream fetch failed rid=%s vin=%s error_class=%s reason=%s",
                    get_rid() or "-",
                    normalized_vin,
                    ERROR_UPSTREAM_FETCH_FAILED,
                    str(exc),
                )
            except Exception:
                pass
            return ReportResult(
                success=False,
                user_message=_t("report.error.fetch", lang_code, "⚠️ Failed to fetch report from upstream."),
                errors=["upstream_fetch_failed"],
                vin=normalized_vin,
                raw_response=api_response,
                error_class=ERROR_UPSTREAM_FETCH_FAILED,
            )
        except PdfBusyError:
            # Fast SLA: perform ONE reset+retry if the PDF engine is saturated.
            if fast_mode:
                try:
                    from bot_core.services.pdf import close_pdf_engine

                    await close_pdf_engine()
                    retry_budget = _remaining_s()
                    async with atimed("report.render_pdf_retry", vin=normalized_vin, lang=lang_code, budget_s=retry_budget):
                        pdf_bytes, skipped_translation = await asyncio.wait_for(
                            _render_pdf_from_response(api_response, normalized_vin, lang_code, deadline=deadline, fast_mode=True),
                            timeout=max(0.25, retry_budget),
                        )
                except Exception:
                    pdf_bytes = None
            if not pdf_bytes:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.timeout", lang_code, "⚠️ تعذّر إكمال الطلب ضمن SLA الوقت."),
                    errors=["pdf_busy"],
                    vin=normalized_vin,
                    raw_response=api_response,
                    error_class=ERROR_PDF_RENDER_FAILED,
                )
        except asyncio.TimeoutError:
            pdf_timed_out = True
        finally:
            pdf_sec = max(0.0, time.perf_counter() - t_pdf0)
        if pdf_timed_out and not pdf_bytes:
            return ReportResult(
                success=False,
                user_message=_t("report.error.timeout", lang_code, "⚠️ تعذّر إكمال الطلب ضمن SLA الوقت."),
                errors=["pdf_timeout"],
                vin=normalized_vin,
                raw_response=api_response,
                error_class=ERROR_PDF_RENDER_FAILED,
            )
        if not pdf_bytes:
            return ReportResult(
                success=False,
                user_message=_t("report.error.pdf_render", lang_code, "⚠️ Failed to generate PDF."),
                errors=["pdf_generation_failed"],
                vin=normalized_vin,
                raw_response=api_response,
                error_class=ERROR_PDF_RENDER_FAILED,
            )

        # Language SLA policy:
        # If translation isn't ready within budget, deliver English FAST PDF + note (delivery layers).
        if fast_mode and requested_lang != "en" and skipped_translation:
            delivered_lang = "en"
            try:
                cached_en = _cache_get(normalized_vin, "en", "fast")
                if cached_en:
                    pdf_bytes = cached_en
                else:
                    rem = max(0.25, _budget_s(_remaining_s(), _PDF_BUDGET_SEC))
                    pdf_bytes_en, _ = await asyncio.wait_for(
                        _render_pdf_from_response(api_response, normalized_vin, "en", deadline=deadline, fast_mode=True),
                        timeout=rem,
                    )
                    if pdf_bytes_en:
                        pdf_bytes = pdf_bytes_en
                        _cache_put(normalized_vin, "en", "fast", pdf_bytes_en)
            except Exception:
                # If English fallback can't be produced, keep the existing PDF.
                delivered_lang = requested_lang

        # Stash Fast Mode decisions into raw_response so delivery layers can message consistently
        # *after* the PDF is actually delivered.
        try:
            api_response["_dv_fast"] = {
                "fast_mode": bool(fast_mode),
                "skipped_translation": bool(skipped_translation),
                "requested_lang": requested_lang,
                "delivered_lang": delivered_lang,
                "fetch_budget_s": float(_FETCH_BUDGET_SEC),
                "translate_budget_s": float(_TRANSLATE_BUDGET_SEC),
                "pdf_budget_s": float(_PDF_BUDGET_SEC),
                "total_budget_s": float(_TOTAL_BUDGET_SEC if fast_mode else _REPORT_TOTAL_TIMEOUT_SEC),
                "fetch_sec": round(float(fetch_sec), 3),
                "pdf_sec": round(float(pdf_sec), 3),
                "total_sec": round(float(time.perf_counter() - start_t), 3),
            }
        except Exception:
            pass

        try:
            LOGGER.info(
                "sla.fast_report rid=%s vin=%s req_lang=%s del_lang=%s fast=%s fetch_sec=%.3f pdf_sec=%.3f total_sec=%.3f outcome=success",
                get_rid() or "-",
                normalized_vin,
                requested_lang,
                delivered_lang,
                bool(fast_mode),
                float(fetch_sec),
                float(pdf_sec),
                float(time.perf_counter() - start_t),
            )
        except Exception:
            pass

        LOGGER.info(
            "report.fastmode done vin=%s lang=%s fast=%s skipped_translation=%s fallback_pdf=%s",
            normalized_vin,
            lang_code,
            bool(fast_mode),
            bool(skipped_translation),
            False,
        )

        result = ReportResult(
            success=True,
            user_message=_t("report.success.pdf_created", lang_code, "✅ تم إنشاء ملف PDF للتقرير."),
            pdf_bytes=pdf_bytes,
            pdf_filename=f"{normalized_vin}.pdf",
            vin=normalized_vin,
            raw_response=api_response,
        )
        return result
    finally:
        try:
            _REPORT_GEN_SEM.release()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers copied from the legacy flow (refactored for reuse).
# ---------------------------------------------------------------------------

async def _call_carfax_api(
    vin: str,
    *,
    prefer_non_pdf: bool = False,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> Dict[str, Any]:
    cfg = get_env()
    api_url = cfg.api_url.strip()
    headers: Dict[str, str] = {}
    token = cfg.api_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if force_fresh:
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
    if prefer_non_pdf:
        headers["Accept"] = "application/json, text/html;q=0.9, */*;q=0.5"
    else:
        headers["Accept"] = "application/pdf, application/json;q=0.9, text/html;q=0.8, */*;q=0.5"
    if not api_url:
        return {"ok": False, "error": "API_URL غير مضبوط في .env"}

    base = api_url.rstrip("/")
    payload = {"vin": vin}
    if force_fresh:
        payload["_ts"] = int(time.time())
    session = await _get_http_session()

    # Cap request time budget (best-effort) so end-to-end report stays fast.
    try:
        budget = float(total_timeout_s) if total_timeout_s is not None else float(_CARFAX_TIMEOUT)
    except Exception:
        budget = float(_CARFAX_TIMEOUT)
    budget = max(0.5, min(budget, float(_CARFAX_TIMEOUT)))
    if deadline is not None:
        # Leave a small headroom so downstream stages can still run.
        rem = max(0.0, float(deadline) - time.perf_counter())
        budget = min(budget, max(0.5, rem - 0.25))
        if budget <= 0.55:
            return {"ok": False, "error": "deadline_exceeded"}
    request_timeout = aiohttp.ClientTimeout(total=budget)

    async def _handle(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        status = resp.status
        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        final_url = str(getattr(resp, "url", "") or "")
        if status not in (200, 201):
            try:
                txt = await resp.text()
            except Exception:
                txt = ""
            return {"ok": False, "status": status, "ctype": ctype, "err_text": txt, "final_url": final_url}
        if "application/pdf" in ctype:
            if prefer_non_pdf:
                return {"ok": False, "status": status, "ctype": ctype, "error": "pdf_returned", "final_url": final_url}
            data = await resp.read()
            return {"ok": True, "pdf_bytes": data, "filename": f"{vin}.pdf", "status": status, "final_url": final_url}
        if "application/json" in ctype:
            try:
                data = await resp.json()
                return {"ok": True, "json": data, "status": status, "final_url": final_url}
            except Exception:
                txt = await resp.text()
                return {"ok": True, "text": txt, "status": status, "final_url": final_url}
        txt = await resp.text()
        return {"ok": True, "text": txt, "status": status, "final_url": final_url}


async def fetch_report_pdf_bytes(
    vin: str,
    options: Optional[Dict[str, Any]] = None,
    lang: str = "en",
    *,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> Optional[bytes]:
    """Primary path: request official upstream PDF bytes when possible.

    Uses the same upstream API endpoint(s) as `_call_carfax_api` but forces
    `Accept: application/pdf`.
    """

    rid = get_rid() or "-"
    # NOTE: options/lang are currently best-effort metadata only. Upstream may ignore them.
    try:
        if options:
            # Keep stable: do not mutate upstream payload format here.
            pass
    except Exception:
        pass

    api_response = await _call_carfax_api(
        vin,
        prefer_non_pdf=False,
        total_timeout_s=total_timeout_s,
        deadline=deadline,
        force_fresh=force_fresh,
    )

    status = api_response.get("status")
    final_url = api_response.get("final_url")
    pdf_bytes = api_response.get("pdf_bytes")
    try:
        LOGGER.info(
            "upstream_pdf_fetch rid=%s vin=%s fetch_status=%s fetch_final_url=%s pdf_bytes_len=%s",
            rid,
            vin,
            status if status is not None else "na",
            final_url or "-",
            len(pdf_bytes) if isinstance(pdf_bytes, (bytes, bytearray)) else 0,
        )
    except Exception:
        pass

    if isinstance(pdf_bytes, (bytes, bytearray)) and pdf_bytes.startswith(b"%PDF"):
        return bytes(pdf_bytes)
    return None


def _sanitize_html_head(text: str, limit: int = 200) -> str:
    try:
        raw = (text or "").strip()
        raw = re.sub(r"\s+", " ", raw)
        raw = raw.replace("\x00", "")
        if len(raw) > limit:
            raw = raw[:limit]
        return raw
    except Exception:
        return ""


def _looks_like_login_or_error_page(html: str) -> bool:
    low = (html or "").lower()
    tokens = (
        "access denied",
        "forbidden",
        "unauthorized",
        "login",
        "sign in",
        "cloudflare",
        "captcha",
        "human verification",
        "not found",
        "404",
        "403",
        "rate limited",
        "temporarily unavailable",
    )
    return any(t in low for t in tokens)


async def _fetch_html_http_validated(
    url: str,
    *,
    deadline: Optional[float] = None,
    timeout_s: float = 8.0,
    max_bytes: int = 250_000,
) -> Dict[str, Any]:
    """HTTP-only HTML fetch with strict validation before rendering."""

    rid = get_rid() or "-"
    session = await _get_http_session()

    # Timebox relative to deadline.
    eff_timeout = float(timeout_s)
    if deadline is not None:
        rem = max(0.0, float(deadline) - time.perf_counter())
        eff_timeout = min(eff_timeout, max(0.5, rem - 0.25))
    eff_timeout = max(0.5, min(eff_timeout, 20.0))
    request_timeout = aiohttp.ClientTimeout(total=eff_timeout)

    try:
        async with atimed("upstream.html_fetch", url_host=(url.split("/", 3)[2] if "//" in url else "")):
            async with session.get(url, allow_redirects=True, timeout=request_timeout) as resp:
                status = int(resp.status)
                final_url = str(getattr(resp, "url", "") or "")
                # Read limited bytes to avoid huge pages.
                body = await resp.content.read(max_bytes)
                try:
                    html = body.decode(resp.charset or "utf-8", errors="ignore")
                except Exception:
                    html = body.decode("utf-8", errors="ignore")

                head = _sanitize_html_head(html, limit=200)
                try:
                    LOGGER.info(
                        "upstream_html rid=%s fetch_status=%s fetch_final_url=%s html_bytes_len=%s html_head=%s",
                        rid,
                        status,
                        final_url or "-",
                        len(body) if body is not None else 0,
                        head,
                    )
                except Exception:
                    pass

                if status != 200:
                    raise UpstreamFetchFailed(f"status_{status}")
                if _looks_like_login_or_error_page(html):
                    raise UpstreamFetchFailed("login_or_error_page")

                return {
                    "ok": True,
                    "status": status,
                    "final_url": final_url,
                    "html": html,
                    "html_bytes_len": len(body) if body is not None else 0,
                }
    except UpstreamFetchFailed:
        raise
    except Exception as exc:
        raise UpstreamFetchFailed(str(exc))

    async def _try_get() -> Optional[Dict[str, Any]]:
        try:
            async with atimed("carfax.http", method="GET", route="/{vin}"):
                url = f"{base}/{vin}"
                if force_fresh:
                    url = f"{url}?ts={int(time.time())}"
                async with session.get(url, headers=headers, timeout=request_timeout) as resp:
                    parsed = await _handle(resp)
                    if parsed.get("ok"):
                        return parsed
                    return None
        except Exception:
            return None

    async def _try_post_vin() -> Optional[Dict[str, Any]]:
        try:
            async with atimed("carfax.http", method="POST", route="/vin"):
                async with session.post(f"{base}/vin", json=payload, headers=headers, timeout=request_timeout) as resp:
                    parsed = await _handle(resp)
                    if parsed.get("ok"):
                        return parsed
                    return None
        except Exception:
            return None

    async def _try_post_base() -> Dict[str, Any]:
        try:
            async with atimed("carfax.http", method="POST", route="/"):
                async with session.post(base, json=payload, headers=headers, timeout=request_timeout) as resp:
                    parsed = await _handle(resp)
                    if parsed.get("ok"):
                        return parsed
                    return {"ok": False, "error": f"HTTP {parsed.get('status')}", "detail": parsed.get("err_text", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # Acquire Carfax slot with a bounded wait; otherwise fail fast as "busy".
    acquired = False
    try:
        queue_budget = _CARFAX_QUEUE_TIMEOUT_SEC
        if deadline is not None:
            queue_budget = min(queue_budget, max(0.05, float(deadline) - time.perf_counter()))
        await asyncio.wait_for(_CARFAX_SEM.acquire(), timeout=max(0.05, queue_budget))
        acquired = True

        if _carfax_parallel_primary_enabled():
            tasks = [asyncio.create_task(_try_get()), asyncio.create_task(_try_post_vin())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    result = d.result()
                    if result and result.get("ok"):
                        for p in pending:
                            p.cancel()
                        return result
                # If first completed was not ok, await the other one too.
                for p in pending:
                    try:
                        other = await p
                        if other and other.get("ok"):
                            return other
                    except Exception:
                        pass
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return await _try_post_base()

        parsed = await _try_get()
        if parsed:
            return parsed
        parsed = await _try_post_vin()
        if parsed:
            return parsed
        return await _try_post_base()

    except asyncio.TimeoutError:
        return {"ok": False, "error": "queue_timeout"}
    finally:
        if acquired:
            try:
                _CARFAX_SEM.release()
            except Exception:
                pass


def _extract_html_or_url_from_json(data: Any) -> Dict[str, Optional[str]]:
    """Extract a report URL/HTML from arbitrary JSON-ish structures.

    Some upstream APIs embed the report HTML as an HTML-escaped string (e.g. `&lt;html...`).
    We detect and unescape that so the PDF renderer sees real HTML (template) instead of
    a JSON dump.
    """

    url_keys = ("url", "html_url", "report_url", "viewerUrl", "reportLink")
    html_keys = ("html", "htmlContent", "report", "content", "body", "data")

    url: Optional[str] = None
    html: Optional[str] = None

    def _maybe_decode_html(value: str) -> Optional[str]:
        if not value:
            return None
        low = value.lower()
        if "<html" in low or "<!doctype" in low:
            return value
        if "&lt;html" in low or "&lt;!doctype" in low:
            decoded = unescape(value)
            dlow = decoded.lower()
            if "<html" in dlow or "<!doctype" in dlow:
                return decoded
        return None

    def _walk(node: Any) -> None:
        nonlocal url, html
        if node is None:
            return
        if isinstance(node, dict):
            mapping: Dict[str, Any] = cast(Dict[str, Any], node)
            if url is None:
                for key in url_keys:
                    value = mapping.get(key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        url = value
                        break
            if html is None:
                for key in html_keys:
                    value = mapping.get(key)
                    if isinstance(value, str):
                        decoded = _maybe_decode_html(value)
                        if decoded:
                            html = decoded
                            break
            for value in mapping.values():
                if url is not None and html is not None:
                    return
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for item in cast(List[Any], node):
                if url is not None and html is not None:
                    return
                _walk(item)

    _walk(data)
    return {"url": url, "html": html}


def _json_to_html_report(payload: Any, vin: str) -> str:
    """Render JSON-ish payload into a readable HTML report.

    This is used only when the upstream API returns JSON without a report URL/HTML.
    It's intentionally simple and stable so it prints well to PDF.
    """

    def _is_primitive(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

    def _render_primitive(value: Any) -> str:
        if value is None:
            return "<em>null</em>"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return escape(str(value))
        text = str(value)
        low = text.lower().strip()
        if low.startswith(("http://", "https://")):
            safe = escape(text)
            return f"<a href='{safe}'>{safe}</a>"
        if len(text) > 200:
            return f"<div style='white-space:pre-wrap'>{escape(text)}</div>"
        return escape(text)

    def _render(value: Any, depth: int = 0) -> str:
        if _is_primitive(value):
            return _render_primitive(value)
        if isinstance(value, list):
            if not value:
                return "<em>[]</em>"
            items = "".join(f"<li>{_render(v, depth + 1)}</li>" for v in value)
            return f"<ol style='margin:0; padding-inline-start: 1.2em'>{items}</ol>"
        if isinstance(value, dict):
            if not value:
                return "<em>{{}}</em>"
            rows = []
            for k, v in cast(Dict[str, Any], value).items():
                key = escape(str(k))
                rows.append(
                    "<tr>"
                    f"<th style='text-align:start; vertical-align:top; padding:6px; border:1px solid #ddd; width:28%'>{key}</th>"
                    f"<td style='vertical-align:top; padding:6px; border:1px solid #ddd'>{_render(v, depth + 1)}</td>"
                    "</tr>"
                )
            body = "".join(rows)
            return (
                "<table style='width:100%; border-collapse:collapse; table-layout:fixed'>"
                f"<tbody>{body}</tbody></table>"
            )
        return _render_primitive(str(value))

    content = _render(payload)
    return (
        "<html><head><meta charset='utf-8'>"
        "<style>"
        "body{font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.5}"
        "h3{margin:0 0 10px 0}"
        "table{word-break:break-word}"
        "</style>"
        "</head>"
        f"<body><h3>CarFax – {escape(vin)}</h3>{content}</body></html>"
    )


def _deadline_remaining_ms(deadline: Optional[float], *, floor_ms: int = 1_000, cap_ms: int = 120_000) -> int:
    if deadline is None:
        return cap_ms
    rem = max(0.0, float(deadline) - time.perf_counter())
    ms = int(rem * 1000.0)
    return max(floor_ms, min(ms, cap_ms))


async def _render_pdf_from_response(
    response: Dict[str, Any],
    vin: str,
    language: str,
    *,
    deadline: Optional[float] = None,
    fast_mode: bool = False,
) -> tuple[Optional[bytes], bool]:
    pdf_bytes: Optional[bytes] = None
    translation_skipped = False
    needs_translation = _needs_translation(language)
    json_payload = response.get("json")

    if json_payload is not None:
        extracted = _extract_html_or_url_from_json(json_payload)
        extracted_url = extracted.get("url")
        if extracted_url:
            pdf_bytes, translation_skipped = await _render_pdf_from_url(
                extracted_url,
                needs_translation,
                language,
                vin,
                deadline=deadline,
                fast_mode=bool(fast_mode),
            )
        else:
            html = extracted.get("html")
            if not html:
                html = _json_to_html_report(json_payload, vin)
            # If the JSON included embedded HTML but no URL, base_url remains None.
            pdf_bytes, translation_skipped = await _render_pdf_from_html(
                html,
                needs_translation,
                language,
                base_url=extracted_url,
                deadline=deadline,
                fast_mode=bool(fast_mode),
            )
    elif response.get("text"):
        text_payload = str(response["text"]).strip()
        if text_payload.startswith(("http://", "https://")):
            pdf_bytes, translation_skipped = await _render_pdf_from_url(
                text_payload,
                needs_translation,
                language,
                vin,
                deadline=deadline,
                fast_mode=bool(fast_mode),
            )
        else:
            html = text_payload if text_payload.lower().startswith("<html") else (
                f"<html><meta charset='utf-8'><body><h3>CarFax – {vin}</h3><pre style='white-space:pre-wrap'>{escape(text_payload)}</pre></body></html>"
            )
            pdf_bytes, translation_skipped = await _render_pdf_from_html(
                html,
                needs_translation,
                language,
                deadline=deadline,
                fast_mode=bool(fast_mode),
            )

    return pdf_bytes, bool(translation_skipped)


async def _render_pdf_from_url(
    url: str,
    needs_translation: bool,
    language: str,
    vin: str,
    *,
    deadline: Optional[float] = None,
    fast_mode: bool = False,
) -> tuple[Optional[bytes], bool]:
    html = None
    pdf_bytes = None
    translation_skipped = False

    # Validate upstream URL fetch via HTTP (status, redirects, login/error pages) before rendering.
    # This prevents misclassifying upstream 403/404/login pages as "PDF failed".
    validated = await _fetch_html_http_validated(
        url,
        deadline=deadline,
        timeout_s=(2.5 if fast_mode else 6.0),
        max_bytes=250_000,
    )
    final_url = str(validated.get("final_url") or url)

    if needs_translation:
        # Chromium-only: fetch rendered HTML via Chromium (keeps styling + logo),
        # translate + inject RTL, then print to PDF.
        async def _chromium_fast() -> Optional[str]:
            try:
                return await fetch_page_html_chromium(
                    url,
                    wait_until=("domcontentloaded" if fast_mode else _translated_fetch_wait_until()),
                    timeout_ms=min(_translated_fetch_timeout_ms(), _deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=30_000)),
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    block_resource_types={"image", "media"} if fast_mode else None,
                )
            except Exception:
                return None

        async def _http_only() -> Optional[str]:
            try:
                # Reuse validated HTML first.
                if validated.get("html"):
                    return str(validated.get("html"))
                return await _fetch_page_html_http_only(final_url)
            except Exception:
                return None

        # Run both; take the first usable HTML.
        t1 = asyncio.create_task(_chromium_fast())
        t2 = asyncio.create_task(_http_only())
        try:
            pending = {t1, t2}
            while pending and not html:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        candidate = task.result()
                    except Exception:
                        candidate = None
                    if candidate and "<html" in candidate.lower():
                        html = candidate
                        for p in pending:
                            p.cancel()
                        pending = set()
                        break
        finally:
            for t in (t1, t2):
                if not t.done():
                    t.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)

        # If still empty, fall back to full Chromium fetch (default wait_until).
        if not html:
            try:
                html = await fetch_page_html_chromium(
                    final_url,
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    wait_until=("domcontentloaded" if fast_mode else None),
                    timeout_ms=(min(int(_FETCH_BUDGET_SEC * 1000), _deadline_remaining_ms(deadline, floor_ms=700, cap_ms=30_000)) if fast_mode else None),
                    block_resource_types={"image", "media"} if fast_mode else None,
                )
            except Exception:
                html = None

        if html:
            html, translated_ok = await _maybe_translate_html(html, language, deadline=deadline, budget_s=_TRANSLATE_BUDGET_SEC if fast_mode else None)
            if not translated_ok:
                translation_skipped = True
            # Only RTL-wrap if we actually translated (Fast Mode) or if this is full mode.
            if (language or "en").lower() in {"ar", "ku", "ckb"} and (translated_ok or not fast_mode):
                html = inject_rtl(html, lang=language)
                if fast_mode:
                    pdf_bytes = await html_to_pdf_bytes_chromium(
                        html_str=html,
                        base_url=final_url,
                        timeout_ms=_deadline_remaining_ms(deadline, floor_ms=700, cap_ms=120_000),
                        acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                        wait_until="domcontentloaded",
                        fast_first_timeout_ms=700,
                        fast_first_wait_until="domcontentloaded",
                        block_resource_types={"image", "media"},
                    )
                else:
                    pdf_bytes = await html_to_pdf_bytes_chromium(
                        html_str=html,
                        base_url=final_url,
                        timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                        acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    )
        if not pdf_bytes:
            # Last-resort: print the original URL without translation.
            if fast_mode:
                pdf_bytes = await html_to_pdf_bytes_chromium(
                    url=final_url,
                    timeout_ms=_deadline_remaining_ms(deadline, floor_ms=700, cap_ms=120_000),
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    wait_until="domcontentloaded",
                    fast_first_timeout_ms=700,
                    fast_first_wait_until="domcontentloaded",
                    block_resource_types={"image", "media"},
                )
            else:
                pdf_bytes = await html_to_pdf_bytes_chromium(
                    url=final_url,
                    timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                )
    else:
        lang_code = (language or "en").lower()

        # English: hedge (parallelize) to reduce tail latency.
        # - Fast path: HTTP GET HTML (short timeout) -> Chromium render from HTML.
        # - Slow path: Chromium render from URL (may wait for networkidle).
        if lang_code == "en" and _prefer_http_fetch_for_en_enabled() and _en_hedged_render_enabled():
            async def _http_html_render() -> Optional[bytes]:
                nonlocal html
                try:
                    async with atimed("report.fetch_html", vin=vin, lang=language, mode="http"):
                        if validated.get("html"):
                            html = str(validated.get("html"))
                        else:
                            html = await _fetch_page_html_http_only(final_url)
                except Exception:
                    html = None
                if html and _html_looks_renderable(html):
                    return await html_to_pdf_bytes_chromium(
                        html_str=html,
                        base_url=final_url,
                        acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                        wait_until="domcontentloaded" if fast_mode else None,
                        timeout_ms=_deadline_remaining_ms(deadline, floor_ms=700, cap_ms=120_000) if fast_mode else None,
                        fast_first_timeout_ms=700 if fast_mode else None,
                        fast_first_wait_until="domcontentloaded" if fast_mode else None,
                        block_resource_types={"image", "media"} if fast_mode else None,
                    )
                return None

            async def _url_render() -> Optional[bytes]:
                delay_ms = _en_hedge_delay_ms()
                if delay_ms:
                    try:
                        await asyncio.sleep(delay_ms / 1000.0)
                    except Exception:
                        pass
                return await html_to_pdf_bytes_chromium(
                    url=final_url,
                    timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    wait_until="domcontentloaded" if fast_mode else None,
                    fast_first_timeout_ms=700 if fast_mode else None,
                    fast_first_wait_until="domcontentloaded" if fast_mode else None,
                    block_resource_types={"image", "media"} if fast_mode else None,
                )

            http_task = asyncio.create_task(_http_html_render())
            url_task = asyncio.create_task(_url_render())
            try:
                pending = {http_task, url_task}
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            candidate = task.result()
                        except Exception:
                            candidate = None
                        if _pdf_bytes_looks_ok(candidate):
                            pdf_bytes = candidate
                            for p in pending:
                                p.cancel()
                            pending = set()
                            break
                if not pdf_bytes:
                    # If neither produced a valid PDF, await results for fallbacks below.
                    try:
                        pdf_bytes = http_task.result() if http_task.done() else await http_task
                    except Exception:
                        pdf_bytes = None
                    if not _pdf_bytes_looks_ok(pdf_bytes):
                        try:
                            pdf_bytes = url_task.result() if url_task.done() else await url_task
                        except Exception:
                            pdf_bytes = None
            finally:
                for t in (http_task, url_task):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(http_task, url_task, return_exceptions=True)
        else:
            # Non-English or hedging disabled: keep existing behavior.
            if _prefer_http_fetch_for_en_enabled() and lang_code == "en":
                try:
                    async with atimed("report.fetch_html", vin=vin, lang=language, mode="http"):
                        html = await _fetch_page_html_http_only(url)
                except Exception:
                    html = None
                if html and _html_looks_renderable(html):
                    pdf_bytes = await html_to_pdf_bytes_chromium(
                        html_str=html,
                        base_url=url,
                        timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                        acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    )
                    if not pdf_bytes:
                        pdf_bytes = await _render_pdf_from_html(html, needs_translation=False, language=language, base_url=url, deadline=deadline)
                else:
                    pdf_bytes = await html_to_pdf_bytes_chromium(
                        url=url,
                        timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                        acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                    )
            else:
                pdf_bytes = await html_to_pdf_bytes_chromium(
                    url=url,
                    timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
                    acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
                )

    if not pdf_bytes and html:
        pdf_bytes = await html_to_pdf_bytes_chromium(
            html_str=html,
            base_url=url,
            timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
            acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
        )
    return pdf_bytes, bool(translation_skipped)


async def _render_pdf_from_html(
    html: Optional[str],
    needs_translation: bool,
    language: str,
    base_url: Optional[str] = None,
    *,
    deadline: Optional[float] = None,
    fast_mode: bool = False,
) -> tuple[Optional[bytes], bool]:
    if not html:
        return None, False
    translation_skipped = False
    if needs_translation:
        translated, translated_ok = await _maybe_translate_html(html, language, deadline=deadline, budget_s=_TRANSLATE_BUDGET_SEC if fast_mode else None)
        if not translated_ok:
            translation_skipped = True
    else:
        translated = html
    # Only RTL-wrap if we actually translated (Fast Mode) or if this is full mode.
    if (language or "en").lower() in {"ar", "ku", "ckb"}:
        if not fast_mode or not translation_skipped:
            translated = inject_rtl(translated, lang=language)
    # Chromium-only renderer.
    if fast_mode:
        pdf_bytes = await html_to_pdf_bytes_chromium(
            html_str=translated,
            base_url=base_url,
            timeout_ms=_deadline_remaining_ms(deadline, floor_ms=700, cap_ms=120_000),
            acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
            wait_until="domcontentloaded",
            fast_first_timeout_ms=700,
            fast_first_wait_until="domcontentloaded",
            block_resource_types={"image", "media"},
        )
    else:
        pdf_bytes = await html_to_pdf_bytes_chromium(
            html_str=translated,
            base_url=base_url,
            timeout_ms=_deadline_remaining_ms(deadline, floor_ms=2_000, cap_ms=120_000),
            acquire_timeout_ms=min(_PDF_QUEUE_TIMEOUT_MS, _deadline_remaining_ms(deadline, floor_ms=100, cap_ms=30_000)),
        )
    return pdf_bytes, bool(translation_skipped)


async def _maybe_translate_html(
    html: Optional[str],
    lang: str,
    *,
    deadline: Optional[float] = None,
    budget_s: Optional[float] = None,
) -> tuple[str, bool]:
    if not html:
        return "", False
    lang_code = (lang or "en").lower()
    if lang_code == "en":
        return html, False

    def _translation_looks_ok(src: str, out: Optional[str], target: str) -> bool:
        if not out:
            return False
        low = out.lower()
        if "<html" not in low:
            return False
        # If output is identical, it's almost certainly an RTL-only fallback.
        if out == src:
            return False
        # For RTL languages we must see Arabic-script characters, otherwise it's not a real translation.
        if (target or "en").lower() in {"ar", "ku", "ckb"}:
            if not re.search(r"[\u0600-\u06FF]", out):
                return False
        return True

    # Run two translation strategies in parallel:
    # - `translate_html`: provider-first + internal Google-free fallback + RTL injection.
    # - `translate_html_google_free`: optimized Google-free-only HTML translator.
    # We then take the first result that *actually* contains Arabic-script output.
    timeout_s = float(os.getenv("TRANSLATE_TIMEOUT_SEC", "6.0") or 6.0)
    if deadline is not None:
        # Keep translation within remaining report budget.
        timeout_s = min(timeout_s, max(2.0, (deadline - time.perf_counter()) - 1.0))
    # Give a tiny bit of headroom so we don't cancel the translator right before success.
    hard_timeout = max(2.0, min(timeout_s + 2.0, 15.0))
    if budget_s is not None:
        hard_timeout = max(0.25, min(float(budget_s), hard_timeout))

    async def _primary() -> Optional[str]:
        try:
            return await asyncio.wait_for(translate_html(html, lang_code), timeout=hard_timeout)
        except Exception:
            return None

    async def _google_free() -> Optional[str]:
        try:
            return await asyncio.wait_for(translate_html_google_free(html, lang_code), timeout=max(0.25, min(hard_timeout, 10.0)))
        except Exception:
            return None

    t1 = asyncio.create_task(_primary())
    t2 = asyncio.create_task(_google_free())
    try:
        pending = {t1, t2}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    candidate = task.result()
                except Exception:
                    candidate = None
                if _translation_looks_ok(html, candidate, lang_code):
                    for p in pending:
                        p.cancel()
                    return candidate, True

                # If neither produced a real translation, fall back to original (English).
                # Delivery layers can then send English FAST PDF and optionally follow up with the full localized version.
                return html, False
    finally:
        for t in (t1, t2):
            if not t.done():
                t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


async def _fetch_page_html(url: str) -> Optional[str]:
    # Try lightweight HTTP fetch first to avoid launching Playwright.
    try:
        html = await _fetch_page_html_http_only(url)
        if html:
            return html
    except Exception:
        pass

    # Fallback to Chromium (shared) only if HTTP fetch failed or returned non-HTML.
    return await fetch_page_html_chromium(url)


async def _fetch_page_html_http_only(url: str) -> Optional[str]:
    """Fetch HTML via aiohttp only (never falls back to Playwright).

    Used for the English fast path where the whole point is to avoid Playwright URL waits.
    """

    if not url:
        return None
    session = await _get_http_session()
    # Keep this relatively small so it can be a fast path.
    timeout_s = float(os.getenv("CARFAX_HTML_HTTP_TIMEOUT", "6") or 6)
    timeout_s = max(2.0, min(timeout_s, float(_CARFAX_TIMEOUT)))
    async with session.get(url, timeout=timeout_s) as resp:
        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        if resp.status in (200, 201) and "text/html" in ctype:
            text = await resp.text()
            if "<html" in text.lower():
                return text
    return None


def _needs_translation(language: str) -> bool:
    return (language or "en").lower() in {"ar", "ku", "ckb"}


def _translated_fetch_wait_until() -> str:
    return (os.getenv("TRANSLATED_FETCH_WAIT_UNTIL", "load") or "load").strip().lower()


def _translated_fetch_timeout_ms() -> int:
    raw = (os.getenv("TRANSLATED_FETCH_TIMEOUT_MS", "8000") or "").strip()
    try:
        val = int(raw)
    except Exception:
        val = 8000
    return max(2_000, min(val, 30_000))


async def _quick_translate_html_google(html: str, lang: str, *, timeout: float = 5.0) -> str:
    """Best-effort fast translation using Google free endpoint; returns original on failure."""

    target = (lang or "en").lower()
    if target == "en":
        return html
    if BeautifulSoup is None:
        return html

    soup = BeautifulSoup(html, "html.parser")
    text_nodes = []
    originals = []
    for element in soup.find_all(text=True):
        if element.parent and element.parent.name in ("script", "style", "noscript"):
            continue
        raw = str(element)
        if raw and len(raw.strip()) >= 2:
            text_nodes.append(element)
            originals.append(raw)

    if not text_nodes:
        return html

    url = "https://translate.googleapis.com/translate_a/single"
    sem = asyncio.Semaphore(10)
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)

    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async def _one(text: str) -> str:
            params = {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
            async with sem:
                try:
                    async with session.get(url, params=params) as resp:
                        data = await resp.json(content_type=None)
                        if isinstance(data, list) and data and isinstance(data[0], list):
                            parts = [seg[0] for seg in data[0] if isinstance(seg, list) and seg and isinstance(seg[0], str)]
                            return "".join(parts) if parts else text
                except Exception:
                    return text
                return text

        translated = await asyncio.gather(*[_one(t) for t in originals])
    if translated and len(translated) == len(text_nodes):
        for node, tr in zip(text_nodes, translated):
            node.replace_with(tr)

    if target == "ku":
        try:
            for element in soup.find_all(text=True):
                if element.parent and element.parent.name in ("script", "style", "noscript"):
                    continue
                element.replace_with(_latin_ku_to_arabic(str(element)))
        except Exception:
            pass

    return str(soup)
