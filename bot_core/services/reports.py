"""Carfax/VIN report generation.

Authoritative upstream API:
- Endpoint: GET https://api.dejavuplus.com/api/carfax/:vin
- Auth header: Authorization: Bearer <JWT>

Production success rules (per upstream contract):
- HTTP 201 + application/json + json.data.htmlContent => SUCCESS (render HTML -> PDF and deliver)
- HTTP 200 + application/pdf => SUCCESS (deliver bytes as-is)

Failure rules:
- HTTP >= 400
- Missing htmlContent when JSON success is returned
- Playwright render error / empty PDF

Operational constraints:
- Hard cap: 10s end-to-end in reports layer.
- No cached PDFs.
- Inflight de-dupe per (user_id, vin).
"""

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
from html import escape
from typing import Any, Dict, List, Optional, cast

from bot_core.config import get_env
from bot_core.telemetry import atimed, get_rid
from bot_core.utils.vin import normalize_vin

from bot_core.services.pdf import html_to_pdf_bytes_chromium
from bot_core.services.translation import inject_rtl, translate_html
from bot_core.services.pdf import PdfBusyError


LOGGER = logging.getLogger(__name__)


SUPPORTED_REPORT_LANGS = {"ar", "en", "ku", "ckb"}


def _normalize_report_lang(lang: Optional[str]) -> str:
    candidate = (lang or "").strip().lower()
    if not candidate:
        return "en"
    if candidate in SUPPORTED_REPORT_LANGS:
        return candidate
    # Accept common system-style tags like ar-IQ, ckb-IQ, ku-TR.
    primary = re.split(r"[-_]", candidate, maxsplit=1)[0]
    if primary in SUPPORTED_REPORT_LANGS:
        return primary
    return "en"


def _empty_errors() -> List[str]:
    return []


_HTTP_SESSION: Optional[aiohttp.ClientSession] = None
_HTTP_SESSION_LOCK = asyncio.Lock()
_CARFAX_SEM = asyncio.Semaphore(int(os.getenv("CARFAX_MAX_CONCURRENCY", "12") or 12))
_CARFAX_TIMEOUT = float(os.getenv("CARFAX_HTTP_TIMEOUT", "8") or 8)
_CARFAX_TIMEOUT = max(1.0, min(_CARFAX_TIMEOUT, 10.0))

# Total wall-clock budget for generating a report end-to-end.
# 10s is too tight in real-world conditions (translation + Chromium render), and can cause
# user-visible timeouts/refunds even when upstream is healthy.
_REPORT_TOTAL_TIMEOUT_SEC = float(os.getenv("REPORT_TOTAL_TIMEOUT_SEC", "45") or 45)
_REPORT_TOTAL_TIMEOUT_SEC = max(5.0, min(_REPORT_TOTAL_TIMEOUT_SEC, 180.0))

# Backpressure: bound concurrent end-to-end report generation so heavy load doesn't
# push all requests into timeouts after partial progress.
_REPORT_MAX_CONCURRENCY = int(os.getenv("REPORT_MAX_CONCURRENCY", "6") or 6)
_REPORT_MAX_CONCURRENCY = max(1, min(_REPORT_MAX_CONCURRENCY, 50))
_REPORT_GEN_SEM = asyncio.Semaphore(_REPORT_MAX_CONCURRENCY)

_REPORT_QUEUE_TIMEOUT_SEC = float(os.getenv("REPORT_QUEUE_TIMEOUT_SEC", "2.0") or 2.0)
_REPORT_QUEUE_TIMEOUT_SEC = max(0.05, min(_REPORT_QUEUE_TIMEOUT_SEC, 5.0))

_CARFAX_QUEUE_TIMEOUT_SEC = float(os.getenv("CARFAX_QUEUE_TIMEOUT_SEC", "2.0") or 2.0)
_CARFAX_QUEUE_TIMEOUT_SEC = max(0.05, min(_CARFAX_QUEUE_TIMEOUT_SEC, 5.0))

_INFLIGHT_LOCK = asyncio.Lock()
_INFLIGHT: Dict[str, asyncio.Task[ReportResult]] = {}


