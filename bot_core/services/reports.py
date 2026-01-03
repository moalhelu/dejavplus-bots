"""Carfax/VIN report generation helpers.

FINAL SPEC (non-negotiable):
- If upstream returns HTTP 200 + Content-Type includes application/pdf + non-empty body,
  we must deliver the PDF bytes byte-for-byte with no validation/classification/translation.
- No language-driven "prefer_non_pdf" behavior.
- No report caching or in-flight dedupe here (ledger handles idempotency).
"""

from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bot_core.config import get_env
from bot_core.telemetry import atimed, get_rid
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

_CARFAX_QUEUE_TIMEOUT_SEC = float(os.getenv("CARFAX_QUEUE_TIMEOUT_SEC", "1.0") or 1.0)
_CARFAX_QUEUE_TIMEOUT_SEC = max(0.05, min(_CARFAX_QUEUE_TIMEOUT_SEC, 10.0))


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

VHR_FETCH_FAILED_USER_MESSAGE = "Could not fetch the Vehicle History Report for this VIN. Credit refunded."


def _canonical_api_base() -> str:
    """Canonical DejaVuPlus base URL per docs.

    We ignore non-canonical API_URL values to avoid hitting undocumented routes.
    """

    cfg = get_env()
    env_base = (cfg.api_url or "").strip().rstrip("/")
    canonical = "https://api.dejavuplus.com/api"
    if not env_base:
        return canonical
    low = env_base.lower()
    if "api.dejavuplus.com" in low:
        return env_base
    try:
        LOGGER.warning("Ignoring non-canonical API_URL=%s; using %s", env_base, canonical)
    except Exception:
        pass
    return canonical


def _carfax_url(vin: str, *, ts_ms: Optional[int] = None) -> str:
    base = _canonical_api_base().rstrip("/")
    url = f"{base}/carfax/{vin}"
    if ts_ms is not None:
        url = f"{url}?ts={int(ts_ms)}"
    return url
