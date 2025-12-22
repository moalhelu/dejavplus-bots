"""PDF generation helpers (WeasyPrint / Playwright)."""
from __future__ import annotations

import os
import asyncio
import re
import mimetypes
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Optional, List

from bot_core.telemetry import atimed


def _get_pdf_wait_until() -> str:
    value = (os.getenv("PDF_WAIT_UNTIL", "networkidle") or "").strip().lower()
    if value in {"load", "domcontentloaded", "networkidle"}:
        return value
    return "networkidle"


def _pdf_wait_until_was_explicitly_set() -> bool:
    # If user explicitly set PDF_WAIT_UNTIL, we should not override it.
    return os.getenv("PDF_WAIT_UNTIL") is not None


def _get_pdf_timeout_ms() -> int:
    # Default lowered to avoid very long stalls when waiting for networkidle.
    # If the page is "good enough", we still generate a PDF even if wait_until times out.
    raw = (os.getenv("PDF_TIMEOUT_MS", "30000") or "").strip()
    try:
        timeout_ms = int(raw)
    except Exception:
        return 60000
    return max(1_000, min(timeout_ms, 300_000))


def _pdf_fast_first_enabled() -> bool:
    return (os.getenv("PDF_FAST_FIRST", "1") or "").strip().lower() not in {"0", "false", "off"}


def _pdf_fast_first_timeout_ms() -> int:
    raw = (os.getenv("PDF_FAST_FIRST_TIMEOUT_MS", "12000") or "").strip()
    try:
        timeout_ms = int(raw)
    except Exception:
        timeout_ms = 12000
    return max(1_000, min(timeout_ms, 60_000))


def _pdf_bytes_looks_ok(pdf_bytes: Optional[bytes]) -> bool:
    if not pdf_bytes:
        return False
    # Heuristic: valid PDFs start with %PDF and are usually not tiny.
    if not pdf_bytes.startswith(b"%PDF"):
        return False
    # Many broken/blank renders are extremely small.
    return len(pdf_bytes) >= 30_000


def _get_pdf_block_resource_types() -> set[str]:
    """Optional Playwright resource blocking.

    Example: PDF_BLOCK_RESOURCE_TYPES=image,font,media
    Defaults to no blocking.
    """

    raw = (os.getenv("PDF_BLOCK_RESOURCE_TYPES", "") or "").strip().lower()
    if not raw:
        return set()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    allowed = {"image", "media", "font"}
    return {p for p in parts if p in allowed}


async def _ensure_page_configured(page) -> None:
    """Configure a pooled Playwright page once (idempotent)."""

    if not page:
        return
    try:
        if getattr(page, "_dv_pdf_configured", False):
            return
    except Exception:
        # If we can't set attributes, just fall through and try config.
        pass

    block_types = _get_pdf_block_resource_types()
    if not block_types:
        try:
            setattr(page, "_dv_pdf_configured", True)
        except Exception:
            pass
        return

    async def _route_handler(route, request) -> None:  # pragma: no cover
        try:
            rtype = (getattr(request, "resource_type", "") or "").lower()
            if rtype in block_types:
                await route.abort()
                return
        except Exception:
            # On any handler failure, continue request to avoid breaking the page.
            pass
        try:
            await route.continue_()
        except Exception:
            pass

    try:
        await page.route("**/*", _route_handler)
    except Exception:
        # If routing isn't available (or already routed), keep going without blocking.
        pass

    try:
        setattr(page, "_dv_pdf_configured", True)
    except Exception:
        pass

_PDF_PLAYWRIGHT = None
_PDF_BROWSER = None
_PDF_BROWSER_LOCK = asyncio.Lock()
_PDF_RENDER_SEM = asyncio.Semaphore(8)
_PDF_PAGE_POOL: List[object] = []
_PDF_PAGE_LOCK = asyncio.Lock()
_PDF_PAGE_MAX = 8
_PDF_PREWARM_ENABLED = os.getenv("ENABLE_PDF_PREWARM", "1").lower() not in {"0", "false", "off"}
_PDF_PREWARM_PAGES = int(os.getenv("PDF_PREWARM_PAGES", "1") or 1)


