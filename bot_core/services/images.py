"""Image providers with basic caching and async-friendly helpers."""
from __future__ import annotations

import asyncio
import os
import logging
import random
import time
from datetime import datetime
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

import httpx

from bot_core.config import get_env

LOGGER = logging.getLogger(__name__)

_IMAGE_DOWNLOAD_MAX_BYTES = int(os.getenv("IMAGE_DOWNLOAD_MAX_BYTES", str(12 * 1024 * 1024)) or (12 * 1024 * 1024))
_IMAGE_DOWNLOAD_MAX_BYTES = max(256_000, min(_IMAGE_DOWNLOAD_MAX_BYTES, 60 * 1024 * 1024))
_IMAGE_DOWNLOAD_DEBUG = str(os.getenv("IMAGE_DOWNLOAD_DEBUG", "")).strip().lower() in ("1", "true", "yes", "on")

# Policy requirement: 2 attempts with short jitter (<=200ms) on 429/5xx/timeouts only.
_IMG_RETRY_ATTEMPTS = 2
_IMG_RETRY_JITTER_MAX_SEC = 0.2


def _rid_or_default(rid: Optional[str]) -> str:
    base = (rid or "").strip()
    if base:
        return base
    return f"img-{int(time.time())}"  # best-effort; caller should pass rid