def token_sanity(raw_token: Optional[str]) -> Dict[str, Any]:
    raw = (raw_token or "").strip()
    # For logs only; never return the full token.
    head5 = raw[:5] if raw else ""
    tail5 = raw[-5:] if len(raw) >= 5 else raw
    dot_parts = raw.count(".") + 1 if raw and "." in raw else (1 if raw else 0)
    return {
        "token_len": len(raw),
        "dot_parts": dot_parts,
        "head5": head5,
        "tail5": tail5,
        "has_space": bool(re.search(r"\s", raw)) if raw else False,
        "has_bearer": raw.lower().startswith("bearer ") if raw else False,
    }


def normalize_token(raw_token: Optional[str]) -> Optional[str]:
    if raw_token is None:
        return None
    token = str(raw_token).strip()
    # Strip surrounding quotes (common in .env on Windows).
    if (token.startswith("\"") and token.endswith("\"")) or (token.startswith("'") and token.endswith("'")):
        token = token[1:-1].strip()
    # Remove an accidental Bearer prefix if provided.
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[-1].strip()
    # Reject tokens containing whitespace or the word 'bearer' (double header bugs).
    if not token:
        return None
    if re.search(r"\s", token):
        return None
    if "bearer" in token.lower():
        return None
    return token


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

    # Observability fields for the strict upstream PDF path.
    upstream_sha256: Optional[str] = None
    upstream_status: Optional[int] = None
    upstream_content_type: Optional[str] = None


ERROR_UPSTREAM_FETCH_FAILED = "UPSTREAM_FETCH_FAILED"
ERROR_PDF_RENDER_FAILED = "PDF_RENDER_FAILED"

VHR_FETCH_FAILED_USER_MESSAGE = "Could not fetch the Vehicle History Report for this VIN. Credit refunded."

PDF_RENDER_FAILED_USER_MESSAGE = "Failed to generate the PDF right now. Credit refunded."


def _sanitize_preview(text: str, *, max_chars: int = 200) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.replace("\x00", "")
    return raw[:max_chars]


def _looks_like_error_or_login_page(html: str) -> bool:
    # Heuristic detection to prevent rendering login/blocked/error pages.
    # Keep intentionally broad; false positives are preferable to delivering garbage.
    raw = (html or "").lower()
    if not raw:
        return True
    # Common blockers / bot protections
    markers = (
        "access denied",
        "request blocked",
        "forbidden",
        "unauthorized",
        "login",
        "sign in",
        "cloudflare",
        "attention required",
        "captcha",
        "not found",
        "404",
        "500",
        "service unavailable",
        "temporarily unavailable",
        "rate limit",
        "too many requests",
    )
    if any(m in raw for m in markers):
        return True
    # If it doesn't even look like HTML, treat as invalid.
    head = raw.lstrip()[:2000]
    if "<html" not in head and "<!doctype html" not in head:
        return True
    return False