def html_to_pdf_bytes_weasyprint(html_str: str) -> Optional[bytes]:
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except Exception:
        return None
    try:
        css = CSS(string="@page { size: A4; margin: 10mm; } body{font-family:Arial,Helvetica,sans-serif}")

        def _weasyprint_fast_fetch_enabled() -> bool:
            return (os.getenv("WEASYPRINT_FAST_FETCH", "0") or "").strip().lower() in {"1", "true", "yes", "on"}

        def _weasyprint_url_timeout_s() -> float:
            raw = (os.getenv("WEASYPRINT_URL_TIMEOUT_S", "5") or "").strip()
            try:
                val = float(raw)
            except Exception:
                val = 5.0
            return max(0.5, min(val, 30.0))

        def _weasyprint_block_resource_types() -> set[str]:
            raw = (os.getenv("WEASYPRINT_BLOCK_RESOURCE_TYPES", "") or "").strip().lower()
            if not raw:
                return set()
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            allowed = {"image", "font", "media"}
            return {p for p in parts if p in allowed}

        def _guess_type_from_url(url: str) -> str:
            low = (url or "").lower()
            mime, _ = mimetypes.guess_type(low)
            return (mime or "application/octet-stream").lower()

        def _is_blocked(url: str, mime_type: str, blocked: set[str]) -> bool:
            if not blocked:
                return False
            mt = (mime_type or "").lower()
            if "image" in blocked and mt.startswith("image/"):
                return True
            if "font" in blocked and ("font" in mt or mt in {"application/font-woff", "application/font-woff2", "application/vnd.ms-fontobject"}):
                return True
            if "media" in blocked and (mt.startswith("video/") or mt.startswith("audio/")):
                return True
            # Heuristic by extension for some common cases when mime is generic.
            low = (url or "").lower()
            if "image" in blocked and low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
                return True
            if "font" in blocked and low.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
                return True
            if "media" in blocked and low.endswith((".mp4", ".webm", ".mp3", ".wav", ".ogg")):
                return True
            return False

        def _fast_url_fetcher(url: str):  # type: ignore[no-untyped-def]
            blocked = _weasyprint_block_resource_types()
            mime_type = _guess_type_from_url(url)
            if _is_blocked(url, mime_type, blocked):
                return {"string": b"", "mime_type": mime_type}

            timeout_s = _weasyprint_url_timeout_s()
            try:
                req = Request(url, headers={"User-Agent": "dejavuplus-bots/weasyprint"})
                with urlopen(req, timeout=timeout_s) as resp:
                    data = resp.read()
                    ct = (resp.headers.get("Content-Type") or mime_type).split(";")[0].strip().lower()
                    return {"string": data, "mime_type": ct}
            except (HTTPError, URLError, TimeoutError, ValueError):
                # Fail soft: empty resource (WeasyPrint will render without it).
                return {"string": b"", "mime_type": mime_type}

        if _weasyprint_fast_fetch_enabled():
            return HTML(string=html_str, base_url=".", url_fetcher=_fast_url_fetcher).write_pdf(stylesheets=[css])

        return HTML(string=html_str, base_url=".").write_pdf(stylesheets=[css])
    except Exception:
        return None


async def html_to_pdf_weasyprint_async(html_str: str) -> Optional[bytes]:
    async with atimed("pdf.weasyprint", html_len=len(html_str or "")):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, html_to_pdf_bytes_weasyprint, html_str)


import traceback

