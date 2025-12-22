# -*- coding: utf-8 -*-
"""Carfax/VIN report generation helpers extracted from the Telegram monolith."""
from __future__ import annotations

import asyncio
import aiohttp
import json
import os
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, List, Optional, cast

import aiohttp

from bot_core.config import get_env
from bot_core.services.pdf import html_to_pdf_bytes_chromium, html_to_pdf_weasyprint_async, fetch_page_html_chromium
from bot_core.services.translation import inject_rtl, translate_html, _latin_ku_to_arabic  # type: ignore
from bot_core.telemetry import atimed

try:  # optional dependency (used for quick HTML translation fallback)
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional
    BeautifulSoup = None
from bot_core.utils.vin import normalize_vin


def _empty_errors() -> List[str]:
    return []


_VIN_CACHE_TTL = 10 * 60  # seconds
_VIN_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_PDF_CACHE_TTL = 30 * 60  # seconds
_PDF_CACHE: Dict[tuple[str, str], tuple[float, "ReportResult"]] = {}
_HTTP_SESSION: Optional[aiohttp.ClientSession] = None
_HTTP_SESSION_LOCK = asyncio.Lock()
_CARFAX_SEM = asyncio.Semaphore(15)
_CARFAX_TIMEOUT = float(os.getenv("CARFAX_HTTP_TIMEOUT", "20") or 20)

_INFLIGHT_REPORTS: Dict[tuple[str, str], asyncio.Task["ReportResult"]] = {}
_INFLIGHT_LOCK = asyncio.Lock()