async def fetch_report_pdf_bytes(
    vin: str,
    options: Optional[Dict[str, Any]] = None,
    lang: Optional[str] = None,
    *,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> tuple[Optional[bytes], Dict[str, Any]]:
    """Primary path: fetch OFFICIAL upstream PDF bytes.

    Endpoint (authoritative): GET https://api.dejavuplus.com/api/carfax/{vin}
    Auth: Authorization: Bearer <JWT>

    Returns (pdf_bytes, meta). pdf_bytes is non-empty only when upstream responds
    with application/pdf.
    """

    _ = (options, lang)  # reserved for future API variants; kept for stable signature.
    resp = await _call_carfax_api(vin, total_timeout_s=total_timeout_s, deadline=deadline, force_fresh=force_fresh)
    ctype = (resp.get("ctype") or "").lower()
    pdf_bytes = resp.get("pdf_bytes")
    if resp.get("ok") and isinstance(pdf_bytes, (bytes, bytearray)) and bytes(pdf_bytes) and "application/pdf" in ctype:
        return bytes(pdf_bytes), resp
    return None, resp


def _extract_html_from_upstream_json(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    # Most common shape (per production logs): {"message":..., "data": {"_id":..., "vin":..., "htmlContent": "<html..."}}
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("htmlContent", "html", "html_content", "content", "reportHtml"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
    for key in ("htmlContent", "html", "html_content", "content", "reportHtml"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def _canonical_api_base() -> str:
    """Canonical DejaVuPlus base URL per docs.

    We ignore non-canonical API_URL values to avoid hitting undocumented routes.
    """

    # Always use the authoritative upstream base.
    # If API_URL is set, we only use it for diagnostics and to guard against common
    # misconfiguration (/api/carfax causing double carfax).
    canonical = "https://api.dejavuplus.com/api"
    cfg = get_env()
    env_base = (cfg.api_url or "").strip().rstrip("/")
    if env_base:
        low = env_base.lower().rstrip("/")
        if "api.dejavuplus.com" in low and low.endswith("/api/carfax"):
            try:
                LOGGER.error("API_URL misconfigured (ends with /api/carfax). Using canonical base=%s", canonical)
            except Exception:
                pass
        elif env_base != canonical and "api.dejavuplus.com" in low:
            try:
                LOGGER.warning("API_URL overridden but canonical base is enforced. API_URL=%s", env_base)
            except Exception:
                pass
    return canonical


def _carfax_url(vin: str, *, ts_ms: Optional[int] = None) -> str:
    base = _canonical_api_base().rstrip("/")
    url = f"{base}/carfax/{vin}"
    # Assert exactly one '/carfax/' segment.
    if url.count("/carfax/") != 1:
        fixed = url.replace("/carfax/carfax/", "/carfax/")
        try:
            LOGGER.error("carfax_url_invalid url=%s fixed=%s", url, fixed)
        except Exception:
            pass
        url = fixed
    if ts_ms is not None:
        url = f"{url}?ts={int(ts_ms)}"
    return url


def build_carfax_url(vin: str) -> str:
    """Public canonical URL builder used by tools and runtime."""

    return _carfax_url(vin)


async def generate_vin_report(
    vin: str,
    *,
    language: str = "en",
    fast_mode: bool = True,
    user_id: Optional[str] = None,
) -> ReportResult:
    """Generate report with a definitive, production-safe pipeline.

    1) Primary path: fetch OFFICIAL upstream PDF bytes and deliver as-is.
    2) Fallback (HTML-only upstream): validate upstream response + HTML content, then render via Playwright.
    """

    requested_lang = _normalize_report_lang(language)
    normalized_vin = normalize_vin(vin)
    if not normalized_vin:
        return ReportResult(
            success=False,
            user_message=_t("report.invalid_vin", requested_lang, "❌ رقم VIN غير صالح."),
            errors=["invalid_vin"],
        )

    start_t = time.perf_counter()
    deadline = start_t + float(_REPORT_TOTAL_TIMEOUT_SEC)

    def _remaining_s() -> float:
        return max(0.0, deadline - time.perf_counter())

    inflight_key = f"{user_id or '-'}:{normalized_vin}"
    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(inflight_key)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)

        def _is_retryable_failure(rr: Optional[ReportResult]) -> bool:
            if rr is None:
                return True
            if rr.success:
                return False
            # Never retry auth/token failures.
            try:
                errors = [str(e).lower() for e in (rr.errors or [])]
            except Exception:
                errors = []
            if any("invalid_token" in e for e in errors):
                return False
            if any(e.startswith("http_401") or e.startswith("http_403") for e in errors):
                return False
            try:
                raw = rr.raw_response
                if isinstance(raw, dict):
                    status = raw.get("status")
                    if int(status) in (401, 403):
                        return False
            except Exception:
                pass
            # Never retry invalid VIN.
            if any("invalid_vin" in e for e in errors):
                return False
            # Retry upstream and render failures once.
            if rr.error_class in {ERROR_UPSTREAM_FETCH_FAILED, ERROR_PDF_RENDER_FAILED}:
                return True
            transient_markers = ("timeout", "http_500", "http_502", "http_503", "http_504", "exception")
            return any(m in e for e in errors for m in transient_markers) or not errors

        async def _runner() -> ReportResult:
            acquired_report_slot = False
            try:
                # Backpressure: bounded wait; return timeout (not busy) on saturation.
                acquire_s = min(_REPORT_QUEUE_TIMEOUT_SEC, max(0.05, _remaining_s()))
                await asyncio.wait_for(_REPORT_GEN_SEM.acquire(), timeout=acquire_s)
                acquired_report_slot = True
            except Exception:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.timeout", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن الوقت المحدد."),
                    errors=["timeout"],
                    vin=normalized_vin,
                    error_class=ERROR_UPSTREAM_FETCH_FAILED,
                    raw_response={"_dv_path": "timeout_before_upstream", "total_time_sec": round(time.perf_counter() - start_t, 3)},
                )

            try:
                last_failure: Optional[ReportResult] = None
                for upstream_attempt in (1, 2):
                    fetch_budget = max(0.5, min(_remaining_s(), float(_CARFAX_TIMEOUT)))
                    rid = get_rid() or "-"

                    async with atimed(
                        "report.upstream",
                        vin=normalized_vin,
                        lang=requested_lang,
                        fast=bool(fast_mode),
                        budget_s=float(fetch_budget),
                        attempt=upstream_attempt,
                    ):
                        pdf_bytes_direct, upstream = await fetch_report_pdf_bytes(
                            normalized_vin,
                            options=None,
                            lang=requested_lang,
                            total_timeout_s=fetch_budget,
                            deadline=deadline,
                            force_fresh=True,
                        )

                    total_time = round(time.perf_counter() - start_t, 3)
                    status = upstream.get("status")
                    ctype = (upstream.get("ctype") or "").lower()
                    final_url = str(upstream.get("final_url") or "")

                    try:
                        LOGGER.info(
                            "upstream_fetch rid=%s vin=%s attempt=%s fetch_status=%s fetch_final_url=%s ctype=%s total_time_sec=%s",
                            rid,
                            normalized_vin,
                            upstream_attempt,
                            status if status is not None else "na",
                            final_url or "-",
                            ctype or "-",
                            total_time,
                        )
                    except Exception:
                        pass

                    # Path A: OFFICIAL upstream PDF bytes.
                    if isinstance(pdf_bytes_direct, (bytes, bytearray)) and bytes(pdf_bytes_direct):
                        try:
                            LOGGER.info(
                                "report_success rid=%s vin=%s upstream_mode=pdf pdf_bytes_len=%s total_time_sec=%s",
                                rid,
                                normalized_vin,
                                len(bytes(pdf_bytes_direct)),
                                total_time,
                            )
                        except Exception:
                            pass

                        return ReportResult(
                            success=True,
                            user_message=_t("report.success.pdf_direct", requested_lang, "✅ Report ready."),
                            pdf_bytes=bytes(pdf_bytes_direct),
                            pdf_filename=str(upstream.get("filename") or f"{normalized_vin}.pdf"),
                            vin=normalized_vin,
                            raw_response={**(upstream or {}), "total_time_sec": total_time, "upstream_mode": "pdf"},
                            upstream_sha256=str(upstream.get("sha256") or "") or None,
                            upstream_status=int(status) if isinstance(status, int) else None,
                            upstream_content_type=str(upstream.get("ctype") or "") or None,
                        )

                    # Only fail upstream fetch on HTTP >= 400 or on transport/token errors.
                    if (not upstream.get("ok")) or (isinstance(status, int) and status >= 400):
                        err = str(upstream.get("error") or upstream.get("err_text") or f"HTTP_{status if status is not None else 'NA'}")
                        failure = ReportResult(
                            success=False,
                            user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                            errors=[err],
                            vin=normalized_vin,
                            raw_response={**(upstream or {}), "total_time_sec": total_time},
                            error_class=ERROR_UPSTREAM_FETCH_FAILED,
                        )
                        last_failure = failure
                        if upstream_attempt == 1 and _is_retryable_failure(failure):
                            continue
                        return failure

                    # Path B: 201 JSON with htmlContent.
                    html_candidate: Optional[str] = None
                    json_payload = upstream.get("json")
                    if isinstance(json_payload, dict):
                        html_candidate = _extract_html_from_upstream_json(cast(Dict[str, Any], json_payload))

                    # Also accept direct HTML bodies (some upstream variants return text/html).
                    if not html_candidate:
                        body_text = upstream.get("text")
                        if isinstance(body_text, str):
                            low = body_text.lstrip().lower()
                            if low.startswith("<html") or "<!doctype html" in low:
                                html_candidate = body_text

                    if not html_candidate:
                        preview = ""
                        try:
                            if isinstance(upstream.get("text"), str):
                                preview = _sanitize_preview(str(upstream.get("text") or ""))
                        except Exception:
                            preview = ""
                        try:
                            LOGGER.warning(
                                "upstream_missing_htmlContent rid=%s vin=%s attempt=%s fetch_status=%s fetch_final_url=%s ctype=%s preview=%s error_class=%s",
                                rid,
                                normalized_vin,
                                upstream_attempt,
                                status if status is not None else "na",
                                final_url or "-",
                                ctype or "-",
                                preview,
                                ERROR_UPSTREAM_FETCH_FAILED,
                            )
                        except Exception:
                            pass
                        failure = ReportResult(
                            success=False,
                            user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                            errors=["missing_htmlContent"],
                            vin=normalized_vin,
                            raw_response={**(upstream or {}), "total_time_sec": total_time},
                            error_class=ERROR_UPSTREAM_FETCH_FAILED,
                        )
                        last_failure = failure
                        if upstream_attempt == 1 and _is_retryable_failure(failure):
                            continue
                        return failure

                    # Strict HTML fetch validation BEFORE rendering.
                    html_len0 = len(html_candidate.encode("utf-8", errors="ignore"))
                    preview0 = _sanitize_preview(html_candidate)
                    try:
                        LOGGER.info(
                            "upstream_html_candidate rid=%s vin=%s attempt=%s fetch_status=%s fetch_final_url=%s html_bytes_len=%s preview=%s",
                            rid,
                            normalized_vin,
                            upstream_attempt,
                            status if status is not None else "na",
                            final_url or "-",
                            html_len0,
                            preview0,
                        )
                    except Exception:
                        pass
                    if _looks_like_error_or_login_page(html_candidate):
                        failure = ReportResult(
                            success=False,
                            user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                            errors=["upstream_html_error_page"],
                            vin=normalized_vin,
                            raw_response={**(upstream or {}), "total_time_sec": total_time, "html_bytes_len": html_len0, "html_preview": preview0},
                            error_class=ERROR_UPSTREAM_FETCH_FAILED,
                        )
                        last_failure = failure
                        if upstream_attempt == 1 and _is_retryable_failure(failure):
                            continue
                        return failure

                    # Translate report HTML when requested (kept bounded by the overall reports deadline).
                    delivered_lang = requested_lang
                    translate_ms = None
                    translated = False
                    if requested_lang != "en":
                        t_tr0 = time.perf_counter()
                        try:
                            html_out = await translate_html(html_candidate, requested_lang)
                            if isinstance(html_out, str) and html_out.strip():
                                translated = (html_out != html_candidate)
                                html_candidate = html_out
                        except Exception:
                            # Hard fallback: preserve original content but ensure correct RTL styling.
                            try:
                                html_candidate = inject_rtl(html_candidate, lang=requested_lang)
                            except Exception:
                                pass
                        translate_ms = round((time.perf_counter() - t_tr0) * 1000.0, 2)

                    html_len = len(html_candidate.encode("utf-8", errors="ignore"))
                    try:
                        LOGGER.info(
                            "upstream_ok rid=%s vin=%s upstream_mode=html status=%s ctype=%s final_url=%s html_bytes_len=%s lang=%s translated=%s translate_ms=%s",
                            rid,
                            normalized_vin,
                            status if status is not None else "na",
                            ctype or "-",
                            final_url or "-",
                            html_len,
                            delivered_lang,
                            translated,
                            translate_ms if translate_ms is not None else "-",
                        )
                    except Exception:
                        pass

                    # Render htmlContent to PDF (official delivered report).
                    t_render0 = time.perf_counter()
                    render_budget_ms = _deadline_remaining_ms(deadline, floor_ms=1500, cap_ms=120_000)
                    pdf_rendered: Optional[bytes] = None
                    try:
                        acquire_ms = min(20_000, max(2_000, int(render_budget_ms * 0.5)))
                        for render_attempt in (1, 2):
                            try:
                                pdf_rendered = await html_to_pdf_bytes_chromium(
                                    html_str=html_candidate,
                                    base_url="https://www.carfax.com/",
                                    timeout_ms=render_budget_ms,
                                    acquire_timeout_ms=acquire_ms,
                                    wait_until="domcontentloaded",
                                )
                                if pdf_rendered:
                                    break
                            except PdfBusyError:
                                if render_attempt == 1:
                                    try:
                                        from bot_core.services.pdf import close_pdf_engine

                                        await close_pdf_engine()
                                    except Exception:
                                        pass
                                    continue
                                pdf_rendered = None
                                break
                            except Exception:
                                pdf_rendered = None
                                break
                    except Exception:
                        pdf_rendered = None

                    render_ms = (time.perf_counter() - t_render0) * 1000.0
                    pdf_len = len(pdf_rendered) if isinstance(pdf_rendered, (bytes, bytearray)) else 0
                    try:
                        LOGGER.info(
                            "render_result rid=%s vin=%s upstream_mode=html render_ms=%s pdf_bytes_len=%s",
                            rid,
                            normalized_vin,
                            round(render_ms, 2),
                            pdf_len,
                        )
                    except Exception:
                        pass

                    if not isinstance(pdf_rendered, (bytes, bytearray)) or not bytes(pdf_rendered):
                        failure = ReportResult(
                            success=False,
                            user_message=PDF_RENDER_FAILED_USER_MESSAGE,
                            errors=["pdf_render_failed"],
                            vin=normalized_vin,
                            raw_response={**(upstream or {}), "total_time_sec": total_time, "upstream_mode": "html"},
                            error_class=ERROR_PDF_RENDER_FAILED,
                        )
                        last_failure = failure
                        if upstream_attempt == 1 and _is_retryable_failure(failure):
                            continue
                        return failure

                    return ReportResult(
                        success=True,
                        user_message=_t("report.success.pdf_direct", requested_lang, "✅ Report ready."),
                        pdf_bytes=bytes(pdf_rendered),
                        pdf_filename=f"{normalized_vin}.pdf",
                        vin=normalized_vin,
                        raw_response={
                            **(upstream or {}),
                            "total_time_sec": total_time,
                            "upstream_mode": "html",
                            "_dv_fast": {
                                "fast_mode": bool(fast_mode),
                                "requested_lang": requested_lang,
                                "delivered_lang": delivered_lang,
                                "translated": bool(translated),
                                "translate_ms": translate_ms,
                                "total_sec": total_time,
                            },
                        },
                    )

                return last_failure or ReportResult(
                    success=False,
                    user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                    errors=["unknown_failure"],
                    vin=normalized_vin,
                    error_class=ERROR_UPSTREAM_FETCH_FAILED,
                )
            finally:
                if acquired_report_slot:
                    try:
                        _REPORT_GEN_SEM.release()
                    except Exception:
                        pass

        task = asyncio.create_task(_runner())
        _INFLIGHT[inflight_key] = task

    try:
        return await asyncio.shield(task)
    finally:
        async with _INFLIGHT_LOCK:
            cur = _INFLIGHT.get(inflight_key)
            if cur is task:
                _INFLIGHT.pop(inflight_key, None)
async def _call_carfax_api(
    vin: str,
    *,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> Dict[str, Any]:
    cfg = get_env()
    headers: Dict[str, str] = {}
    raw_token = (cfg.api_token or "")
    clean_token = normalize_token(raw_token)
    sanity = token_sanity(raw_token)
    rid = get_rid() or "-"
    try:
        LOGGER.info(
            "token_sanity rid=%s token_len=%s dot_parts=%s head5=%s tail5=%s has_space=%s has_bearer=%s",
            rid,
            sanity.get("token_len"),
            sanity.get("dot_parts"),
            sanity.get("head5"),
            sanity.get("tail5"),
            sanity.get("has_space"),
            sanity.get("has_bearer"),
        )
    except Exception:
        pass
    if not clean_token:
        return {"ok": False, "error": "invalid_token", "status": 0, "ctype": "", "final_url": "", "token_sanity": sanity, "_dv_path": "invalid_token"}
    headers["Authorization"] = f"Bearer {clean_token}"
    # Always no-cache to avoid stale variants; retries add a cache-buster too.
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    # No language-driven format switching. We always prefer upstream PDF.
    headers["Accept"] = "application/pdf, application/json;q=0.9, text/html;q=0.8, */*;q=0.5"

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

    url = _carfax_url(vin, ts_ms=(int(time.time() * 1000) if force_fresh else None))

    # Acquire Carfax slot with a bounded wait; return timeout (not busy) on saturation.
    acquired = False
    try:
        queue_budget = _CARFAX_QUEUE_TIMEOUT_SEC
        if deadline is not None:
            queue_budget = min(queue_budget, max(0.05, float(deadline) - time.perf_counter()))
        await asyncio.wait_for(_CARFAX_SEM.acquire(), timeout=max(0.05, queue_budget))
        acquired = True

        async with atimed("carfax.http", method="GET", route="/carfax/{vin}"):
            async with session.get(url, headers=headers, timeout=request_timeout, allow_redirects=True) as resp:
                status = int(resp.status)
                ctype = (resp.headers.get("Content-Type", "") or "").lower()
                final_url = str(getattr(resp, "url", "") or "")
                body = await resp.read()
                rid = get_rid() or "-"
                sha256 = None
                try:
                    if body:
                        sha256 = hashlib.sha256(body).hexdigest()
                except Exception:
                    sha256 = None
                try:
                    LOGGER.info(
                        "upstream_call rid=%s url=%s status=%s content_type=%s bytes_len=%s sha256=%s",
                        rid,
                        final_url or url,
                        status,
                        ctype or "-",
                        len(body) if body is not None else 0,
                        sha256 or "-",
                    )
                except Exception:
                    pass

                if status not in (200, 201):
                    try:
                        txt = body.decode("utf-8", errors="ignore")
                    except Exception:
                        txt = ""
                    return {"ok": False, "status": status, "ctype": ctype, "err_text": txt, "final_url": final_url, "sha256": sha256, "_dv_path": "non_200"}

                # NOTE: We do not validate PDF headers or content.
                if "application/pdf" in ctype and body:
                    return {
                        "ok": True,
                        "pdf_bytes": body,
                        "filename": f"{vin}.pdf",
                        "status": status,
                        "final_url": final_url,
                        "ctype": ctype,
                        "sha256": sha256,
                        "_dv_path": "upstream_pdf",
                    }

                # Non-PDF: preserve body for debugging but do not attempt conversion.
                if "application/json" in ctype or (body[:1] == b"{"):
                    try:
                        data = json.loads(body.decode("utf-8", errors="ignore") or "{}")
                        return {"ok": True, "json": data, "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256, "_dv_path": f"json_{status}"}
                    except Exception:
                        return {"ok": True, "text": body.decode("utf-8", errors="ignore"), "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256, "_dv_path": f"json_text_{status}"}

                return {"ok": True, "text": body.decode("utf-8", errors="ignore"), "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256, "_dv_path": f"html_or_text_{status}"}

    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "_dv_path": "timeout"}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "_dv_path": "exception"}
    finally:
        if acquired:
            try:
                _CARFAX_SEM.release()
            except Exception:
                pass


async def fetch_upstream_pdf(
    vin: str,
    *,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> Dict[str, Any]:
    """Fetch upstream response and return a dict containing PDF bytes if present.

    STRICT RULE: if upstream returns 200 + application/pdf + non-empty body, we accept it.
    No PDF header checks, no classification.
    """

    rid = get_rid() or "-"
    api_response = await _call_carfax_api(
        vin,
        total_timeout_s=total_timeout_s,
        deadline=deadline,
        force_fresh=force_fresh,
    )

    status = api_response.get("status")
    ctype = (api_response.get("ctype") or "").lower()
    pdf_bytes = api_response.get("pdf_bytes")
    sha = api_response.get("sha256")

    try:
        LOGGER.info(
            "upstream_pdf_candidate rid=%s vin=%s status=%s ctype=%s bytes_len=%s sha256=%s",
            rid,
            vin,
            status if status is not None else "na",
            ctype or "-",
            len(pdf_bytes) if isinstance(pdf_bytes, (bytes, bytearray)) else 0,
            sha or "-",
        )
    except Exception:
        pass

    if not api_response.get("ok"):
        return api_response

    # Success only if PDF by Content-Type + non-empty bytes.
    if isinstance(pdf_bytes, (bytes, bytearray)) and bytes(pdf_bytes) and ("application/pdf" in ctype):
        return api_response

    # Non-PDF upstream response.
    return {
        "ok": False,
        "status": api_response.get("status"),
        "ctype": api_response.get("ctype"),
        "final_url": api_response.get("final_url"),
        "sha256": api_response.get("sha256"),
        "error": "non_pdf_upstream",
        "json": api_response.get("json"),
        "text": api_response.get("text"),
        "_dv_path": str(api_response.get("_dv_path") or "non_pdf"),
    }



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