def _log_image_event(
    rid: Optional[str],
    event: str,
    *,
    url: Optional[str] = None,
    final_url: Optional[str] = None,
    status: Optional[int] = None,
    content_type: Optional[str] = None,
    bytes_len: Optional[int] = None,
    err: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        payload: Dict[str, Any] = {
            "rid": _rid_or_default(rid),
            "event": event,
        }
        if url:
            payload["url"] = url
        if final_url:
            payload["final_url"] = final_url
        if status is not None:
            payload["status"] = int(status)
        if content_type:
            payload["content_type"] = content_type
        if bytes_len is not None:
            payload["bytes_len"] = int(bytes_len)
        if err:
            payload["error"] = err
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
        # Keep structured logs compact and safe.
        LOGGER.info("image_%s %s", event, payload)
    except Exception:
        return

try:  # optional dependency
    from badvin import BadvinScraper
except Exception:  # pragma: no cover
    BadvinScraper = None  # type: ignore

_BADVIN_TOTAL_TIMEOUT = float(os.getenv("BADVIN_TOTAL_TIMEOUT", "25") or 25.0)
_BADVIN_MIN_CACHE_PHOTOS = int(os.getenv("BADVIN_MIN_CACHE_PHOTOS", "6") or 6)
_BADVIN_SEM = asyncio.Semaphore(2)  # simple rate-limit for Badvin login/scrape
_PHOTO_EXCLUDE_MARKERS = (
    "360view",
    "360-view",
    "360_view",
    "360deg",
    "360-degree",
    "360degree",
    "360spin",
    "spin360",
    "threesixty",
    "3sixty",
)
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_HTTP_CLIENT_LOCK = asyncio.Lock()


async def _get_http_client() -> httpx.AsyncClient:
    """Return a shared client; timeouts are set per-request to avoid mismatch."""

    global _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT and not _HTTP_CLIENT.is_closed:
            return _HTTP_CLIENT
        _HTTP_CLIENT = httpx.AsyncClient()
        return _HTTP_CLIENT


def _looks_like_image_bytes(content: bytes, url: str = "", content_type: str = "") -> bool:
    ct = (content_type or "").lower().strip()
    if ct.startswith("image/"):
        return True

    u = (url or "").lower().split("?", 1)[0]
    if u.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return True

    if not content:
        return False

    # Magic bytes for common image formats.
    if content.startswith(b"\xFF\xD8\xFF"):
        return True  # JPEG
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True  # PNG
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return True  # GIF
    if content.startswith(b"RIFF") and b"WEBP" in content[:32]:
        return True  # WebP

    return False


async def get_badvin_images(vin: str) -> List[str]:
    """Fetch Badvin photos (no caching), with retries and basic rate limiting.

    The Badvin scraper already selects the oldest sale record with photos; this
    wrapper adds:
    - in-memory cache
    - concurrency guard to avoid hammering Badvin
    - small retry loop on transient failures
    """

    cfg = get_env()
    if not BadvinScraper or not cfg.badvin_email or not cfg.badvin_password:
        return []

    async def _fetch_once() -> List[str]:
        return await asyncio.to_thread(_badvin_fetch_sync, vin, cfg.badvin_email, cfg.badvin_password)

    async def _fetch_with_retries() -> List[str]:
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                async with _BADVIN_SEM:
                    return await _fetch_once()
            except Exception as exc:  # pragma: no cover - network/login dependent
                last_err = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        if last_err:
            LOGGER.warning("badvin fetch failed vin=%s error=%s", vin, last_err)
        return []

    try:
        urls = await asyncio.wait_for(_fetch_with_retries(), timeout=_BADVIN_TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        LOGGER.warning("badvin fetch timed out vin=%s timeout=%s", vin, _BADVIN_TOTAL_TIMEOUT)
        urls = []
    return urls


async def get_badvin_images_media(vin: str, *, limit: int = 10) -> List[Tuple[str, bytes]]:
    """Fetch BadVin photos as (filename, bytes) using an authenticated session.

    Some deployments (Telegram/WhatsApp) import this helper to reliably send
    protected BadVin images. If the site allows direct URL fetching, callers can
    still use `get_badvin_images`.
    """

    cfg = get_env()
    if not BadvinScraper or not cfg.badvin_email or not cfg.badvin_password:
        return []

    env_limit_raw = os.getenv("BADVIN_MEDIA_LIMIT", "30")
    try:
        env_limit = int(env_limit_raw)
    except Exception:
        env_limit = 30
    # cap to avoid huge sends by accident
    safe_limit = max(1, min(50, min(env_limit, int(limit or env_limit))))

    async def _fetch_once() -> List[Tuple[str, bytes]]:
        return await asyncio.to_thread(
            _badvin_fetch_media_sync,
            vin,
            cfg.badvin_email,
            cfg.badvin_password,
            safe_limit,
        )

    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            async with _BADVIN_SEM:
                return await asyncio.wait_for(_fetch_once(), timeout=_BADVIN_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            last_err = RuntimeError("badvin media fetch timed out")
        except Exception as exc:  # pragma: no cover
            last_err = exc
        await asyncio.sleep(0.5 * (attempt + 1))

    if last_err:
        LOGGER.warning("badvin media fetch failed vin=%s error=%s", vin, last_err)
    return []


def _badvin_fetch_sync(vin: str, email: str, password: str) -> List[str]:
    if BadvinScraper is None:
        raise RuntimeError("BadvinScraper dependency is unavailable")
    scraper = BadvinScraper(email, password)
    try:
        if not scraper.login():
            return []
        result_url = scraper.search_vin(vin)
        if not result_url:
            return []
        def _fetch_html(url: str) -> str:
            try:
                r = scraper.session.get(url, headers=scraper.headers, timeout=getattr(scraper, "timeout", 12.0))
                return getattr(r, "text", "") or ""
            except Exception:
                return ""

        html_candidates: List[str] = []
        # Vehicle landing page
        html_candidates.append(_fetch_html(result_url))

        # Prefer BASIC first to avoid preview/blur-only landing pages; still try FULL afterwards.
        report_types_raw = os.getenv("BADVIN_REPORT_TYPES", "basic,full")
        report_types = [t.strip().lower() for t in report_types_raw.split(",") if t.strip()]
        for rtype in (report_types or ["basic"]):
            try:
                _, report_html = scraper.get_report(result_url, vin, rtype)
                if report_html:
                    html_candidates.append(report_html)
            except Exception:
                continue

        # Extra tab URLs frequently host sale history / photos.
        extra_urls = [
            result_url.rstrip("/") + "/photos",
            result_url.rstrip("/") + "/photos/",
            result_url.rstrip("/") + "/sales-history",
            result_url.rstrip("/") + "/sales-history/",
            result_url + "?tab=photos",
            result_url + "?tab=sales",
        ]
        for u in extra_urls:
            html = _fetch_html(u)
            if html:
                html_candidates.append(html)

        def _dedupe_preserve_order(urls_in: List[str]) -> List[str]:
            out: List[str] = []
            seen: set[str] = set()
            for u in urls_in or []:
                if not isinstance(u, str):
                    continue
                s = u.strip()
                if not s:
                    continue
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
            return out

        def _looks_blurry_url(url: str) -> bool:
            low = (url or "").lower()
            return any(tok in low for tok in ("blur", "blurry", "masked", "preview", "placeholder", "thumb"))

        best_images: List[str] = []
        last_car_data: Dict[str, Any] = {}
        best_car_data: Dict[str, Any] = {}
        for html in html_candidates:
            if not html:
                continue
            car_data, images = scraper.extract_car_data_and_images(html, vin)
            if isinstance(car_data, dict):
                last_car_data = car_data
            images = _dedupe_preserve_order(images)
            if images and len(images) > len(best_images):
                best_images = images
                best_car_data = car_data if isinstance(car_data, dict) else {}

        # If we have enough non-blurry URLs, prefer them.
        if best_images:
            non_blur = [u for u in best_images if not _looks_blurry_url(u)]
            if len(non_blur) >= max(4, len(best_images) // 2):
                best_images = non_blur

        if not best_images:
            if last_car_data:
                LOGGER.info(
                    "badvin: no images vin=%s diag source=%s sale_section=%s blocks=%s json_records=%s",
                    vin,
                    last_car_data.get("source"),
                    last_car_data.get("sale_section_found"),
                    last_car_data.get("sale_record_blocks"),
                    last_car_data.get("json_records"),
                )
            return []
        env_url_limit_raw = os.getenv("BADVIN_URL_LIMIT", os.getenv("BADVIN_MEDIA_LIMIT", "30"))
        try:
            env_url_limit = int(env_url_limit_raw)
        except Exception:
            env_url_limit = 30
        url_limit = max(1, min(60, env_url_limit))

        if best_car_data:
            try:
                LOGGER.info(
                    "badvin: selected images vin=%s count=%s source=%s blocks=%s json_records=%s",
                    vin,
                    len(best_images),
                    best_car_data.get("source"),
                    best_car_data.get("sale_record_blocks"),
                    best_car_data.get("json_records"),
                )
            except Exception:
                pass

        deduped: List[str] = []
        for url in best_images:
            if isinstance(url, str) and url.strip() and url.strip().lower().startswith(("http://", "https://")):
                if url not in deduped:
                    deduped.append(url)
            if len(deduped) >= url_limit:
                break
        return deduped
    except Exception:
        return []
    finally:
        try:
            scraper.logout()
        except Exception:
            pass


def _badvin_fetch_media_sync(vin: str, email: str, password: str, limit: int) -> List[Tuple[str, bytes]]:
    if BadvinScraper is None:
        raise RuntimeError("BadvinScraper dependency is unavailable")

    def _filename_from_url(url: str) -> str:
        try:
            base = (url or "").split("?", 1)[0].rstrip("/")
            name = base.rsplit("/", 1)[-1] or "photo.jpg"
        except Exception:
            name = "photo.jpg"
        if "." not in name:
            name += ".jpg"
        return name

    def _looks_like_image(content: bytes, url: str, ctype: str) -> bool:
        ct = (ctype or "").lower()
        if ct.startswith("image/"):
            return True
        # Some endpoints omit content-type; fall back to URL extension.
        if url and any(url.lower().split("?", 1)[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            return True
        if not content:
            return False
        # Magic bytes for common image formats.
        if content.startswith(b"\xFF\xD8\xFF"):
            return True
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        if content.startswith(b"RIFF") and b"WEBP" in content[:32]:
            return True
        return False

    scraper = BadvinScraper(email, password)
    try:
        if not scraper.login():
            return []
        result_url = scraper.search_vin(vin)
        if not result_url:
            return []

        def _fetch_html(url: str) -> str:
            try:
                r = scraper.session.get(url, headers=scraper.headers, timeout=getattr(scraper, "timeout", 12.0))
                return getattr(r, "text", "") or ""
            except Exception:
                return ""

        html_candidates: List[str] = []
        html_candidates.append(_fetch_html(result_url))

        report_types_raw = os.getenv("BADVIN_REPORT_TYPES", "basic,full")
        report_types = [t.strip().lower() for t in report_types_raw.split(",") if t.strip()]
        for rtype in (report_types or ["basic"]):
            try:
                _, report_html = scraper.get_report(result_url, vin, rtype)
                if report_html:
                    html_candidates.append(report_html)
            except Exception:
                continue

        extra_urls = [
            result_url.rstrip("/") + "/photos",
            result_url.rstrip("/") + "/photos/",
            result_url.rstrip("/") + "/sales-history",
            result_url.rstrip("/") + "/sales-history/",
            result_url + "?tab=photos",
            result_url + "?tab=sales",
        ]
        for u in extra_urls:
            html = _fetch_html(u)
            if html:
                html_candidates.append(html)

        def _dedupe_preserve_order(urls_in: List[str]) -> List[str]:
            out: List[str] = []
            seen: set[str] = set()
            for u in urls_in or []:
                if not isinstance(u, str):
                    continue
                s = u.strip()
                if not s:
                    continue
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
            return out

        def _looks_blurry_url(url: str) -> bool:
            low = (url or "").lower()
            return any(tok in low for tok in ("blur", "blurry", "masked", "preview", "placeholder", "thumb"))

        best_urls: List[str] = []
        last_car_data: Dict[str, Any] = {}
        best_car_data: Dict[str, Any] = {}
        for html in html_candidates:
            if not html:
                continue
            car_data, urls = scraper.extract_car_data_and_images(html, vin)
            if isinstance(car_data, dict):
                last_car_data = car_data
            urls = _dedupe_preserve_order(urls)
            if urls and len(urls) > len(best_urls):
                best_urls = urls
                best_car_data = car_data if isinstance(car_data, dict) else {}

        if best_urls:
            non_blur = [u for u in best_urls if not _looks_blurry_url(u)]
            if len(non_blur) >= max(4, len(best_urls) // 2):
                best_urls = non_blur

        if not best_urls:
            if last_car_data:
                LOGGER.info(
                    "badvin media: no urls vin=%s diag source=%s sale_section=%s blocks=%s json_records=%s",
                    vin,
                    last_car_data.get("source"),
                    last_car_data.get("sale_section_found"),
                    last_car_data.get("sale_record_blocks"),
                    last_car_data.get("json_records"),
                )
            return []

        if best_car_data:
            try:
                LOGGER.info(
                    "badvin media: selected urls vin=%s count=%s source=%s blocks=%s json_records=%s",
                    vin,
                    len(best_urls),
                    best_car_data.get("source"),
                    best_car_data.get("sale_record_blocks"),
                    best_car_data.get("json_records"),
                )
            except Exception:
                pass

        headers = dict(scraper.headers)
        headers["Referer"] = result_url
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

        media: List[Tuple[str, bytes]] = []
        seen: set[str] = set()
        for url in best_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                resp = scraper.session.get(url, headers=headers, timeout=getattr(scraper, "timeout", 12.0))
                if getattr(resp, "status_code", 0) >= 400:
                    continue
                ctype = str(resp.headers.get("content-type", ""))
                content = resp.content or b""
                if not _looks_like_image(content, url, ctype):
                    continue
                if len(content) < 128:
                    continue
                media.append((_filename_from_url(url), content))
                if len(media) >= limit:
                    break
            except Exception:
                continue
        return media
    finally:
        try:
            scraper.logout()
        except Exception:
            pass


def _is_360_spin_url(url: str) -> bool:
    lower = url.lower()
    if "360" not in lower and "three" not in lower:
        return False
    if any(marker in lower for marker in _PHOTO_EXCLUDE_MARKERS):
        return True
    # Treat explicit /360/ path segments as 360-view assets but allow resolution numbers elsewhere.
    if "/360/" in lower or lower.endswith("/360"):
        return True
    return False


def _select_images(urls: List[str], limit: int = 20) -> List[str]:
    selected: List[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        lower = url.lower()
        if _is_360_spin_url(lower):
            continue
        if lower.startswith(("http://", "https://")) and url not in selected:
            selected.append(url)
        if len(selected) >= limit:
            break
    return selected


def _apicar_base_headers() -> Dict[str, str]:
    cfg = get_env()
    return {
        "accept": "*/*",
        "api-key": cfg.apicar_api_key,
    }


def _apicar_base_url(path: str) -> str:
    cfg = get_env()
    return f"{cfg.apicar_base_url.rstrip('/')}/{path.lstrip('/')}"


async def _apicar_fetch_json(path: str, params: Dict[str, str]) -> Any:
    cfg = get_env()
    if not cfg.apicar_api_key:
        return None
    client = await _get_http_client()

    url = _apicar_base_url(path)
    rid = params.get("rid") if isinstance(params, dict) else None
    safe_params = {k: v for k, v in (params or {}).items() if k != "rid"}

    last_exc: Optional[Exception] = None
    for attempt in range(_IMG_RETRY_ATTEMPTS):
        _log_image_event(rid, "fetch_attempt", url=url, extra={"kind": "json", "path": path, "attempt": attempt + 1})
        try:
            resp = await client.get(
                url,
                params=safe_params,
                headers=_apicar_base_headers(),
                timeout=cfg.apicar_timeout,
                follow_redirects=True,
            )
            status = int(resp.status_code)
            ctype = str(resp.headers.get("content-type", ""))
            final_url = str(getattr(resp, "url", url))

            if status == 429 or status >= 500:
                _log_image_event(
                    rid,
                    "fetch_failed",
                    url=url,
                    final_url=final_url,
                    status=status,
                    content_type=ctype,
                    bytes_len=len(resp.content or b""),
                    err=f"http:{status}",
                    extra={"kind": "json", "path": path},
                )
                if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                    await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                    continue
                return None

            resp.raise_for_status()
            data = resp.json()
            keys = list(data.keys())[:20] if isinstance(data, dict) else []
            _log_image_event(
                rid,
                "fetch_ok",
                url=url,
                final_url=final_url,
                status=status,
                content_type=ctype,
                bytes_len=len(resp.content or b""),
                extra={"kind": "json", "path": path, "json_keys": keys},
            )
            return data
        except httpx.TimeoutException as exc:
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err="timeout", extra={"kind": "json", "path": path})
            if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                continue
            return None
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            try:
                status = int(exc.response.status_code)
                ctype = str(exc.response.headers.get("content-type", ""))
                final_url = str(getattr(exc.response, "url", url))
                body_len = len(exc.response.content or b"")
            except Exception:
                status = None
                ctype = ""
                final_url = url
                body_len = None
            _log_image_event(
                rid,
                "fetch_failed",
                url=url,
                final_url=final_url,
                status=status,
                content_type=ctype,
                bytes_len=body_len,
                err="http_status",
                extra={"kind": "json", "path": path},
            )
            if status in (429,) or (status is not None and status >= 500):
                if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                    await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                    continue
            return None
        except httpx.TransportError as exc:
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err="transport", extra={"kind": "json", "path": path})
            if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                continue
            return None
        except Exception as exc:
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err=str(exc), extra={"kind": "json", "path": path})
            return None

    if _IMAGE_DOWNLOAD_DEBUG and last_exc:
        LOGGER.info("apicar json fetch failed path=%s error=%s", path, last_exc)
    return None


def _apicar_extract_images(obj: Any) -> List[str]:
    hd_urls: List[str] = []
    small_urls: List[str] = []

    def add_unique(bucket: List[str], url: Any) -> None:
        if isinstance(url, str):
            stripped = url.strip()
            if stripped.lower().startswith(("http://", "https://")) and stripped not in bucket:
                bucket.append(stripped)

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                key_lower = key.lower() if isinstance(key, str) else ""
                if key_lower == "link_img_hd":
                    add_unique(hd_urls, value)
                    continue
                if key_lower == "link_img_small":
                    add_unique(small_urls, value)
                    continue
                walk(value)
        elif isinstance(node, str):
            add_unique(hd_urls, node)

    walk(obj)
    primary = _select_images(hd_urls, limit=20)
    fallback = [u for u in small_urls if u not in primary]
    return primary + fallback


async def get_apicar_current_images(vin: str, *, rid: Optional[str] = None) -> List[str]:
    key = ("apicar_current", vin)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        data = await _apicar_fetch_json("cars/vin/all", {"vin": vin, "rid": _rid_or_default(rid)})
    except Exception:
        data = None
    urls = _collect_apicar_urls(data)
    if urls:
        _cache_set(key, urls)
    return urls


async def get_apicar_history_images(vin: str, *, rid: Optional[str] = None) -> List[str]:
    key = ("apicar_history", vin)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        data = await _apicar_fetch_json("sale-histories/vin", {"vin": vin, "rid": _rid_or_default(rid)})
    except Exception:
        data = None
    urls = _collect_apicar_urls(data)
    if urls:
        _cache_set(key, urls)
    return urls


async def get_apicar_accident_images(vin: str, *, limit: int = 12, rid: Optional[str] = None) -> List[str]:
    """Accident/old damage images.

    New strategy (per request):
    - Always pull from ApiCar `/cars/vin/all` and pick the *oldest* record (oldest by timestamp; if no timestamps, the last record in the list).
    - Use `link_img_hd` only (prefer HD images of that record) and respect the requested limit.
    - If nothing is found, fall back to the existing history → badvin chain.
    """

    vin_norm = (vin or "").strip()
    cache_key = ("accident_primary", vin_norm)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # --- Primary: cars/vin/all oldest-record HD images ---
    primary_urls: List[str] = []
    try:
        data = await _apicar_fetch_json("cars/vin/all", {"vin": vin_norm, "rid": _rid_or_default(rid)})
        primary_urls = _collect_oldest_hd_urls(data)
    except Exception as exc:  # pragma: no cover - network dependent
        LOGGER.warning("apicar cars/vin/all fetch failed vin=%s error=%s", vin_norm, exc)
        primary_urls = []

    if primary_urls:
        selected = _select_images(primary_urls, limit=limit)
        _cache_set(cache_key, selected)
        return selected

    # --- Fallback: History API (oldest-first) ---
    try:
        hist_urls = await get_apicar_history_images(vin_norm, rid=_rid_or_default(rid))
    except Exception as exc:  # pragma: no cover - network dependent
        LOGGER.warning("history fetch failed vin=%s error=%s", vin_norm, exc)
        hist_urls = []

    if hist_urls:
        selected = _select_images(hist_urls, limit=limit)
        _cache_set(cache_key, selected)
        return selected

    # --- Fallback: Badvin (oldest sale record only) ---
    try:
        badvin_urls = await get_badvin_images(vin_norm)
    except Exception as exc:  # pragma: no cover - network/login dependent
        LOGGER.warning("badvin fallback failed vin=%s error=%s", vin_norm, exc)
        badvin_urls = []

    selected = _select_images(badvin_urls, limit=limit)
    if selected:
        _cache_set(cache_key, selected)
    return selected


async def get_hidden_vehicle_images(vin: str, *, limit: int = 20, rid: Optional[str] = None) -> List[str]:
    """Hidden vehicle photos.

    Primary source is BadVin (when credentials are configured).
    If BadVin is unavailable or returns no images, fall back to ApiCar images
    so users still get a useful photo bundle.
    """

    vin_norm = (vin or "").strip()
    cache_key = ("hidden_vehicle", vin_norm, int(limit or 20))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    urls: List[str] = []
    try:
        urls = await get_badvin_images(vin_norm)
    except Exception:
        urls = []
    selected = _select_images(urls or [], limit=int(limit or 20))
    if selected:
        _cache_set(cache_key, selected)
        return selected

    # Fallback: ApiCar current (often has auction/gallery images)
    try:
        urls = await get_apicar_current_images(vin_norm, rid=_rid_or_default(rid))
    except Exception:
        urls = []
    selected = _select_images(urls or [], limit=int(limit or 20))
    if selected:
        _cache_set(cache_key, selected)
        return selected

    # Fallback: ApiCar history
    try:
        urls = await get_apicar_history_images(vin_norm, rid=_rid_or_default(rid))
    except Exception:
        urls = []
    selected = _select_images(urls or [], limit=int(limit or 20))
    if selected:
        _cache_set(cache_key, selected)
        return selected

    _cache_set(cache_key, [])
    return []


def _collect_apicar_urls(data: Any) -> List[str]:
    if not data:
        return []

    def _parse_ts(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            try:
                # Handle trailing Z by replacing with UTC offset
                iso_raw = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                return datetime.fromisoformat(iso_raw).timestamp()
            except Exception:
                return None
        return None

    def _ts_from_entry(entry: Dict[str, Any]) -> Optional[float]:
        for key in ("auction_date", "sale_date", "sold_date", "date", "created_at", "updated_at", "saleDate"):
            ts = _parse_ts(entry.get(key))
            if ts is not None:
                return ts
        return None

    def _push(target: List[str], value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                _push(target, item)
        elif isinstance(value, str):
            trimmed = value.strip()
            if trimmed.lower().startswith(("http://", "https://")):
                target.append(trimmed)

    # Normalize incoming payload
    if isinstance(data, dict):
        raw = data.get("data", data)
        entries = raw if isinstance(raw, list) else [raw]
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    # Build candidate buckets (top-level entries + sale_history records)
    candidates: List[Tuple[Optional[float], List[str], List[str]]] = []

    def _add_candidate(entry: Dict[str, Any]) -> None:
        primary_hd: List[str] = []
        primary_small: List[str] = []
        fallback: List[str] = []
        _push(primary_small, entry.get("link_img_small"))
        _push(primary_hd, entry.get("link_img_hd"))
        _push(fallback, entry.get("images"))
        _push(fallback, entry.get("link_img_small"))
        _push(fallback, entry.get("link_img_hd"))
        ts = _ts_from_entry(entry)
        candidates.append((ts, primary_small, primary_hd, fallback))

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        _add_candidate(entry)
        sale_history = entry.get("sale_history")
        if isinstance(sale_history, list):
            for hist_entry in sale_history:
                if isinstance(hist_entry, dict):
                    _add_candidate(hist_entry)

    if not candidates:
        return []

    dated = [c for c in candidates if c[0] is not None]
    if dated:
        dated.sort(key=lambda x: x[0])  # oldest first
        chosen = dated[0]
    else:
        # If no timestamps, pick the first candidate (treat earlier entries as older)
        chosen = candidates[0]

    _, primary_small, primary_hd, fallback_urls = chosen

    cleaned: List[str] = []
    # Prefer smaller images first (more reliable for WhatsApp), then HD, then fallback.
    for url in _select_images(primary_small, limit=20):
        if url not in cleaned:
            cleaned.append(url)
    if len(cleaned) < 20:
        for url in _select_images(primary_hd, limit=20):
            if url not in cleaned:
                cleaned.append(url)
            if len(cleaned) >= 20:
                break
    if len(cleaned) < 20:
        for url in _select_images(fallback_urls, limit=20):
            if url not in cleaned:
                cleaned.append(url)
            if len(cleaned) >= 20:
                break

    return cleaned


def _collect_oldest_hd_urls(data: Any) -> List[str]:
    """Select HD images from the oldest available record in /cars/vin/all payload.

    Rules:
    - Use link_img_hd only (no mixed sources) to honor request for accident photos.
    - Oldest is determined by the earliest timestamp among known date fields; if none, pick the last entry (older at bottom).
    - If sale_history exists, it is treated as part of the same record chronology; we still pick the outer record's timestamp.
    """

    if not data:
        return []

    def _parse_ts(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            try:
                iso_raw = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                return datetime.fromisoformat(iso_raw).timestamp()
            except Exception:
                return None
        return None

    def _ts_from_entry(entry: Dict[str, Any]) -> Optional[float]:
        for key in ("auction_date", "sale_date", "sold_date", "date", "created_at", "updated_at", "saleDate"):
            ts = _parse_ts(entry.get(key))
            if ts is not None:
                return ts
        return None

    raw_entries = data.get("data", data) if isinstance(data, dict) else data
    entries = raw_entries if isinstance(raw_entries, list) else [raw_entries]
    candidates: List[Tuple[int, Optional[float], List[str]]] = []  # (index, ts, hd_urls)

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        hd_urls: List[str] = []
        value = entry.get("link_img_hd")
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip().lower().startswith(("http://", "https://")):
                    hd_urls.append(item.strip())
        elif isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
            hd_urls.append(value.strip())
        ts = _ts_from_entry(entry)
        # Prefer the record itself; sale_history entries remain ignored for image selection
        if hd_urls:
            candidates.append((idx, ts, hd_urls))

    if not candidates:
        return []

    dated = [c for c in candidates if c[1] is not None]
    if dated:
        dated.sort(key=lambda x: x[1])  # oldest timestamp first
        chosen = dated[0]
    else:
        # No timestamps: assume list order is newest->oldest, so pick last entry
        candidates.sort(key=lambda x: x[0], reverse=True)
        chosen = candidates[0]

    _, _, hd_urls = chosen
    # Keep original order; limit later
    cleaned = []
    for url in hd_urls:
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


async def download_image_bytes(url: str, *, rid: Optional[str] = None) -> Optional[bytes]:
    cfg = get_env()

    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return None

    def _same_host(a: str, b: str) -> bool:
        try:
            return bool(urlparse(a).netloc) and urlparse(a).netloc.lower() == urlparse(b).netloc.lower()
        except Exception:
            return False

    headers: Dict[str, str] = {
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (compatible; DejavuPlusBot/1.0)",
    }

    # Some ApiCar image links require the api-key header. If the host matches,
    # inject it for the download step (Telegram/WhatsApp URL-fetch cannot).
    apicar_base = (cfg.apicar_base_url or "").strip()
    if cfg.apicar_api_key and apicar_base and _same_host(url, apicar_base):
        headers["api-key"] = cfg.apicar_api_key

    last_exc: Optional[Exception] = None
    for attempt in range(_IMG_RETRY_ATTEMPTS):
        _log_image_event(rid, "fetch_attempt", url=url, extra={"kind": "image", "attempt": attempt + 1})
        try:
            client = await _get_http_client()
            async with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=cfg.apicar_image_timeout,
                follow_redirects=True,
            ) as resp:
                status = int(resp.status_code)
                final_url = str(getattr(resp, "url", url))
                ctype = str(resp.headers.get("content-type", ""))
                if status >= 400:
                    _log_image_event(
                        rid,
                        "fetch_failed",
                        url=url,
                        final_url=final_url,
                        status=status,
                        content_type=ctype,
                        err=f"http:{status}",
                        extra={"kind": "image"},
                    )
                    if (status == 429 or status >= 500) and attempt < (_IMG_RETRY_ATTEMPTS - 1):
                        await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                        continue
                    return None
                clen = resp.headers.get("content-length")
                try:
                    if clen is not None and int(clen) > _IMAGE_DOWNLOAD_MAX_BYTES:
                        if _IMAGE_DOWNLOAD_DEBUG:
                            LOGGER.info("image download too large url=%s content_length=%s", url, clen)
                        return None
                except Exception:
                    pass

                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > _IMAGE_DOWNLOAD_MAX_BYTES:
                        if _IMAGE_DOWNLOAD_DEBUG:
                            LOGGER.info("image download exceeded max bytes url=%s bytes=%s", url, len(buf))
                        return None

                content = bytes(buf)
                if len(content) < 128:
                    _log_image_event(
                        rid,
                        "fetch_failed",
                        url=url,
                        final_url=final_url,
                        status=status,
                        content_type=ctype,
                        bytes_len=len(content),
                        err="too_small",
                        extra={"kind": "image"},
                    )
                    return None
                if not _looks_like_image_bytes(content, url=url, content_type=ctype):
                    _log_image_event(
                        rid,
                        "fetch_failed",
                        url=url,
                        final_url=final_url,
                        status=status,
                        content_type=ctype,
                        bytes_len=len(content),
                        err="not_image",
                        extra={"kind": "image"},
                    )
                    return None
                _log_image_event(
                    rid,
                    "fetch_ok",
                    url=url,
                    final_url=final_url,
                    status=status,
                    content_type=ctype,
                    bytes_len=len(content),
                    extra={"kind": "image"},
                )
                return content
        except httpx.TimeoutException as exc:  # pragma: no cover
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err="timeout", extra={"kind": "image"})
            if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                continue
            return None
        except httpx.TransportError as exc:  # pragma: no cover
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err="transport", extra={"kind": "image"})
            if attempt < (_IMG_RETRY_ATTEMPTS - 1):
                await asyncio.sleep(random.random() * _IMG_RETRY_JITTER_MAX_SEC)
                continue
            return None
        except Exception as exc:  # pragma: no cover
            last_exc = exc
            _log_image_event(rid, "fetch_failed", url=url, err=str(exc), extra={"kind": "image"})
            return None

    if _IMAGE_DOWNLOAD_DEBUG and last_exc:
        LOGGER.info("image download failed url=%s error=%s", url, last_exc)
    return None
