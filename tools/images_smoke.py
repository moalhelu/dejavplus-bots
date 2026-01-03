"""Smoke test for vehicle image providers.

Usage:
  python tools/images_smoke.py <VIN>

What it does:
- Reads env (.env) via bot_core.config.get_env()
- Fetches accident image list (ApiCar)
- Fetches hidden image list (BadVin -> ApiCar fallback)
- For each, tries to download the first image and prints evidence:
  endpoint, status, content-type, bytes_len, and first 200 chars if not image

This script is intentionally verbose and evidence-driven for production debugging.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, Optional, Tuple

import httpx

from bot_core.config import get_env
from bot_core.services.images import (
    get_apicar_accident_images,
    get_hidden_vehicle_images,
)


def _redact(s: str) -> str:
    # Avoid leaking secrets in logs.
    if not s:
        return s
    return s.replace(get_env().apicar_api_key or "", "<REDACTED>")


async def _fetch_evidence(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 25.0,
) -> Tuple[int, str, int, str, str]:
    """Return (status, content_type, bytes_len, final_url, preview_text)."""

    async with client.stream("GET", url, headers=headers or {}, timeout=timeout, follow_redirects=True) as resp:
        status = int(resp.status_code)
        ctype = str(resp.headers.get("content-type", ""))
        final_url = str(getattr(resp, "url", url))
        data = await resp.aread()
        preview = ""
        if not (ctype or "").lower().startswith("image/"):
            try:
                preview = (data[:200] or b"").decode("utf-8", errors="replace")
            except Exception:
                preview = repr(data[:200])
        return status, ctype, len(data or b""), final_url, preview


def _print_block(title: str, lines: list[str]) -> None:
    print("\n" + ("=" * 70))
    print(title)
    print("=" * 70)
    for line in lines:
        print(line)


async def main() -> int:
    vin = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not vin:
        vin = (os.getenv("VIN") or "").strip()
    if not vin:
        print("Usage: python tools/images_smoke.py <VIN>")
        return 2

    cfg = get_env()
    rid = f"smoke-{vin}"

    _print_block(
        "ENV",
        [
            f"APICAR_API_BASE: {cfg.apicar_base_url}",
            f"APICAR_API_KEY set: {bool(cfg.apicar_api_key)}",
            f"BADVIN_EMAIL set: {bool(cfg.badvin_email)}",
            f"BADVIN_PASSWORD set: {bool(cfg.badvin_password)}",
            f"APICAR_API_TIMEOUT: {cfg.apicar_timeout}",
            f"APICAR_IMAGE_TIMEOUT: {cfg.apicar_image_timeout}",
        ],
    )

    # Fetch URL lists through production code paths.
    accident_urls = await get_apicar_accident_images(vin, rid=rid)
    hidden_urls = await get_hidden_vehicle_images(vin, rid=rid)

    _print_block(
        "LIST RESULTS",
        [
            f"VIN: {vin}",
            f"accident_urls: {len(accident_urls)}",
            f"hidden_urls: {len(hidden_urls)}",
            f"accident_first: {accident_urls[0] if accident_urls else '-'}",
            f"hidden_first: {hidden_urls[0] if hidden_urls else '-'}",
        ],
    )

    headers = {
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (compatible; DejavuPlusBot/1.0)",
    }

    async with httpx.AsyncClient() as client:
        # Evidence: try downloading first accident image.
        if accident_urls:
            url = accident_urls[0]
            # If it's an ApiCar host, include api-key to validate auth requirement.
            if cfg.apicar_api_key and cfg.apicar_base_url and (httpx.URL(url).host == httpx.URL(cfg.apicar_base_url).host):
                headers_with_key = dict(headers)
                headers_with_key["api-key"] = cfg.apicar_api_key
            else:
                headers_with_key = headers

            status, ctype, blen, final_url, preview = await _fetch_evidence(
                client, url, headers=headers_with_key, timeout=float(cfg.apicar_image_timeout or 25.0)
            )
            _print_block(
                "ACCIDENT FIRST IMAGE DOWNLOAD",
                [
                    f"url: {_redact(url)}",
                    f"final_url: {_redact(final_url)}",
                    f"status: {status}",
                    f"content_type: {ctype}",
                    f"bytes_len: {blen}",
                    f"preview_200: {preview if preview else '-'}",
                ],
            )
        else:
            _print_block("ACCIDENT FIRST IMAGE DOWNLOAD", ["no urls"]) 

        if hidden_urls:
            url = hidden_urls[0]
            if cfg.apicar_api_key and cfg.apicar_base_url and (httpx.URL(url).host == httpx.URL(cfg.apicar_base_url).host):
                headers_with_key = dict(headers)
                headers_with_key["api-key"] = cfg.apicar_api_key
            else:
                headers_with_key = headers

            status, ctype, blen, final_url, preview = await _fetch_evidence(
                client, url, headers=headers_with_key, timeout=float(cfg.apicar_image_timeout or 25.0)
            )
            _print_block(
                "HIDDEN FIRST IMAGE DOWNLOAD",
                [
                    f"url: {_redact(url)}",
                    f"final_url: {_redact(final_url)}",
                    f"status: {status}",
                    f"content_type: {ctype}",
                    f"bytes_len: {blen}",
                    f"preview_200: {preview if preview else '-'}",
                ],
            )
        else:
            _print_block("HIDDEN FIRST IMAGE DOWNLOAD", ["no urls"]) 

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
