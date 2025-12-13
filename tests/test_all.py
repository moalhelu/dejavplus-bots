import re

from bot_core import bridge
from bot_core.services.translation import inject_rtl
from bot_core.services import reports


def test_t_sorani_falls_back_to_kurdish_arabic_script():
    rendered = bridge.t("menu.header", "ckb")
    # Expect Arabic-script characters to avoid Latin leakage
    assert any("\u0600" <= ch <= "\u06FF" for ch in rendered), rendered


def test_inject_rtl_marks_sorani_html():
    html = inject_rtl("<body>hello</body>", lang="ckb")
    assert "dir=\"rtl\"" in html.lower() or "dir='rtl'" in html.lower()


def test_reports_need_translation_for_sorani():
    assert reports._needs_translation("ckb") is True  # type: ignore[attr-defined]