async def _ensure_browser():
    """Re-use a single Chromium instance to avoid cold starts per PDF."""

    global _PDF_PLAYWRIGHT, _PDF_BROWSER
    try:
        async with _PDF_BROWSER_LOCK:
            try:
                if _PDF_BROWSER and not _PDF_BROWSER.is_closed():
                    return _PDF_BROWSER
            except Exception:
                _PDF_BROWSER = None

            if _PDF_PLAYWRIGHT is None:
                from playwright.async_api import async_playwright  # type: ignore
                _PDF_PLAYWRIGHT = await async_playwright().start()

            _PDF_BROWSER = await _PDF_PLAYWRIGHT.chromium.launch()
            if _PDF_PREWARM_ENABLED:
                try:
                    page = await _PDF_BROWSER.new_page()
                    try:
                        await page.goto("about:blank")
                        await _ensure_page_configured(page)
                        async with _PDF_PAGE_LOCK:
                            if len(_PDF_PAGE_POOL) < _PDF_PAGE_MAX:
                                _PDF_PAGE_POOL.append(page)
                            else:
                                await page.close()
                    except Exception:
                        try:
                            await page.close()
                        except Exception:
                            pass
                except Exception:
                    pass
            return _PDF_BROWSER
    except Exception as exc:
        with open("pdf_errors.log", "a", encoding="utf-8") as f:
            f.write(f"Browser Init Error: {repr(exc)}\n")
        _PDF_BROWSER = None
        return None


async def prewarm_pdf_engine() -> None:
    """Eagerly initialize the shared Chromium browser + a small page pool.

    Intended to run at service startup (Telegram/WhatsApp) to avoid the first
    user request paying the Playwright cold-start cost.
    """

    if not _PDF_PREWARM_ENABLED:
        return
    try:
        pages = max(1, min(int(_PDF_PREWARM_PAGES), _PDF_PAGE_MAX))
    except Exception:
        pages = 1

    try:
        async with atimed("pdf.prewarm", pages=pages):
            browser = await _ensure_browser()
            if browser is None:
                return
            # Ensure we have at least N warmed pages ready.
            created: List[object] = []
            try:
                async with _PDF_PAGE_LOCK:
                    needed = max(0, pages - len(_PDF_PAGE_POOL))
                for _ in range(needed):
                    try:
                        page = await browser.new_page()
                        await page.goto("about:blank")
                        await _ensure_page_configured(page)
                        created.append(page)
                    except Exception:
                        break

                if created:
                    async with _PDF_PAGE_LOCK:
                        while created and len(_PDF_PAGE_POOL) < _PDF_PAGE_MAX:
                            _PDF_PAGE_POOL.append(created.pop())
            finally:
                for page in created:
                    try:
                        await page.close()
                    except Exception:
                        pass
    except Exception:
        return


async def _reset_browser() -> None:
    global _PDF_BROWSER
    global _PDF_PAGE_POOL
    try:
        if _PDF_BROWSER and not _PDF_BROWSER.is_closed():
            await _PDF_BROWSER.close()
    except Exception:
        pass
    _PDF_BROWSER = None
    _PDF_PAGE_POOL = []


async def _acquire_page():
    browser = await _ensure_browser()
    if browser is None:
        return None

    async with _PDF_PAGE_LOCK:
        # Reuse an existing idle page if available
        while _PDF_PAGE_POOL:
            page = _PDF_PAGE_POOL.pop()
            try:
                if page and not page.is_closed():
                    return page
            except Exception:
                continue

        # Create a new page if under the cap
        try:
            if len(_PDF_PAGE_POOL) < _PDF_PAGE_MAX:
                return await browser.new_page()
        except Exception:
            return None

    # Fallback: create page outside the lock if pool was busy but under cap
    try:
        return await browser.new_page()
    except Exception:
        return None


async def _release_page(page) -> None:
    if not page:
        return
    try:
        if page.is_closed():
            return
    except Exception:
        return
    async with _PDF_PAGE_LOCK:
        if len(_PDF_PAGE_POOL) < _PDF_PAGE_MAX:
            _PDF_PAGE_POOL.append(page)
            return
    try:
        await page.close()
    except Exception:
        pass


