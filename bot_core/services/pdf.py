"""PDF generation helpers (Playwright/Chromium)."""
from __future__ import annotations

import os
import asyncio
import re
import traceback
from urllib.parse import urlparse
from typing import Optional, List

from bot_core.telemetry import atimed


class PdfBusyError(RuntimeError):
    """Raised when the PDF engine is saturated (queue timeout waiting for a slot)."""


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


def _pdf_fast_first_wait_until() -> str:
    value = (os.getenv("PDF_FAST_FIRST_WAIT_UNTIL", "load") or "").strip().lower()
    if value in {"load", "domcontentloaded", "networkidle"}:
        return value
    return "load"


def _html_base_url_default() -> str:
    return (os.getenv("PDF_HTML_BASE_URL", "https://www.carfax.com/") or "https://www.carfax.com/").strip()


def _compute_base_href(base_url: Optional[str]) -> str:
    raw = (base_url or "").strip() or _html_base_url_default()
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return _html_base_url_default()

        # Use the directory of the path so relative assets resolve correctly.
        path = parsed.path or "/"
        if not path.endswith("/"):
            path = path.rsplit("/", 1)[0] + "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    except Exception:
        return _html_base_url_default()


def _pdf_bytes_looks_ok(pdf_bytes: Optional[bytes]) -> bool:
    if not pdf_bytes:
        return False
    # Heuristic: valid PDFs start with %PDF and are usually not tiny.
    if not pdf_bytes.startswith(b"%PDF"):
        return False
    # Many broken/blank renders are extremely small, but some valid PDFs can also be
    # smaller than our old 30KB threshold. Keep this both fast and safer by checking
    # for common page markers.
    raw_min = (os.getenv("PDF_MIN_BYTES_OK", "12000") or "12000").strip()
    try:
        min_bytes = int(raw_min)
    except Exception:
        min_bytes = 12000
    min_bytes = max(4000, min(min_bytes, 200_000))
    if len(pdf_bytes) < min_bytes:
        return False
    # Lightweight structure check (works for many non-encrypted PDFs; if not found,
    # we still may accept if the PDF isn't tiny).
    head = pdf_bytes[:200_000]
    if b"/Type /Page" in head or b"/Type/Pages" in head or b"/Pages" in head:
        return True
    # Fall back to size-only acceptance.
    return True


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


async def html_to_pdf_bytes_chromium(
    html_str: Optional[str] = None,
    url: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    acquire_timeout_ms: Optional[int] = None,
    wait_until: Optional[str] = None,
    fast_first_timeout_ms: Optional[int] = None,
    fast_first_wait_until: Optional[str] = None,
) -> Optional[bytes]:
    try:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # type: ignore
        except Exception:  # pragma: no cover
            PlaywrightTimeoutError = Exception  # type: ignore[assignment]

        wait_until = (wait_until or _get_pdf_wait_until()).strip().lower()
        if wait_until not in {"load", "domcontentloaded", "networkidle"}:
            wait_until = _get_pdf_wait_until()

        timeout_ms = int(timeout_ms) if timeout_ms is not None else _get_pdf_timeout_ms()
        timeout_ms = max(1_000, min(timeout_ms, 300_000))
        block_types = sorted(_get_pdf_block_resource_types())
        fast_first = _pdf_fast_first_enabled() and not _pdf_wait_until_was_explicitly_set()
        if fast_first_timeout_ms is not None:
            try:
                fast_first_timeout_ms = int(fast_first_timeout_ms)
            except Exception:
                fast_first_timeout_ms = None
        fast_first_timeout_ms = min(fast_first_timeout_ms or _pdf_fast_first_timeout_ms(), timeout_ms)

        fast_first_wait_until = (fast_first_wait_until or _pdf_fast_first_wait_until()).strip().lower()
        if fast_first_wait_until not in {"load", "domcontentloaded", "networkidle"}:
            fast_first_wait_until = _pdf_fast_first_wait_until()
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
            sem_acquired = False
            try:
                if acquire_timeout_ms is None:
                    await _PDF_RENDER_SEM.acquire()
                    sem_acquired = True
                else:
                    acquire_s = max(0.001, min(float(acquire_timeout_ms) / 1000.0, 120.0))
                    try:
                        await asyncio.wait_for(_PDF_RENDER_SEM.acquire(), timeout=acquire_s)
                    except asyncio.TimeoutError as exc:
                        raise PdfBusyError("pdf_queue_timeout") from exc
                    sem_acquired = True

                page = await _acquire_page()
                try:
                    await _ensure_page_configured(page)
                    if url:
                        if fast_first:
                            try:
                                await page.goto(url, wait_until=fast_first_wait_until, timeout=fast_first_timeout_ms)
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
                            base_href = _compute_base_href(base_url)
                            clean = re.sub(
                                r"(?i)<head([^>]*)>",
                                rf"<head\1><base href='{base_href}'>",
                                clean,
                                count=1,
                            )
                        if fast_first:
                            try:
                                await page.set_content(clean, wait_until=fast_first_wait_until, timeout=fast_first_timeout_ms)
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
            finally:
                if sem_acquired:
                    try:
                        _PDF_RENDER_SEM.release()
                    except Exception:
                        pass
    except PdfBusyError:
        # Don't reset Chromium for load shedding; let caller decide how to report.
        raise
    except Exception as e:
        await _reset_browser()
        with open("pdf_errors.log", "a", encoding="utf-8") as f:
            f.write(f"Runtime Error: {repr(e)}\n")
            f.write(traceback.format_exc() + "\n")
        return None


async def fetch_page_html_chromium(
    url: str,
    *,
    wait_until: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    acquire_timeout_ms: Optional[int] = None,
) -> Optional[str]:
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

        wait_until = (wait_until or _get_pdf_wait_until()).strip().lower()
        if wait_until not in {"load", "domcontentloaded", "networkidle"}:
            wait_until = _get_pdf_wait_until()

        timeout_ms = int(timeout_ms) if timeout_ms is not None else _get_pdf_timeout_ms()
        timeout_ms = max(1_000, min(timeout_ms, 300_000))
        block_types = sorted(_get_pdf_block_resource_types())
        async with atimed(
            "pdf.chromium.fetch_html",
            has_url=True,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            block_types=",".join(block_types),
        ):
            sem_acquired = False
            try:
                if acquire_timeout_ms is None:
                    await _PDF_RENDER_SEM.acquire()
                    sem_acquired = True
                else:
                    acquire_s = max(0.001, min(float(acquire_timeout_ms) / 1000.0, 120.0))
                    try:
                        await asyncio.wait_for(_PDF_RENDER_SEM.acquire(), timeout=acquire_s)
                    except asyncio.TimeoutError as exc:
                        raise PdfBusyError("pdf_queue_timeout") from exc
                    sem_acquired = True

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
            finally:
                if sem_acquired:
                    try:
                        _PDF_RENDER_SEM.release()
                    except Exception:
                        pass
    except PdfBusyError:
        raise
    except Exception:
        return None
