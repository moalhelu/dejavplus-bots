"""PDF generation helpers (WeasyPrint / Playwright)."""
from __future__ import annotations

import os
import asyncio
import re
from typing import Optional, List

from bot_core.telemetry import atimed

_PDF_PLAYWRIGHT = None
_PDF_BROWSER = None
_PDF_BROWSER_LOCK = asyncio.Lock()
_PDF_RENDER_SEM = asyncio.Semaphore(8)
_PDF_PAGE_POOL: List[object] = []
_PDF_PAGE_LOCK = asyncio.Lock()
_PDF_PAGE_MAX = 8
_PDF_PREWARM_ENABLED = os.getenv("ENABLE_PDF_PREWARM", "1").lower() not in {"0", "false", "off"}


def html_to_pdf_bytes_weasyprint(html_str: str) -> Optional[bytes]:
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except Exception:
        return None
    try:
        css = CSS(string="@page { size: A4; margin: 10mm; } body{font-family:Arial,Helvetica,sans-serif}")
        return HTML(string=html_str, base_url=".").write_pdf(stylesheets=[css])
    except Exception:
        return None


async def html_to_pdf_weasyprint_async(html_str: str) -> Optional[bytes]:
    async with atimed("pdf.weasyprint", html_len=len(html_str) if html_str else 0):
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
        async with _PDF_RENDER_SEM:
            async with atimed(
                "pdf.chromium",
                mode="url" if url else "html",
                html_len=len(html_str) if html_str else 0,
            ):
                page = await _acquire_page()
                try:
                    if url:
                        await page.goto(url, wait_until="networkidle", timeout=60000)
                    elif html_str:
                        clean = re.sub(r"<script\b[^>]*>.*?</script>", "", html_str, flags=re.I | re.S)
                        if "<head" in clean.lower() and "<base" not in clean.lower():
                            clean = re.sub(r"(?i)<head([^>]*)>", r"<head\1><base href='https://www.carfax.com/'>", clean, count=1)
                        await page.set_content(clean, wait_until="networkidle", timeout=60000)
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