def _carfax_parallel_primary_enabled() -> bool:
    return (os.getenv("CARFAX_PARALLEL_PRIMARY", "0") or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _cache_get(vin: str) -> Optional[Dict[str, Any]]:
    exp_payload = _VIN_CACHE.get(vin)
    if not exp_payload:
        return None
    expires_at, payload = exp_payload
    if expires_at > time.time():
        return payload
    _VIN_CACHE.pop(vin, None)
    return None


def _cache_set(vin: str, payload: Dict[str, Any]) -> None:
    _VIN_CACHE[vin] = (time.time() + _VIN_CACHE_TTL, payload)


def _pdf_cache_get(vin: str, language: str) -> Optional["ReportResult"]:
    key = (vin, language)
    payload = _PDF_CACHE.get(key)
    if not payload:
        return None
    expires_at, result = payload
    if expires_at > time.time():
        return result
    _PDF_CACHE.pop(key, None)
    return None


def _pdf_cache_set(vin: str, language: str, result: "ReportResult") -> None:
    key = (vin, language)
    _PDF_CACHE[key] = (time.time() + _PDF_CACHE_TTL, result)


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


async def generate_vin_report(vin: str, *, language: str = "en") -> ReportResult:
    """Fetch a VIN report from the upstream API and return a PDF (if possible)."""

    lang_code = (language or "en").strip().lower()
    normalized_vin = normalize_vin(vin)
    if not normalized_vin:
        return ReportResult(success=False, user_message=_t("report.invalid_vin", lang_code, "❌ رقم VIN غير صالح."), errors=["invalid_vin"])

    cached_pdf = _pdf_cache_get(normalized_vin, lang_code)
    if cached_pdf:
        return cached_pdf

    # Prevent duplicate work for the same VIN+language when multiple requests arrive concurrently.
    inflight_key = (normalized_vin, lang_code)
    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT_REPORTS.get(inflight_key)
        if existing and not existing.done():
            return await asyncio.shield(existing)

        task = asyncio.create_task(_generate_vin_report_inner(normalized_vin, lang_code))
        _INFLIGHT_REPORTS[inflight_key] = task

    try:
        return await asyncio.shield(task)
    finally:
        async with _INFLIGHT_LOCK:
            cur = _INFLIGHT_REPORTS.get(inflight_key)
            if cur is task:
                _INFLIGHT_REPORTS.pop(inflight_key, None)


async def _generate_vin_report_inner(normalized_vin: str, lang_code: str) -> ReportResult:
    """Inner implementation for generate_vin_report (supports in-flight de-dupe)."""

    cached_pdf = _pdf_cache_get(normalized_vin, lang_code)
    if cached_pdf:
        return cached_pdf

    api_response = _cache_get(normalized_vin)
    if not api_response:
        prefer_non_pdf = lang_code != "en"
        async with atimed("report.fetch", vin=normalized_vin, lang=lang_code, prefer_non_pdf=prefer_non_pdf):
            api_response = await _call_carfax_api(normalized_vin, prefer_non_pdf=prefer_non_pdf)
        if api_response.get("ok"):
            _cache_set(normalized_vin, api_response)
    if not api_response.get("ok"):
        err = api_response.get("error") or f"HTTP_{api_response.get('status','NA')}"
        return ReportResult(
            success=False,
            user_message=_t("report.error.fetch_detailed", lang_code, "⚠️ فشل جلب تقرير VIN: {error}", error=err),
            errors=[str(err)],
            vin=normalized_vin,
            raw_response=api_response,
        )

    # Shortcut: upstream returned a PDF already.
    if api_response.get("pdf_bytes"):
        filename = api_response.get("filename", f"{normalized_vin}.pdf")
        # For non-English, avoid returning raw PDF (would remain English). Force translation flow.
        if lang_code == "en":
            return ReportResult(
                success=True,
                user_message=_t("report.success.pdf_direct", lang_code, "✅ تم استلام ملف PDF مباشر."),
                pdf_bytes=api_response["pdf_bytes"],
                pdf_filename=filename,
                vin=normalized_vin,
                raw_response=api_response,
            )
        # If non-English but only PDF is available, treat as not ok to push fallback rendering.
        api_response = {"ok": False, "error": "pdf_only_non_translatable"}

    async with atimed("report.render_pdf", vin=normalized_vin, lang=lang_code):
        pdf_bytes = await _render_pdf_from_response(api_response, normalized_vin, lang_code)
    if not pdf_bytes:
        return ReportResult(
            success=False,
            user_message=_t("report.error.pdf_render", lang_code, "⚠️ تعذّر تحويل التقرير إلى PDF."),
            errors=["pdf_generation_failed"],
            vin=normalized_vin,
            raw_response=api_response,
        )

    result = ReportResult(
        success=True,
        user_message=_t("report.success.pdf_created", lang_code, "✅ تم إنشاء ملف PDF للتقرير."),
        pdf_bytes=pdf_bytes,
        pdf_filename=f"{normalized_vin}.pdf",
        vin=normalized_vin,
        raw_response=api_response,
    )
    _pdf_cache_set(normalized_vin, lang_code, result)
    return result


# ---------------------------------------------------------------------------
# Internal helpers copied from the legacy flow (refactored for reuse).
# ---------------------------------------------------------------------------

async def _call_carfax_api(vin: str, *, prefer_non_pdf: bool = False) -> Dict[str, Any]:
    cfg = get_env()
    api_url = cfg.api_url.strip()
    headers: Dict[str, str] = {}
    token = cfg.api_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if prefer_non_pdf:
        headers["Accept"] = "application/json, text/html;q=0.9, */*;q=0.5"
    else:
        headers["Accept"] = "application/pdf, application/json;q=0.9, text/html;q=0.8, */*;q=0.5"
    if not api_url:
        return {"ok": False, "error": "API_URL غير مضبوط في .env"}

    base = api_url.rstrip("/")
    payload = {"vin": vin}
    session = await _get_http_session()

    async def _handle(resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        status = resp.status
        ctype = (resp.headers.get("Content-Type", "") or "").lower()
        if status not in (200, 201):
            try:
                txt = await resp.text()
            except Exception:
                txt = ""
            return {"ok": False, "status": status, "ctype": ctype, "err_text": txt}
        if "application/pdf" in ctype:
            if prefer_non_pdf:
                return {"ok": False, "status": status, "ctype": ctype, "error": "pdf_returned"}
            data = await resp.read()
            return {"ok": True, "pdf_bytes": data, "filename": f"{vin}.pdf"}
        if "application/json" in ctype:
            try:
                data = await resp.json()
                return {"ok": True, "json": data}
            except Exception:
                txt = await resp.text()
                return {"ok": True, "text": txt}
        txt = await resp.text()
        return {"ok": True, "text": txt}

    async def _try_get() -> Optional[Dict[str, Any]]:
        try:
            async with session.get(f"{base}/{vin}", headers=headers) as resp:
                parsed = await _handle(resp)
                return parsed if parsed.get("ok") else None
        except Exception:
            return None

    async def _try_post_vin() -> Optional[Dict[str, Any]]:
        try:
            async with session.post(f"{base}/vin", json=payload, headers=headers) as resp:
                parsed = await _handle(resp)
                return parsed if parsed.get("ok") else None
        except Exception:
            return None

    async def _try_post_base() -> Dict[str, Any]:
        try:
            async with session.post(base, json=payload, headers=headers) as resp:
                parsed = await _handle(resp)
                if parsed.get("ok"):
                    return parsed
                return {"ok": False, "error": f"HTTP {parsed.get('status')}", "detail": parsed.get("err_text", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async with _CARFAX_SEM:
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
            return await _try_post_base()

        parsed = await _try_get()
        if parsed:
            return parsed
        parsed = await _try_post_vin()
        if parsed:
            return parsed
        return await _try_post_base()


def _extract_html_or_url_from_json(data: Any) -> Dict[str, Optional[str]]:
    url_keys = ("url", "html_url", "report_url", "viewerUrl", "reportLink")
    html_keys = ("html", "htmlContent", "report", "content", "body", "data")
    url = None
    html = None
    if isinstance(data, dict):
        mapping: Dict[str, Any] = cast(Dict[str, Any], data)
        for key in url_keys:
            value = mapping.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                url = value
                break
        if not url:
            for key in html_keys:
                value = mapping.get(key)
                if isinstance(value, str) and ("<html" in value.lower() or "<!doctype" in value.lower()):
                    html = value
                    break
        if not (url or html):
            for value in mapping.values():
                if isinstance(value, (dict, list)):
                    nested = _extract_html_or_url_from_json(value)
                    if nested.get("url") or nested.get("html"):
                        return nested
    elif isinstance(data, list):
        for item in cast(List[Any], data):
            nested = _extract_html_or_url_from_json(item)
            if nested.get("url") or nested.get("html"):
                return nested
    return {"url": url, "html": html}


async def _render_pdf_from_response(response: Dict[str, Any], vin: str, language: str) -> Optional[bytes]:
    pdf_bytes: Optional[bytes] = None
    needs_translation = _needs_translation(language)
    json_payload = response.get("json")

    if json_payload is not None:
        extracted = _extract_html_or_url_from_json(json_payload)
        extracted_url = extracted.get("url")
        if extracted_url:
            pdf_bytes = await _render_pdf_from_url(extracted_url, needs_translation, language, vin)
        else:
            html = extracted.get("html")
            if not html:
                formatted = json.dumps(json_payload, ensure_ascii=False, indent=2)
                html = f"<html><meta charset='utf-8'><body><h3>CarFax – {vin}</h3><pre style='white-space:pre-wrap'>{escape(formatted)}</pre></body></html>"
            pdf_bytes = await _render_pdf_from_html(html, needs_translation, language)
    elif response.get("text"):
        text_payload = str(response["text"]).strip()
        if text_payload.startswith(("http://", "https://")):
            pdf_bytes = await _render_pdf_from_url(text_payload, needs_translation, language, vin)
        else:
            html = text_payload if text_payload.lower().startswith("<html") else (
                f"<html><meta charset='utf-8'><body><h3>CarFax – {vin}</h3><pre style='white-space:pre-wrap'>{escape(text_payload)}</pre></body></html>"
            )
            pdf_bytes = await _render_pdf_from_html(html, needs_translation, language)

    return pdf_bytes


async def _render_pdf_from_url(url: str, needs_translation: bool, language: str, vin: str) -> Optional[bytes]:
    html = None
    pdf_bytes = None

    if needs_translation:
        html = await _fetch_page_html(url)
        if html:
            html = await _maybe_translate_html(html, language)
            pdf_bytes = await html_to_pdf_weasyprint_async(html)
            if not pdf_bytes:
                pdf_bytes = await html_to_pdf_bytes_chromium(html_str=html)
        else:
            pdf_bytes = await html_to_pdf_bytes_chromium(url=url)
    else:
        pdf_bytes = await html_to_pdf_bytes_chromium(url=url)

    if not pdf_bytes and html:
        pdf_bytes = await html_to_pdf_bytes_chromium(html_str=html)
    if not pdf_bytes:
        fallback = (
            f"<html><meta charset='utf-8'><body><h3>CarFax – {vin}</h3>"
            f"<a href='{escape(url)}'>{escape(url)}</a></body></html>"
        )
        fallback = await _maybe_translate_html(fallback, language) if needs_translation else fallback
        pdf_bytes = await html_to_pdf_weasyprint_async(fallback) or await html_to_pdf_bytes_chromium(html_str=fallback)
    return pdf_bytes


async def _render_pdf_from_html(html: Optional[str], needs_translation: bool, language: str) -> Optional[bytes]:
    if not html:
        return None
    translated = await _maybe_translate_html(html, language) if needs_translation else html
    # Enforce RTL wrapper for Arabic/Kurdish even if translation fell back to original
    if (language or "en").lower() in {"ar", "ku", "ckb"}:
        translated = inject_rtl(translated, lang=language)
    pdf_bytes = await html_to_pdf_weasyprint_async(translated)
    if not pdf_bytes:
        pdf_bytes = await html_to_pdf_bytes_chromium(html_str=translated)
    return pdf_bytes


async def _maybe_translate_html(html: Optional[str], lang: str) -> str:
    if not html:
        return ""
    lang_code = (lang or "en").lower()
    if lang_code == "en":
        return html
    try:
        timeout_s = float(os.getenv("TRANSLATE_TIMEOUT_SEC", "8.0") or 8.0)
        return await asyncio.wait_for(translate_html(html, lang_code), timeout=timeout_s)
    except asyncio.TimeoutError:
        quick = await _quick_translate_html_google(html, lang_code)
        return quick if quick else inject_rtl(html, lang=lang_code)
    except Exception:
        quick = await _quick_translate_html_google(html, lang_code)
        return quick if quick else inject_rtl(html, lang=lang_code)


async def _fetch_page_html(url: str) -> Optional[str]:
    # Try lightweight HTTP fetch first to avoid launching Playwright.
    try:
        session = await _get_http_session()
        async with session.get(url, timeout=_CARFAX_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type", "") or "").lower()
            if resp.status in (200, 201) and "text/html" in ctype:
                text = await resp.text()
                if "<html" in text.lower():
                    return text
    except Exception:
        pass

    # Fallback to Chromium (shared) only if HTTP fetch failed or returned non-HTML.
    return await fetch_page_html_chromium(url)


def _needs_translation(language: str) -> bool:
    return (language or "en").lower() in {"ar", "ku", "ckb"}


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
