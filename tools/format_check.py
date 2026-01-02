import sys
from pathlib import Path


def _main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/format_check.py <pdf_path>")
        return 2

    pdf_path = Path(sys.argv[1]).resolve()
    if not pdf_path.exists():
        print("FAIL: file_not_found")
        return 2

    from bot_core.utils.pdf_format import validate_pdf_format

    data = pdf_path.read_bytes()
    res = validate_pdf_format(data, require_official_tokens=True)
    if res.ok:
        print("PASS")
        return 0

    print(f"FAIL: {res.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
