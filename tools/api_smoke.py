"""Smoke test for upstream Carfax API URL + Authorization.

Usage:
  py tools/api_smoke.py 4T1BE46K59U853103
  py tools/api_smoke.py 4T1BE46K59U853103 1HGCM82633A004352

It uses the same env loader as the apps (bot_core.config.get_env) and the same
canonical URL builder + token normalization in bot_core.services.reports.

This tool NEVER prints the full token.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Ensure repo root is importable when running `py tools\api_smoke.py ...`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from bot_core.config import get_env
from bot_core.services.reports import (
    build_carfax_url,
    normalize_token,
    token_sanity,
)


def _extract_html_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if isinstance(data, dict):
        val = data.get("htmlContent")
        if isinstance(val, str):
            return val
    val = payload.get("htmlContent")
    if isinstance(val, str):
        return val
    return ""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _fetch_one(vin: str) -> Dict[str, Any]:
    import aiohttp

    cfg = get_env()
    raw_token = cfg.api_token
    clean_token = normalize_token(raw_token)
    sanity = token_sanity(raw_token)

    url = build_carfax_url(vin)

    headers = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept": "application/pdf, application/json;q=0.9, text/html;q=0.8, */*;q=0.5",
    }
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"

    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=8)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                status = int(resp.status)
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body = await resp.read()
                t1 = time.perf_counter()

                result: Dict[str, Any] = {
                    "vin": vin,
                    "url": url,
                    "final_url": str(getattr(resp, "url", "")) or "",
                    "status": status,
                    "content_type": ctype,
                    "bytes_len": len(body) if body else 0,
                    "elapsed_sec": round(t1 - t0, 3),
                    "token_sanity": sanity,
                    "url_carfax_segment_count": url.count("/carfax/"),
                }

                if body:
                    result["sha256"] = _sha256_hex(body)

                if "application/pdf" in ctype and body:
                    result["path"] = "pdf"
                elif status in (200, 201) and ("application/json" in ctype or (body[:1] == b"{")):
                    result["path"] = f"json_{status}"
                    try:
                        payload = body.decode("utf-8", errors="ignore")
                        data = __import__("json").loads(payload or "{}")
                        html = _extract_html_from_payload(data)
                        result["html_bytes_len"] = len(html.encode("utf-8", errors="ignore")) if html else 0
                    except Exception:
                        result["html_bytes_len"] = 0
                    try:
                        result["body_preview"] = body[:200].decode("utf-8", errors="ignore")
                    except Exception:
                        result["body_preview"] = ""
                elif status in (200, 201):
                    result["path"] = f"non_pdf_{status}"
                    try:
                        result["body_preview"] = body[:200].decode("utf-8", errors="ignore")
                    except Exception:
                        result["body_preview"] = ""
                else:
                    result["path"] = f"non_success_{status}"
                    try:
                        result["body_preview"] = body[:200].decode("utf-8", errors="ignore")
                    except Exception:
                        result["body_preview"] = ""

                return result
        except Exception as exc:
            return {
                "vin": vin,
                "url": url,
                "error": str(exc),
                "token_sanity": sanity,
                "url_carfax_segment_count": url.count("/carfax/"),
            }


def _print_result(r: Dict[str, Any]) -> None:
    print("-")
    print(f"VIN: {r.get('vin')}")
    print(f"URL: {r.get('url')}")
    if r.get("final_url"):
        print(f"Final URL: {r.get('final_url')}")
    print(f"/carfax/ count: {r.get('url_carfax_segment_count')}")

    sanity = r.get("token_sanity") or {}
    print(
        "Token sanity: "
        f"len={sanity.get('token_len')} dot_parts={sanity.get('dot_parts')} "
        f"head5={sanity.get('head5')} tail5={sanity.get('tail5')} "
        f"has_space={sanity.get('has_space')} has_bearer={sanity.get('has_bearer')}"
    )

    if r.get("error"):
        print(f"ERROR: {r.get('error')}")
        return

    print(f"Status: {r.get('status')}")
    print(f"Content-Type: {r.get('content_type')}")
    print(f"Bytes: {r.get('bytes_len')}")
    print(f"Elapsed: {r.get('elapsed_sec')}s")
    if r.get("sha256"):
        print(f"SHA256: {r.get('sha256')}")
    print(f"Path: {r.get('path')}")

    if r.get("html_bytes_len") is not None:
        print(f"HTML bytes: {r.get('html_bytes_len')}")

    if r.get("body_preview"):
        print("Body preview:")
        print(r.get("body_preview"))


async def main(argv: List[str]) -> int:
    # Match app behavior: load .env if present.
    load_dotenv(override=False)

    if len(argv) < 2:
        print("Usage: py tools/api_smoke.py <VIN> [VIN2 ...]")
        return 2

    vins = [a.strip() for a in argv[1:] if a.strip()]
    # Quick visibility into which API_URL is set, but canonical base is enforced.
    cfg = get_env()
    print(f"API_URL env: {cfg.api_url or ''}")

    results = await asyncio.gather(*[_fetch_one(v) for v in vins])
    for r in results:
        _print_result(r)

    # Exit non-zero if any URL is malformed or any request errors.
    bad = 0
    for r in results:
        if r.get("url_carfax_segment_count") != 1:
            bad += 1
        if r.get("error"):
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
