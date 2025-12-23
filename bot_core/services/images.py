"""Image providers with basic caching and async-friendly helpers."""
from __future__ import annotations

import asyncio
import os
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from bot_core.config import get_env

LOGGER = logging.getLogger(__name__)

try:  # optional dependency
    from badvin import BadvinScraper
except Exception:  # pragma: no cover
    BadvinScraper = None  # type: ignore

_CACHE_TTL = 30 * 60  # seconds
_BADVIN_TOTAL_TIMEOUT = float(os.getenv("BADVIN_TOTAL_TIMEOUT", "25") or 25.0)
_IMAGE_CACHE: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
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


def _cache_get(key: Tuple[str, str]) -> Optional[List[str]]:
    exp_payload = _IMAGE_CACHE.get(key)
    if not exp_payload:
        return None
    expires_at, payload = exp_payload
    if expires_at > time.time():
        return list(payload)
    _IMAGE_CACHE.pop(key, None)
    return None


def _cache_set(key: Tuple[str, str], urls: List[str]) -> None:
    _IMAGE_CACHE[key] = (time.time() + _CACHE_TTL, list(urls))


async def get_badvin_images(vin: str) -> List[str]:
    """Fetch Badvin photos with caching, retries, and basic rate limiting.

    The Badvin scraper already selects the oldest sale record with photos; this
    wrapper adds:
    - in-memory cache
    - concurrency guard to avoid hammering Badvin
    - small retry loop on transient failures
    """

    key = ("badvin", vin)
    cached = _cache_get(key)
    if cached is not None:
        return cached

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
    if urls:
        _cache_set(key, urls)
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

    safe_limit = max(1, min(10, int(limit)))

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

        # Prefer FULL when account is purchased; fallback to BASIC.
        report_types_raw = os.getenv("BADVIN_REPORT_TYPES", "full,basic")
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

        images: List[str] = []
        for html in html_candidates:
            if not html:
                continue
            _, images = scraper.extract_car_data_and_images(html, vin)
            if images:
                break

        if not images:
            return []
        deduped: List[str] = []
        for url in images:
            if isinstance(url, str) and url.strip() and url.strip().lower().startswith(("http://", "https://")):
                if url not in deduped:
                    deduped.append(url)
            if len(deduped) >= 20:
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

    scraper = BadvinScraper(email, password)
    try:
        if not scraper.login():
            return []
        result_url = scraper.search_vin(vin)
        if not result_url:
            return []

        # Reuse the URL extraction logic (oldest sale record with photos) then download bytes.
        urls = _badvin_fetch_sync(vin, email, password)
        if not urls:
            return []

        headers = dict(scraper.headers)
        headers["Referer"] = result_url
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

        media: List[Tuple[str, bytes]] = []
        seen: set[str] = set()
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                resp = scraper.session.get(url, headers=headers, timeout=getattr(scraper, "timeout", 12.0))
                if getattr(resp, "status_code", 0) >= 400:
                    continue
                ctype = str(resp.headers.get("content-type", "")).lower()
                if "image" not in ctype:
                    continue
                content = resp.content or b""
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
    resp = await client.get(
        _apicar_base_url(path),
        params=params,
        headers=_apicar_base_headers(),
        timeout=cfg.apicar_timeout,
    )
    resp.raise_for_status()
    return resp.json()


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


async def get_apicar_current_images(vin: str) -> List[str]:
    key = ("apicar_current", vin)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        data = await _apicar_fetch_json("cars/vin/all", {"vin": vin})
    except Exception:
        data = None
    urls = _collect_apicar_urls(data)
    if urls:
        _cache_set(key, urls)
    return urls


async def get_apicar_history_images(vin: str) -> List[str]:
    key = ("apicar_history", vin)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        data = await _apicar_fetch_json("sale-histories/vin", {"vin": vin})
    except Exception:
        data = None
    urls = _collect_apicar_urls(data)
    if urls:
        _cache_set(key, urls)
    return urls


async def get_apicar_accident_images(vin: str, *, limit: int = 12) -> List[str]:
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
        data = await _apicar_fetch_json("cars/vin/all", {"vin": vin_norm})
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
        hist_urls = await get_apicar_history_images(vin_norm)
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


async def download_image_bytes(url: str) -> Optional[bytes]:
    cfg = get_env()
    try:
        client = await _get_http_client()
        resp = await client.get(url, timeout=cfg.apicar_image_timeout)
        resp.raise_for_status()
        if "image" not in (resp.headers.get("content-type", "").lower()):
            return None
        return resp.content
    except Exception:
        return None
