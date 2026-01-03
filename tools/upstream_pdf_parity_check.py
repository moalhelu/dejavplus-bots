"""Upstream PDF parity check tool.

Purpose:
- Proves the FINAL SPEC invariant: if upstream returns a PDF, the bot delivers
  the exact same bytes (sha256 match).

Example:
- Set API token (same as production):
    set API_TOKEN=...  (or use your .env)

- Run:
    python tools/upstream_pdf_parity_check.py --vin 1HGCM82633A004352 --lang ar

Expected output (shape):
    VIN=1HGCM82633A004352 lang=ar
    upstream: ok=True status=200 ctype=application/pdf... bytes=123456 sha256=...
    bot:      success=True bytes=123456 upstream_sha256=... delivered_sha256=...
    MATCH=True

Exit codes:
- 0: parity OK
- 2: parity mismatch
- 3: upstream didn't return a PDF (per Content-Type)
- 4: bot failed to produce a PDF
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is importable when running `py tools\upstream_pdf_parity_check.py ...`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import asyncio
import hashlib
import sys
from typing import Optional


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _run(vin: str, lang: str) -> int:
    from bot_core.services.reports import fetch_upstream_pdf, generate_vin_report

    upstream = await fetch_upstream_pdf(vin, force_fresh=True)
    upstream_ok = bool(upstream.get("ok"))
    upstream_status = upstream.get("status")
    upstream_ctype = (upstream.get("ctype") or "").lower()
    upstream_bytes = upstream.get("pdf_bytes")
    upstream_sha = upstream.get("sha256")

    print(f"VIN={vin} lang={lang}")
    print(
        "upstream: "
        f"ok={upstream_ok} status={upstream_status} ctype={upstream_ctype or '-'} "
        f"bytes={(len(upstream_bytes) if isinstance(upstream_bytes, (bytes, bytearray)) else 0)} "
        f"sha256={(upstream_sha or '-') }"
    )

    if not upstream_ok or ("application/pdf" not in upstream_ctype) or not isinstance(upstream_bytes, (bytes, bytearray)) or not bytes(upstream_bytes):
        return 3

    result = await generate_vin_report(vin, language=lang, fast_mode=True, user_id="tool")
    if not getattr(result, "success", False) or not getattr(result, "pdf_bytes", None):
        print(f"bot:      success={getattr(result, 'success', None)} bytes=0")
        return 4

    delivered_bytes: bytes = bytes(result.pdf_bytes)
    delivered_sha = _sha256(delivered_bytes)
    bot_upstream_sha: Optional[str] = getattr(result, "upstream_sha256", None)

    print(
        "bot:      "
        f"success={bool(result.success)} bytes={len(delivered_bytes)} "
        f"upstream_sha256={(bot_upstream_sha or '-') } "
        f"delivered_sha256={delivered_sha}"
    )

    # Primary check: upstream sha reported by fetch_upstream_pdf equals delivered sha.
    # Secondary check: ReportResult.upstream_sha256 equals delivered sha.
    match_primary = bool(upstream_sha) and (str(upstream_sha) == delivered_sha)
    match_secondary = (not bot_upstream_sha) or (bot_upstream_sha == delivered_sha)
    match = bool(match_primary and match_secondary)

    print(f"MATCH={match}")

    return 0 if match else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vin", required=True, help="VIN to fetch")
    parser.add_argument("--lang", default="en", help="Requested language (does not change PDF bytes)")
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args.vin, (args.lang or "en").strip().lower()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