async def html_to_pdf_bytes_chromium(html_str: Optional[str] = None, url: Optional[str] = None) -> Optional[bytes]:
    try:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # pragma: no cover
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        wait_until = _get_pdf_wait_until()
        timeout_ms = _get_pdf_timeout_ms()
        block_types = sorted(_get_pdf_block_resource_types())
        fast_first = _pdf_fast_first_enabled() and not _pdf_wait_until_was_explicitly_set()
        fast_first_timeout_ms = min(_pdf_fast_first_timeout_ms(), timeout_ms)
        async with atimed(
            "pdf.chromium",
            html_len=len(html_str or "") if html_str else 0,
            has_url=bool(url),
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            block_types=",".join(block_types),
            fast_first=fast_first,
            fast_first_timeout_ms=fast_first_timeout_ms,
        ):
            async with _PDF_RENDER_SEM:
                page = await _acquire_page()
                try:
                    await _ensure_page_configured(page)
                    if url:
                        if fast_first:
                            try:
                                await page.goto(url, wait_until="domcontentloaded", timeout=fast_first_timeout_ms)
                                pdf_bytes = await page.pdf(
                                    format="A4",
                                    print_background=True,
                                    margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
                                )
                                if _pdf_bytes_looks_ok(pdf_bytes):
                                    return pdf_bytes
                            except Exception:
                                pass
                        try:
                            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                        except PlaywrightTimeoutError:
                            # If we timed out waiting for the chosen load state (often networkidle),
                            # the DOM may still be sufficiently rendered for printing.
                            pass
                    elif html_str:
                        clean = re.sub(r"<script\b[^>]*>.*?</script>", "", html_str, flags=re.I | re.S)
                        if "<head" in clean.lower() and "<base" not in clean.lower():
                            clean = re.sub(r"(?i)<head([^>]*)>", r"<head\1><base href='https://www.carfax.com/'>", clean, count=1)
                        if fast_first:
                            try:
                                await page.set_content(clean, wait_until="domcontentloaded", timeout=fast_first_timeout_ms)
                                pdf_bytes = await page.pdf(
                                    format="A4",
                                    print_background=True,
                                    margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
                                )
                                if _pdf_bytes_looks_ok(pdf_bytes):
                                    return pdf_bytes
                            except Exception:
                                pass
                        try:
                            await page.set_content(clean, wait_until=wait_until, timeout=timeout_ms)
                        except PlaywrightTimeoutError:
                            # Same idea as goto(): don't fail the whole render just because
                            # a load-state condition didn't settle.
                            pass
                    else:
                        return None

                    pdf_bytes = await page.pdf(
                        format="A4",
                        print_background=True,
                        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
                    )
                    return pdf_bytes
                finally:
                    await _release_page(page)
    except Exception as e:
        await _reset_browser()
        with open("pdf_errors.log", "a", encoding="utf-8") as f:
            f.write(f"Runtime Error: {repr(e)}\n")
            f.write(traceback.format_exc() + "\n")
        return None


async def fetch_page_html_chromium(url: str) -> Optional[str]:
    """Fetch fully-rendered page HTML using the shared Chromium instance.

    This avoids the per-call Playwright cold start used by older fallback code.
    """

    if not url:
        return None
    try:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # pragma: no cover
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        wait_until = _get_pdf_wait_until()
        timeout_ms = _get_pdf_timeout_ms()
        block_types = sorted(_get_pdf_block_resource_types())
        async with atimed(
            "pdf.chromium.fetch_html",
            has_url=True,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            block_types=",".join(block_types),
        ):
            async with _PDF_RENDER_SEM:
                page = await _acquire_page()
                try:
                    await _ensure_page_configured(page)
                    try:
                        await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                    except PlaywrightTimeoutError:
                        pass
                    return await page.content()
                finally:
                    await _release_page(page)
    except Exception:
        return None