async def generate_vin_report(vin: str, *, language: str = "en", fast_mode: bool = True) -> ReportResult:
    """Fetch the report from upstream and return the official PDF bytes.

    Language must NOT change the upstream fetch format; it only affects messaging.
    """

    requested_lang = (language or "en").strip().lower()
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

    # Backpressure: fail fast under load.
    acquired_report_slot = False
    try:
        acquire_s = min(_REPORT_QUEUE_TIMEOUT_SEC, max(0.05, _remaining_s()))
        await asyncio.wait_for(_REPORT_GEN_SEM.acquire(), timeout=acquire_s)
        acquired_report_slot = True
    except Exception:
        return ReportResult(
            success=False,
            user_message=_t("report.error.timeout", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن الوقت المحدد."),
            errors=["queue_timeout"],
            vin=normalized_vin,
            error_class=ERROR_UPSTREAM_FETCH_FAILED,
        )

    try:
        fetch_budget = max(0.5, min(_remaining_s(), float(_CARFAX_TIMEOUT)))
        async with atimed("report.upstream_pdf", vin=normalized_vin, lang=requested_lang, fast=bool(fast_mode), budget_s=float(fetch_budget)):
            upstream = await fetch_upstream_pdf(
                normalized_vin,
                total_timeout_s=fetch_budget,
                deadline=deadline,
                force_fresh=True,
            )

        if not upstream.get("ok"):
            err = str(upstream.get("error") or upstream.get("err_text") or f"HTTP_{upstream.get('status','NA')}")
            # Bubble timeouts as timeout UX.
            if err.lower() in {"busy", "queue_timeout", "deadline_exceeded"}:
                return ReportResult(
                    success=False,
                    user_message=_t("report.error.timeout", requested_lang, "⚠️ تعذّر إكمال الطلب ضمن الوقت المحدد."),
                    errors=[err],
                    vin=normalized_vin,
                    raw_response=upstream,
                    error_class=ERROR_UPSTREAM_FETCH_FAILED,
                )
            return ReportResult(
                success=False,
                user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                errors=[err],
                vin=normalized_vin,
                raw_response=upstream,
                error_class=ERROR_UPSTREAM_FETCH_FAILED,
            )

        pdf_bytes = upstream.get("pdf_bytes")
        if not isinstance(pdf_bytes, (bytes, bytearray)) or not bytes(pdf_bytes):
            return ReportResult(
                success=False,
                user_message=VHR_FETCH_FAILED_USER_MESSAGE,
                errors=["no_pdf_bytes"],
                vin=normalized_vin,
                raw_response=upstream,
                error_class=ERROR_UPSTREAM_FETCH_FAILED,
            )

        upstream_sha = str(upstream.get("sha256") or "") or None
        upstream_status = upstream.get("status")
        upstream_ctype = upstream.get("ctype")

        # Stash Fast Mode decisions for delivery-layer messaging. We never generate localized PDFs.
        delivered_lang = "en" if (requested_lang or "en") != "en" else "en"
        skipped_translation = (requested_lang or "en") != "en"
        try:
            upstream.setdefault("_dv_fast", {})
            upstream["_dv_fast"].update(
                {
                    "fast_mode": bool(fast_mode),
                    "skipped_translation": bool(skipped_translation),
                    "requested_lang": (requested_lang or "en"),
                    "delivered_lang": delivered_lang,
                    "total_sec": round(float(time.perf_counter() - start_t), 3),
                }
            )
        except Exception:
            pass

        return ReportResult(
            success=True,
            user_message=_t("report.success.pdf_direct", requested_lang, "✅ Report ready."),
            pdf_bytes=bytes(pdf_bytes),
            pdf_filename=str(upstream.get("filename") or f"{normalized_vin}.pdf"),
            vin=normalized_vin,
            raw_response=upstream,
            upstream_sha256=upstream_sha,
            upstream_status=int(upstream_status) if isinstance(upstream_status, int) else None,
            upstream_content_type=str(upstream_ctype) if upstream_ctype else None,
        )
    finally:
        if acquired_report_slot:
            try:
                _REPORT_GEN_SEM.release()
            except Exception:
                pass

async def _call_carfax_api(
    vin: str,
    *,
    total_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    force_fresh: bool = False,
) -> Dict[str, Any]:
    cfg = get_env()
    headers: Dict[str, str] = {}
    token = (cfg.api_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
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

    # Acquire Carfax slot with a bounded wait; otherwise fail fast as "busy".
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

                if status != 200:
                    try:
                        txt = body.decode("utf-8", errors="ignore")
                    except Exception:
                        txt = ""
                    return {"ok": False, "status": status, "ctype": ctype, "err_text": txt, "final_url": final_url, "sha256": sha256}

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
                    }

                # Preserve non-PDF bodies for debugging (but do not attempt conversion).
                if "application/json" in ctype or (body[:1] == b"{"):
                    try:
                        data = json.loads(body.decode("utf-8", errors="ignore") or "{}")
                        return {"ok": True, "json": data, "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256}
                    except Exception:
                        return {"ok": True, "text": body.decode("utf-8", errors="ignore"), "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256}

                return {"ok": True, "text": body.decode("utf-8", errors="ignore"), "status": status, "final_url": final_url, "ctype": ctype, "sha256": sha256}

    except asyncio.TimeoutError:
        return {"ok": False, "error": "queue_timeout"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
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

    if isinstance(pdf_bytes, (bytes, bytearray)) and bytes(pdf_bytes) and ("application/pdf" in ctype):
        return api_response

    # Non-PDF upstream response: do not attempt conversion.
    return {
        "ok": False,
        "status": api_response.get("status"),
        "ctype": api_response.get("ctype"),
        "final_url": api_response.get("final_url"),
        "sha256": api_response.get("sha256"),
        "error": "non_pdf_upstream",
        "json": api_response.get("json"),
        "text": api_response.get("text"),
    }


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
