import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _repo_root() -> Path:
    return _ROOT


def _load_sample_html() -> str:
    html_path = _repo_root() / "tests" / "sample.html"
    return html_path.read_text(encoding="utf-8")


_PLACEHOLDER_PHRASE = "This is a fast/light PDF"


async def _render_one(i: int, html_template: str) -> bytes:
    from bot_core.services.pdf import html_to_pdf_bytes_chromium

    html = html_template.replace("{{I}}", str(i)).replace("{{TS}}", str(int(time.time())))
    pdf_bytes = await html_to_pdf_bytes_chromium(
        html_str=html,
        base_url="file:///",  # no external assets
        timeout_ms=int(os.getenv("PDF_SELFCHECK_TIMEOUT_MS", "60000")),
        acquire_timeout_ms=int(os.getenv("PDF_SELFCHECK_ACQUIRE_TIMEOUT_MS", "20000")),
    )
    if not pdf_bytes or len(pdf_bytes) < 500 or not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError(f"render produced invalid PDF bytes (len={0 if not pdf_bytes else len(pdf_bytes)})")
    return pdf_bytes


def _bytes_contain_token(pdf_bytes: bytes, token: str) -> bool:
    if not token:
        return False
    try:
        return token.encode("utf-8") in pdf_bytes
    except Exception:
        return False


async def _check_fast_report(vin: str, lang: str) -> None:
    from bot_core.services import reports

    res = await reports.generate_vin_report(vin, language=lang, fast_mode=True)
    if not res or not res.success or not res.pdf_bytes:
        raise RuntimeError(f"FAST report failed (success={getattr(res,'success',None)} errors={getattr(res,'errors',None)})")

    pdf_bytes = bytes(res.pdf_bytes)

    # Must contain VIN somewhere (basic sanity).
    if not _bytes_contain_token(pdf_bytes, vin):
        raise RuntimeError("FAST PDF does not contain VIN token (possible wrong/blank content)")

    # Must never contain placeholder phrase.
    if _bytes_contain_token(pdf_bytes, _PLACEHOLDER_PHRASE):
        raise RuntimeError("PLACEHOLDER detected in FAST PDF output")


async def _main() -> int:
    # Mode switch:
    # - Default: stress-test pdf engine
    # - report <VIN> [LANG]: generate FAST VIN report and assert no placeholder
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "report":
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python tools/pdf_selfcheck.py report <VIN> [LANG]")
        vin = sys.argv[2].strip()
        lang = (sys.argv[3].strip() if len(sys.argv) >= 4 else os.getenv("SELFCHECK_LANG", "en")).strip() or "en"
        await _check_fast_report(vin, lang)
        print("FAST report self-check OK")
        print(f"- vin: {vin}")
        print(f"- lang: {lang}")
        return 0

    sequential_n = int(os.getenv("PDF_SELFCHECK_SEQUENTIAL", "20"))
    concurrent_n = int(os.getenv("PDF_SELFCHECK_CONCURRENT", "10"))

    html_template = _load_sample_html()

    # Optional best-effort process counting (only if psutil is installed).
    proc_before = None
    proc_after_render = None
    proc_after_close = None
    try:
        from bot_core.services.pdf import _chromium_process_count_best_effort  # type: ignore

        proc_before = _chromium_process_count_best_effort()
    except Exception:
        proc_before = None

    t0 = time.perf_counter()

    for i in range(sequential_n):
        await _render_one(i, html_template)

    # Burst of concurrent renders (bounded by PDF_PAGE_MAX in pdf.py).
    await asyncio.gather(*[_render_one(1000 + i, html_template) for i in range(concurrent_n)])

    elapsed = time.perf_counter() - t0

    try:
        from bot_core.services.pdf import _chromium_process_count_best_effort  # type: ignore

        proc_after_render = _chromium_process_count_best_effort()
    except Exception:
        proc_after_render = None

    # Ensure the engine can be cleanly shut down.
    try:
        from bot_core.services.pdf import close_pdf_engine

        await close_pdf_engine()
        await asyncio.sleep(0.25)
    except Exception:
        pass

    try:
        from bot_core.services.pdf import _chromium_process_count_best_effort  # type: ignore

        proc_after_close = _chromium_process_count_best_effort()
    except Exception:
        proc_after_close = None

    print("PDF self-check OK")
    print(f"- sequential: {sequential_n}")
    print(f"- concurrent: {concurrent_n}")
    print(f"- elapsed_s: {elapsed:.2f}")
    if proc_before is not None or proc_after_render is not None or proc_after_close is not None:
        print(f"- chromium_proc_count_before: {proc_before}")
        print(f"- chromium_proc_count_after_render: {proc_after_render}")
        print(f"- chromium_proc_count_after_close: {proc_after_close}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
