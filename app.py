# type: ignore
# pyright: reportGeneralTypeIssues=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportPrivateUsage=false, reportConstantRedefinition=false, reportUnusedImport=false, reportUnusedFunction=false, reportUnnecessaryIsInstance=false, reportDeprecated=false
# -*- coding: utf-8 -*-
import asyncio
import sys

import copy
import json
import logging
import hashlib

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from bot_core.logging_setup import configure_logging

# Centralized, share-friendly logs (set LOG_PRESET=verbose to restore noisy debug).
configure_logging()

import mimetypes
import os
import re
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


logger = logging.getLogger(__name__)

from bot_core.auth import (
    env_super_admins as _env_super_admins,
    db_super_admins as _db_super_admins,
    is_super_admin as _is_super_admin,
    is_admin_tg as _is_admin_tg,
    is_ultimate_super as _is_ultimate_super,
    reload_env as _reload_env,
)
from bot_core.config import get_env, get_report_default_lang
from bot_core import bridge as _bridge
from bot_core.bridge import CAPABILITIES_PATTERNS as _CAPABILITIES_PATTERNS
from bot_core.storage import (
    load_db as _load_db,
    save_db as _save_db,
    ensure_user as _ensure_user,
    fmt_date as _fmt_date,
    display_name as _display_name,
    days_left as _days_left,
    format_tg_with_phone as _fmt_tg_with_phone,
    audit as _audit,
    bump_usage as _bump_usage,
    now_str as _now_str,
    remaining_monthly_reports as _remaining_monthly_reports,
    reserve_credit as _reserve_credit,
    refund_credit as _refund_credit,
    commit_credit as _commit_credit,
)
from bot_core.request_id import compute_request_id
from bot_core.services.translation import (
    inject_rtl as _inject_rtl,
    translate_html as _translate_html,
    close_http_session as _close_translation_session,
)
from bot_core.services.pdf import (
    html_to_pdf_bytes_chromium as _html_to_pdf_bytes_chromium,
)
from bot_core.services.notifications import (
    check_and_send_auto_notifications,
    notify_supers as _notify_supers,
    notify_user as _notify_user,
)
from bot_core.services.reports import (
    ReportResult as _ReportResult,
    close_http_session as _close_reports_session,
    generate_vin_report as _generate_vin_report,
)
from bot_core.utils.vin import (
    VIN_RE,
    normalize_vin as _norm_vin,
    make_progress_bar as _make_progress_bar,
)

REPORT_LANG_INFO = {
    "ar": {"label": "🌐 العربية", "name": "العربية"},
    "en": {"label": "🌐 English", "name": "English"},
    "ku": {"label": "🌐 کوردی بادینی", "name": "Kurdish Badini"},
    "ckb": {"label": "🌐 کوردی سۆرانی", "name": "Kurdish Sorani"},
}
REPORT_LANG_CODES = tuple(REPORT_LANG_INFO.keys())
LANG_BUTTON_TEXTS = tuple(info["label"] for info in REPORT_LANG_INFO.values())
RTL_REPORT_LANGS = {"ar", "ku", "ckb"}


WA_POST_ACTIVATION_NOTICE_V1 = (
    "📢 إشعار من الإدارة\n\n"
    "🔔 تنويه لمستخدمي بوت واتساب\n\n"
    "لتغيير لغة التقارير داخل البوت،\n"
    "يرجى إرسال رقم 3 الى البوت\n"
    "ثم رقم اللغة فقط بدون أي نص إضافي،\n"
    "وكل رقم يكون برسالة منفصلة.\n\n"
    "🌐 اللغات المتاحة:\n"
    "1️⃣ عربي\n"
    "2️⃣ انجليزي\n"
    "3️⃣ كوردی\n\n"
    "⚠️ ملاحظة مهمة:\n"
    "أرسل رقم واحد فقط\n"
    "بدون كتابة أي كلام\n"
    "برسالة مستقلة"
)


TG_POST_ACTIVATION_NOTICE_V1 = (
    "📢 <b>إشعار من الإدارة</b>\n\n"
    "🔔 تنويه لمستخدمي بوت تيليغرام\n\n"
    "لتغيير لغة التقارير داخل البوت:\n"
    "افتح القائمة الرئيسية ثم اضغط على زر <b>🌐 اللغة</b>، وبعدها اختر اللغة.\n\n"
    "🌐 اللغات المتاحة:\n"
    "• عربي\n"
    "• English\n"
    "• كوردی"
)


def _activation_request_info(db: Dict[str, Any], target_tg: str, user: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    phone = None
    platform = None
    try:
        for req in db.get("activation_requests", []) or []:
            if str(req.get("tg_id")) == str(target_tg):
                phone = req.get("phone") or None
                platform = req.get("platform") or None
                break
    except Exception:
        pass
    if not phone and isinstance(user, dict):
        phone = user.get("phone") or None
    return phone, platform


def _is_probable_whatsapp_user(*, target_tg: str, user: Dict[str, Any], platform: Optional[str], phone: Optional[str]) -> bool:
    if (platform or "").strip().lower() == "whatsapp":
        return True
    if not phone:
        return False
    phone_digits = str(phone).strip().lstrip("+")
    # WhatsApp-only users use tg_id == phone digits (country code + number).
    if str(target_tg).strip() == phone_digits and len(phone_digits) > 10 and not (user.get("tg_username") or "").strip():
        return True
    return False


async def _post_activation_admin_notice_if_needed(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    db: Dict[str, Any],
    user: Dict[str, Any],
    target_tg: str,
    first_activation: bool,
    is_whatsapp: bool,
) -> None:
    if not first_activation:
        return
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    notices = user.setdefault("notices", {})

    if is_whatsapp:
        if notices.get("wa_post_activation_notice_v1"):
            return
        ok = await _notify_user(context, target_tg, WA_POST_ACTIVATION_NOTICE_V1, preferred_channel="wa")
        if ok:
            notices["wa_post_activation_notice_v1"] = today_str
            _save_db(db)
        return

    if notices.get("tg_post_activation_notice_v1"):
        return
    ok = await _notify_user(context, target_tg, TG_POST_ACTIVATION_NOTICE_V1, preferred_channel="tg")
    if ok:
        notices["tg_post_activation_notice_v1"] = today_str
        _save_db(db)


def _is_upstream_unauthorized_result(rr: Optional["_ReportResult"]) -> bool:
    if not rr:
        return False
    try:
        errors = [str(e).lower() for e in (getattr(rr, "errors", None) or [])]
    except Exception:
        errors = []
    if any("invalid_token" in e for e in errors):
        return True
    if any(e.startswith("http_401") or e.startswith("http_403") for e in errors):
        return True
    try:
        raw = getattr(rr, "raw_response", None)
        if isinstance(raw, dict):
            status = raw.get("status")
            if int(status) in (401, 403):
                return True
    except Exception:
        pass
    return False


def _is_retryable_report_failure(rr: Optional["_ReportResult"]) -> bool:
    if not rr:
        return True
    if getattr(rr, "success", False):
        return False
    if _is_upstream_unauthorized_result(rr):
        return False
    try:
        errors = [str(e).lower() for e in (getattr(rr, "errors", None) or [])]
    except Exception:
        errors = []
    if any("invalid_vin" in e for e in errors):
        return False
    transient_markers = (
        "timeout",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "non_pdf_upstream",
        "exception",
    )
    return any(m in e for e in errors for m in transient_markers) or not errors


def _report_failure_user_message(rr: Optional["_ReportResult"], lang: str) -> str:
    if not rr:
        return _bridge.t("report.error.generic", lang)
    if _is_upstream_unauthorized_result(rr):
        return _bridge.t("report.error.fetch", lang)
    try:
        errors = [str(e).lower() for e in (getattr(rr, "errors", None) or [])]
    except Exception:
        errors = []
    if any("timeout" in e for e in errors):
        return _bridge.t("report.error.timeout", lang)
    user_msg = str(getattr(rr, "user_message", None) or "").strip()
    return user_msg or _bridge.t("report.error.fetch", lang)

def _menu_label(lang: str, key: str) -> str:
    lang_code = (lang or "ar").lower()
    return _bridge.t(f"menu.{key}.label", lang_code)


# === Unified button labels (old + new) to avoid VIN warning on button press ===
ALL_BUTTON_LABELS = set(LANG_BUTTON_TEXTS)
for lang in ("ar", "en", "ku", "ckb"):
    ALL_BUTTON_LABELS.update({
        _menu_label(lang, "report"),
        _menu_label(lang, "profile"),
        _menu_label(lang, "balance"),
        _menu_label(lang, "activation"),
        _menu_label(lang, "help"),
        _menu_label(lang, "language"),
        _menu_label(lang, "users"),
        _menu_label(lang, "stats"),
        _menu_label(lang, "pending"),
        _menu_label(lang, "settings"),
        _menu_label(lang, "notifications"),
        _bridge.t("menu.header", lang),
        _bridge.t("action.back", lang),
        _bridge.t("action.cancel", lang),
    })

MENU_TEXT_TO_ID: Dict[str, str] = {}
for lang in ("ar", "en", "ku", "ckb"):
    MENU_TEXT_TO_ID[_menu_label(lang, "report")] = "report"
    MENU_TEXT_TO_ID[_menu_label(lang, "profile")] = "profile"
    MENU_TEXT_TO_ID[_menu_label(lang, "balance")] = "balance"
    MENU_TEXT_TO_ID[_menu_label(lang, "activation")] = "activation"
    MENU_TEXT_TO_ID[_menu_label(lang, "help")] = "help"
    MENU_TEXT_TO_ID[_menu_label(lang, "language")] = "language"

USERS_PANEL_TEXT = _bridge.t("users.panel.header", None)

SUBSCRIPTION_PLANS = {"monthly", "trial", "custom"}

DEFAULT_ACTIVATION_PRESETS = {
    "trial": {"days": 1, "daily": 25, "monthly": 25},
    "monthly": {"days": 30, "daily": 25, "monthly": 500},
}


def _resolve_activation_preset(db: Optional[Dict[str, Any]], key: str) -> Dict[str, int]:
    if key not in DEFAULT_ACTIVATION_PRESETS:
        raise KeyError(f"Unknown activation preset: {key}")
    if db is None:
        db = _load_db()
    store = db.setdefault("activation_presets", {})
    record = store.get(key) or {}
    resolved = {}
    for field, default_value in DEFAULT_ACTIVATION_PRESETS[key].items():
        try:
            resolved[field] = max(1, int(record.get(field, default_value)))
        except (TypeError, ValueError):
            resolved[field] = max(1, default_value)
    return resolved


def _set_activation_preset(db: Dict[str, Any], key: str, *, days: int, daily: int, monthly: int) -> None:
    if key not in DEFAULT_ACTIVATION_PRESETS:
        raise KeyError(f"Unknown activation preset: {key}")
    store = db.setdefault("activation_presets", {})
    store[key] = {
        "days": max(1, int(days)),
        "daily": max(1, int(daily)),
        "monthly": max(1, int(monthly)),
    }
    _save_db(db)


def _activation_button_label(title: str, preset: Dict[str, int]) -> str:
    return _bridge.t(
        "activation.preset.label",
        None,
        title=title,
        days=preset.get("days", ""),
        daily=preset.get("daily", ""),
        monthly=preset.get("monthly", ""),
    )


def _plan_code(u: Dict[str, Any]) -> str:
    return str(u.get("plan") or "").strip().lower()


def _current_balance(u: Dict[str, Any]) -> int:
    credit = _remaining_monthly_reports(u)
    return int(credit) if credit is not None else 0


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# =================== Environment Variables ===================
DB_PATH  = os.getenv("DB_PATH", "db.json").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_URL   = os.getenv("API_URL", "").strip()
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# =================== Telegram ===================
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.constants import ParseMode

MAIN_MENU_TEXTS = tuple(_bridge.t("menu.header", lang) for lang in ("ar", "en", "ku", "ckb"))
MAIN_MENU_BUTTON_REGEX = r"^(?:" + "|".join(re.escape(x) for x in MAIN_MENU_TEXTS) + r")$"

# Open the main menu only when the user explicitly asks for it.
_MENU_SHOW_KEYWORDS_BASE = {"/menu", "menu", "main menu", "mainmenu", ".", "القائمة", "قائمة"}
MENU_SHOW_KEYWORDS = set(_MENU_SHOW_KEYWORDS_BASE)
for _lang in ("ar", "en", "ku", "ckb"):
    _header = _bridge.t("menu.header", _lang)
    if _header:
        MENU_SHOW_KEYWORDS.add((_header or "").strip().lower())
        MENU_SHOW_KEYWORDS.add((_header or "").replace("🏠", "").strip().lower())


def _menu_hint_text(lang: Optional[str]) -> str:
    lang_code = _normalize_report_lang_code(lang)
    if lang_code == "ar":
        return "أرسل نقطة (.) لفتح القائمة الرئيسية أو أرسل رقم VIN."
    return "Send '.' to open the main menu, or send a VIN."


def _main_menu_button_text(lang: Optional[str]) -> str:
    return _bridge.t("menu.header", _normalize_report_lang_code(lang))


_MAIN_MENU_HEADER_FALLBACK = {
    "ar": "🏠 القائمة الرئيسية",
    "en": "🏠 Main Menu",
    "ku": "🏠 لیستەی سەرەکی",
    "ckb": "🏠 لیستەی سەرەکی",
}

_MAIN_MENU_PROMPT_FALLBACK = {
    "ar": "اختر أحد الأزرار أدناه للمتابعة.",
    "en": "Choose one of the buttons below to continue.",
    "ku": "یەکێک لە دوگمەکان خوارەوە هەڵبژێرە بۆ بەردەوامبوون.",
    "ckb": "یەکێک لە دوگمەکان خوارەوە هەڵبژێرە بۆ بەردەوامبوون.",
}


def _resolve_lang_for_tg(tg_id: Optional[str], fallback: Optional[str] = None) -> str:
    lang_candidate = _normalize_report_lang_code(fallback or get_report_default_lang() or "ar")
    try:
        if tg_id and _is_super_admin(str(tg_id)):
            return "ar"
        if tg_id:
            db = _load_db()
            user = db.get("users", {}).get(str(tg_id)) or {}
            lang_candidate = _get_user_report_lang(user)
    except Exception:
        pass
    return _normalize_report_lang_code(lang_candidate)


def _main_menu_prompt_text(lang: Optional[str]) -> str:
    lang_code = _normalize_report_lang_code(lang)
    header = _bridge.t("menu.header", lang_code, default=_MAIN_MENU_HEADER_FALLBACK.get(lang_code, _MAIN_MENU_HEADER_FALLBACK["en"]))
    prompt = _bridge.t("menu.telegram.prompt", lang_code, default=_MAIN_MENU_PROMPT_FALLBACK.get(lang_code, _MAIN_MENU_PROMPT_FALLBACK["en"]))
    return f"{header}\n{prompt}"


def _normalize_report_lang_code(lang: Optional[str]) -> str:
    candidate = (lang or "").strip().lower()
    if candidate in REPORT_LANG_INFO:
        return candidate
    # Accept system language tags like ar-IQ, en-US, ckb-IQ.
    try:
        primary = re.split(r"[-_]", candidate, maxsplit=1)[0]
    except Exception:
        primary = ""
    if primary in REPORT_LANG_INFO:
        return primary
    default_candidate = (get_report_default_lang() or "ar").strip().lower()
    return default_candidate if default_candidate in REPORT_LANG_INFO else "ar"


def _lang_label(lang: str) -> str:
    return REPORT_LANG_INFO.get(lang, REPORT_LANG_INFO["en"])["label"]


def _lang_name(lang: str) -> str:
    return REPORT_LANG_INFO.get(lang, REPORT_LANG_INFO["en"])["name"]


def _unauthorized(lang: Optional[str] = None) -> str:
    return _bridge.t("common.unauthorized", _normalize_report_lang_code(lang))


def _language_choice_rows(current_lang: str, callback_builder: Callable[[str], str]) -> List[List[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            ("✅ " if current_lang == code else "") + _lang_label(code),
            callback_data=callback_builder(code),
        )
        for code in REPORT_LANG_CODES
    ]
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i + 2])
    return rows


def _build_bridge_user_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[_bridge.UserContext]:
    user = update.effective_user if update else None
    if not user:
        return None
    tg_id = str(user.id)
    db = _load_db()
    db_user = _ensure_user(db, tg_id, user.username)
    lang = "ar" if _is_super_admin(tg_id) else _get_user_report_lang(db_user)
    if db_user.get("language") != lang or db_user.get("report_lang") != lang:
        db_user["language"] = lang
        db_user["report_lang"] = lang
        _save_db(db)
    logger.debug("bridge_user_context language resolved", extra={"tg_id": tg_id, "lang": lang})
    await_state = context.user_data.get("await")
    state = await_state.get("op") if isinstance(await_state, dict) else None
    metadata = {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "language_code": getattr(user, "language_code", None),
        "user_data": dict(context.user_data),
        "db_user": db_user,
    }
    return _bridge.UserContext(
        user_id=tg_id,
        phone=db_user.get("phone"),
        language=lang,
        state=state,
        metadata=metadata,
    )


def _bridge_raw_payload(update: Update) -> Dict[str, Any]:
    if not update:
        return {}
    try:
        return update.to_dict()
    except Exception:
        return {"update_id": getattr(update, "update_id", None)}


def _build_telegram_media_fetcher(context: ContextTypes.DEFAULT_TYPE):
    async def _resolver(
        message: _bridge.IncomingMessage,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
        file_id = message.media_url
        if not context or not getattr(context, "bot", None) or not file_id:
            return None, filename, mime_type
        try:
            telegram_file = await context.bot.get_file(file_id)
            data = await telegram_file.download_as_bytearray()
            inferred_name = filename or Path(getattr(telegram_file, "file_path", "") or f"{file_id}.jpg").name
            inferred_mime = mime_type or mimetypes.guess_type(inferred_name)[0]
            return bytes(data), inferred_name, inferred_mime
        except Exception:
            logging.exception("Failed to fetch Telegram media file")
            return None, filename, mime_type

    return _resolver


async def _send_bridge_responses(
    update: Update,
    responses,
    context: Optional["ContextTypes.DEFAULT_TYPE"] = None,
) -> None:
    if not responses or not update:
        return

    try:
        from bot_core.telemetry import atimed, new_rid, set_rid
        set_rid(new_rid(prefix="tg-"))
    except Exception:
        atimed = None  # type: ignore

    payloads: List[str]
    documents: List[Dict[str, Any]] = []
    media_payloads: List[Dict[str, Any]] = []
    temp_files: List[str] = []

    if isinstance(responses, _bridge.BridgeResponse):
        payloads = responses.messages or []
        documents = responses.documents or []
        media_payloads = responses.media or []
        temp_files = list(responses.actions.get("temp_files", []))
    else:
        payloads = responses or []

    if not payloads and not documents and not media_payloads:
        return

    chat_id = update.effective_chat.id if update and update.effective_chat else None

    async def _send_text(body: str) -> None:
        if update.message:
            if atimed:
                async with atimed("tg.send_text", bytes=len(body or "")):
                    await update.message.reply_text(body, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(body, parse_mode=ParseMode.HTML)
        elif chat_id and context:
            if atimed:
                async with atimed("tg.send_text", bytes=len(body or "")):
                    await context.bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.HTML)

    for body in payloads:
        if body:
            await _send_text(body)

    if context and chat_id:
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            caption = doc.get("caption")
            filename = doc.get("filename")
            path = doc.get("path")
            url = doc.get("url")
            content_bytes = doc.get("bytes")
            try:
                if isinstance(content_bytes, (bytes, bytearray)) and content_bytes:
                    bio = BytesIO(bytes(content_bytes))
                    bio.name = filename or "document.bin"
                    if atimed:
                        async with atimed("tg.send_document", bytes=len(content_bytes), via="bytes"):
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=bio,
                                caption=caption,
                                filename=filename,
                                parse_mode=ParseMode.HTML,
                            )
                    else:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=bio,
                            caption=caption,
                            filename=filename,
                            parse_mode=ParseMode.HTML,
                        )
                elif path and os.path.exists(path):
                    with open(path, "rb") as handler:
                        if atimed:
                            try:
                                size = os.path.getsize(path)
                            except Exception:
                                size = 0
                            async with atimed("tg.send_document", bytes=size, via="file"):
                                await context.bot.send_document(
                                    chat_id=chat_id,
                                    document=handler,
                                    caption=caption,
                                    filename=filename,
                                    parse_mode=ParseMode.HTML,
                                )
                        else:
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=handler,
                                caption=caption,
                                filename=filename,
                                parse_mode=ParseMode.HTML,
                            )
                elif url:
                    if atimed:
                        async with atimed("tg.send_document", bytes=0, via="url"):
                            await context.bot.send_document(
                                chat_id=chat_id,
                                document=url,
                                caption=caption,
                                filename=filename,
                                parse_mode=ParseMode.HTML,
                            )
                    else:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=url,
                            caption=caption,
                            filename=filename,
                            parse_mode=ParseMode.HTML,
                        )
            except Exception:
                logging.exception("Failed to relay bridge document to Telegram")

        for media in media_payloads:
            if not isinstance(media, dict):
                continue
            if media.get("type") not in {"photo", "image"}:
                continue
            caption = media.get("caption")
            path = media.get("path")
            url = media.get("url")
            media_bytes = media.get("bytes")
            try:
                if isinstance(media_bytes, (bytes, bytearray)) and media_bytes:
                    bio = BytesIO(bytes(media_bytes))
                    bio.name = media.get("filename") or "image.jpg"
                    if atimed:
                        async with atimed("tg.send_photo", bytes=len(media_bytes), via="bytes"):
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=bio,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                            )
                    else:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=bio,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        )
                elif path and os.path.exists(path):
                    with open(path, "rb") as handler:
                        if atimed:
                            try:
                                size = os.path.getsize(path)
                            except Exception:
                                size = 0
                            async with atimed("tg.send_photo", bytes=size, via="file"):
                                await context.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=handler,
                                    caption=caption,
                                    parse_mode=ParseMode.HTML,
                                )
                        else:
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=handler,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                            )
                elif url:
                    if atimed:
                        async with atimed("tg.send_photo", bytes=0, via="url"):
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=url,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                            )
                    else:
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        )
            except Exception:
                logging.exception("Failed to relay bridge media to Telegram")

    _cleanup_temp_files(temp_files)


def _cleanup_temp_files(paths: List[str]) -> None:
    for entry in paths or []:
        try:
            if entry and os.path.exists(entry):
                os.remove(entry)
        except OSError:
            logging.debug("Failed to clean temp file %s", entry)

# ===== Helper: contact URL (prefer @username, return None otherwise) =====
def _tg_contact_url(u: Dict[str, Any]) -> Optional[str]:
    username = (u.get("tg_username") or "").strip()
    if username:
        return f"https://t.me/{username}"
    return None

from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.ext import filters as _filters

# =================== Storage ===================

# === Safe reply helper: works for both message and callback ===
_PANEL_MESSAGES: Dict[str, Dict[str, int]] = {}


async def _panel_message(update, context: ContextTypes.DEFAULT_TYPE, text: str, *, parse_mode=None, reply_markup=None):
    """Edit the existing panel message if possible; otherwise send a new one and remember it."""
    bot = context.bot if context else update.get_bot()
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    user_id = str(update.effective_user.id) if update and update.effective_user else None
    previous = _PANEL_MESSAGES.get(user_id) if user_id else None
    if not previous and getattr(update, "callback_query", None) and getattr(update.callback_query, "message", None):
        q_msg = update.callback_query.message
        previous = {"chat_id": q_msg.chat_id, "message_id": q_msg.message_id}

    async def _delete_entry(entry):
        try:
            await bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
        except Exception:
            pass

    if previous:
        try:
            msg = await bot.edit_message_text(
                chat_id=previous["chat_id"],
                message_id=previous["message_id"],
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            if user_id and msg:
                _PANEL_MESSAGES[user_id] = {"chat_id": msg.chat_id, "message_id": msg.message_id}
            if getattr(update, "message", None):
                try:
                    await update.message.delete()
                except Exception:
                    pass
            if getattr(update, "callback_query", None):
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
            return msg
        except Exception:
            await _delete_entry(previous)
            _PANEL_MESSAGES.pop(user_id, None)

    if getattr(update, "callback_query", None):
        q = update.callback_query
        try:
            await q.answer()
        except Exception:
            pass
        try:
            await bot.delete_message(chat_id=q.message.chat_id, message_id=q.message.message_id)
        except Exception:
            pass
        msg = await bot.send_message(chat_id=q.message.chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    elif getattr(update, "message", None):
        orig_msg = update.message
        msg = await bot.send_message(chat_id=orig_msg.chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        try:
            await orig_msg.delete()
        except Exception:
            pass
    else:
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)

    if user_id and msg:
        _PANEL_MESSAGES[user_id] = {"chat_id": msg.chat_id, "message_id": msg.message_id}

    chat_data = getattr(context, "chat_data", None)
    if isinstance(chat_data, dict):
        is_main = text.strip().startswith("🏠") and any(label in text for label in MAIN_MENU_TEXTS)
        chat_data["main_menu_visible"] = is_main
        if is_main:
            # Prevent downstream fallback from duplicating the main menu
            chat_data["suppress_fallback"] = True
    return msg


async def _dismiss_panel_message(context: ContextTypes.DEFAULT_TYPE, tg_id: str) -> bool:
    entry = _PANEL_MESSAGES.pop(str(tg_id), None)
    if not entry:
        return False
    if not getattr(context, "bot", None):
        return False
    try:
        await context.bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
        return True
    except Exception:
        return False


async def _send_or_edit(update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    return await _panel_message(update, context, text, **kwargs)


async def _ensure_main_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    force: bool = False,
    notice: Optional[str] = None,
) -> None:
    """Send the persistent reply keyboard once per user while avoiding repeated reminder spam."""
    if not context or not getattr(context, "bot", None):
        return
    chat = update.effective_chat if update else None
    if not chat:
        return

    tg_id = str(update.effective_user.id) if update and update.effective_user else None
    username = update.effective_user.username if update and update.effective_user else None
    user_ready = False
    db = None
    user_obj = None
    if tg_id:
        try:
            db = _load_db()
            user_obj = _ensure_user(db, tg_id, username)
            flags = user_obj.setdefault("flags", {})
            user_ready = bool(flags.get("reply_keyboard_ready"))
        except Exception:
            db = None
            user_obj = None
            user_ready = False

    chat_data = context.chat_data if isinstance(getattr(context, "chat_data", None), dict) else None
    session_ready = bool(chat_data.get("reply_keyboard_ready")) if chat_data else False

    if user_ready and session_ready and not force:
        return

    lang = _get_user_report_lang(user_obj) if user_obj else None
    helper_text = notice or ""
    await context.bot.send_message(
        chat_id=chat.id,
        text=helper_text if helper_text else " ",
        reply_markup=build_start_menu(lang),
    )

    if chat_data is not None:
        chat_data["reply_keyboard_ready"] = True

    if tg_id and db and user_obj:
        try:
            user_obj.setdefault("flags", {})["reply_keyboard_ready"] = True
            _save_db(db)
        except Exception:
            pass
def _should_force_reply_keyboard(chat_state: Optional[Dict[str, Any]], ttl_seconds: int = 300) -> bool:
    if not isinstance(chat_state, dict):
        return True
    now = time.time()
    last = float(chat_state.get("reply_keyboard_last_force", 0))
    if ttl_seconds <= 0 or now - last >= ttl_seconds:
        chat_state["reply_keyboard_last_force"] = now
        return True
    return False


_user_locks = defaultdict(asyncio.Lock)  # per-user locks (use as needed)
def _user_lock(tg_id: str):
    return _user_locks[str(tg_id)]


# Telegram: parallel report processing with bounded concurrency.
_TG_REPORT_LIMITS_LOCK = asyncio.Lock()
_TG_REPORT_GLOBAL_SEM: Optional[asyncio.Semaphore] = None
_TG_REPORT_USER_SEMS: Dict[str, asyncio.Semaphore] = {}

_TG_INFLIGHT_LOCK = asyncio.Lock()
_TG_INFLIGHT: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _tg_report_limits() -> Tuple[int, int]:
    # Defaults chosen to allow parallelism without saturating the machine.
    # Increase via env if you have more CPU/network headroom.
    per_user = int(os.getenv("TG_REPORT_CONCURRENCY_PER_USER", "2") or 2)
    global_limit = int(os.getenv("TG_REPORT_CONCURRENCY_GLOBAL", "4") or 4)
    per_user = max(1, min(per_user, 6))
    global_limit = max(1, min(global_limit, 30))
    return per_user, global_limit


async def _tg_get_semaphores(tg_id: str) -> Tuple[asyncio.Semaphore, asyncio.Semaphore]:
    tg_id = str(tg_id)
    per_user, global_limit = _tg_report_limits()
    async with _TG_REPORT_LIMITS_LOCK:
        global _TG_REPORT_GLOBAL_SEM
        if not _TG_REPORT_GLOBAL_SEM or getattr(_TG_REPORT_GLOBAL_SEM, "_value", None) is None:
            _TG_REPORT_GLOBAL_SEM = asyncio.Semaphore(global_limit)
        user_sem = _TG_REPORT_USER_SEMS.get(tg_id)
        if not user_sem:
            user_sem = asyncio.Semaphore(per_user)
            _TG_REPORT_USER_SEMS[tg_id] = user_sem
        return user_sem, _TG_REPORT_GLOBAL_SEM


async def _tg_register_inflight(
    tg_id: str,
    vin: str,
    *,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    ttl_s: float = 900.0,
) -> bool:
    tg_id = str(tg_id)
    vin = (vin or "").strip().upper()
    if not vin:
        return False
    now = time.time()
    async with _TG_INFLIGHT_LOCK:
        bucket = _TG_INFLIGHT.setdefault(tg_id, {})
        # prune old
        for k, entry in list(bucket.items()):
            try:
                ts = float((entry or {}).get("ts") or 0.0)
            except Exception:
                ts = 0.0
            if (now - ts) > ttl_s:
                bucket.pop(k, None)

        entry = bucket.get(vin)
        if not isinstance(entry, dict):
            entry = {"ts": now, "subs": set()}
            bucket[vin] = entry
            is_new = True
        else:
            is_new = False
            entry["ts"] = now

        if chat_id and message_id:
            subs = entry.get("subs")
            if not isinstance(subs, set):
                subs = set()
                entry["subs"] = subs
            try:
                subs.add((int(chat_id), int(message_id)))
            except Exception:
                pass

        return is_new


async def _tg_inflight_targets(tg_id: str, vin: str) -> List[Tuple[int, int]]:
    tg_id = str(tg_id)
    vin = (vin or "").strip().upper()
    if not (tg_id and vin):
        return []
    async with _TG_INFLIGHT_LOCK:
        entry = (_TG_INFLIGHT.get(tg_id) or {}).get(vin)
        if not isinstance(entry, dict):
            return []
        subs = entry.get("subs")
        if not isinstance(subs, set):
            return []
        out: List[Tuple[int, int]] = []
        for pair in list(subs):
            try:
                c, m = pair
                out.append((int(c), int(m)))
            except Exception:
                continue
        return out


async def _tg_edit_inflight_messages(
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: str,
    vin: str,
    *,
    text: str,
    parse_mode: Optional[str] = ParseMode.HTML,
) -> None:
    targets = await _tg_inflight_targets(tg_id, vin)
    if not targets:
        return
    for chat_id, message_id in targets:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            try:
                logger.warning("progress_update_failed_html", extra={"err": str(exc)})
            except Exception:
                pass
            try:
                header_plain = re.sub(r"<[^>]+>", "", text or "")
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=header_plain,
                )
            except Exception:
                pass


async def _tg_unmark_inflight(tg_id: str, vin: str) -> None:
    tg_id = str(tg_id)
    vin = (vin or "").strip().upper()
    if not vin:
        return
    async with _TG_INFLIGHT_LOCK:
        bucket = _TG_INFLIGHT.get(tg_id)
        if isinstance(bucket, dict):
            bucket.pop(vin, None)
            if not bucket:
                _TG_INFLIGHT.pop(tg_id, None)


async def _tg_submit_report_job(context: ContextTypes.DEFAULT_TYPE, job: Dict[str, Any]) -> None:
    tg_id = str(job.get("tg_id") or "")
    vin = str(job.get("vin") or "")
    chat_id = int(job.get("chat_id") or 0)
    mid = int(job.get("progress_message_id") or 0)
    is_new = await _tg_register_inflight(tg_id, vin, chat_id=chat_id, message_id=mid)
    if not is_new:
        # Same VIN already being processed; keep this request as a subscriber.
        # (Do not reject; progress updates will fan-out to all subscribers.)
        return

    async def _runner() -> None:
        user_sem: Optional[asyncio.Semaphore] = None
        global_sem: Optional[asyncio.Semaphore] = None
        got_user = False
        got_global = False
        try:
            user_sem, global_sem = await _tg_get_semaphores(tg_id)
            # IMPORTANT: acquire in a consistent order to avoid deadlocks.
            await user_sem.acquire()
            got_user = True
            await global_sem.acquire()
            got_global = True
            await _tg_run_vin_report_job(context, job)
        finally:
            try:
                if got_global and global_sem:
                    global_sem.release()
            except Exception:
                pass
            try:
                if got_user and user_sem:
                    user_sem.release()
            except Exception:
                pass
            try:
                await _tg_unmark_inflight(tg_id, vin)
            except Exception:
                pass

    task = asyncio.create_task(_runner())
    try:
        _track_bg_task(task, name=f"tg_report_job:{tg_id}:{vin}")
    except Exception:
        pass


SUPER_DASHBOARD_EVENTS_LIMIT = 15


def _super_dashboard_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    state = context.bot_data.setdefault("super_dashboard", {}) if context else {}
    listeners = state.setdefault("listeners", {})
    events = state.get("events")
    if not isinstance(events, deque) or events.maxlen != SUPER_DASHBOARD_EVENTS_LIMIT:
        events_list = list(events) if isinstance(events, (list, deque)) else []
        state["events"] = deque(events_list, maxlen=SUPER_DASHBOARD_EVENTS_LIMIT)
    return state


def _compute_super_dashboard_snapshot() -> Dict[str, Any]:
    db = _load_db()
    users = db.get("users", {}) or {}
    total_users = len(users)
    active_users = 0
    expiring_soon = 0
    expired_users = 0
    reports_today = 0
    reports_month = 0
    pending_reports = 0
    recent_activity: List[Tuple[str, str, Dict[str, Any]]] = []
    wa_users = 0
    tg_users = 0
    compact: List[Dict[str, Any]] = []

    for tg_id, user in users.items():
        limits = user.get("limits", {}) or {}
        reports_today += _safe_int(limits.get("today_used"))
        reports_month += _safe_int(limits.get("month_used"))
        stats = user.get("stats", {}) or {}
        pending_reports += _safe_int(stats.get("pending_reports"))
        last_ts = stats.get("last_report_ts")
        if last_ts:
            recent_activity.append((str(last_ts), str(tg_id), user))

        platform_code = "wa" if str(tg_id).isdigit() and len(str(tg_id)) > 10 else "tg"
        if platform_code == "wa":
            wa_users += 1
        else:
            tg_users += 1

        daily_limit = _safe_int(limits.get("daily"))
        daily_used = _safe_int(limits.get("today_used"))
        monthly_limit = _safe_int(limits.get("monthly"))
        monthly_used = _safe_int(limits.get("month_used"))
        days_left = _days_left(user.get("expiry_date"))
        plan = (user.get("plan") or "-").lower()

        compact.append({
            "name": _display_name(user),
            "platform": platform_code,
            "plan": plan,
            "days_left": days_left,
            "daily_used": daily_used,
            "daily_limit": daily_limit,
            "monthly_used": monthly_used,
            "monthly_limit": monthly_limit,
        })

        days_left = _days_left(user.get("expiry_date"))
        if user.get("is_active"):
            if days_left is None or days_left >= 0:
                active_users += 1
            if days_left is not None and 0 <= days_left <= 3:
                expiring_soon += 1
        if days_left is not None and days_left < 0:
            expired_users += 1

    recent_activity.sort(key=lambda row: row[0], reverse=True)
    top_recent = [
        {
            "tg_id": tg_id,
            "name": _display_name(user),
            "ts": ts,
            "total": _safe_int(user.get("stats", {}).get("total_reports")),
        }
        for ts, tg_id, user in recent_activity[:3]
    ]

    snapshot = {
        "total_users": total_users,
        "active_users": active_users,
        "expiring_soon": expiring_soon,
        "expired_users": expired_users,
        "reports_today": reports_today,
        "reports_month": reports_month,
        "pending_reports": pending_reports,
        "pending_activation": len(db.get("activation_requests", []) or []),
        "top_recent": top_recent,
        "wa_users": wa_users,
        "tg_users": tg_users,
        "users_compact": sorted(
            compact,
            key=lambda e: int(str(e.get("daily", "0/0")).split("/")[0] or 0),
            reverse=True,
        )[:12],
    }
    return snapshot


SUPER_DASHBOARD_LOCALE = {
    "ar": {
        "header": "🛡️ <b>لوحة السوبر المباشرة</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "active": "👥 النشطون: <b>{active}</b>/<b>{total}</b>",
        "soon": "⏳ ينتهي قريباً (3 أيام): <b>{soon}</b>",
        "expired": "🛑 منتهي: <b>{expired}</b>",
        "today": "📊 تقارير اليوم: <b>{today}</b>",
        "month": "🗓️ هذا الشهر: <b>{month}</b>",
        "pending_activation": "📥 طلبات التفعيل المعلقة: <b>{pending_activation}</b>",
        "pending_reports": "⏳ تقارير قيد التنفيذ: <b>{pending_reports}</b>",
        "recent_header": "⏱️ آخر التقارير:\n",
        "recent_total": " (الإجمالي: {total})\n",
        "events_header": "📰 آخر الأحداث:\n",
        "no_events": "• لا أحداث حديثة",
        "footer": "⚙️ استخدم الأزرار لتحديث أو إخفاء اللوحة.",
        "btn_refresh": "↻ تحديث",
        "btn_hide": "❌ إخفاء",
        "platforms": "📞 واتساب: <b>{wa}</b> | 💬 تيليغرام: <b>{tg}</b>",
        "compact_header": "\n\n📊 حالة المستخدمين (أعلى {cnt}):\n",
        "compact_entry": "• {name} — {platform} | {plan} | {expiry} | {daily} | {monthly}\n",
        "platform_wa": "📞 WA",
        "platform_tg": "💬 TG",
        "plan_label": "خطة: {plan}",
        "expiry_label": "ينتهي: {expiry}",
        "daily_label": "يومي {used}/{limit}",
        "monthly_label": "شهري {used}/{limit}",
    },
    "en": {
        "header": "🛡️ <b>Live Super Dashboard</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "active": "👥 Active: <b>{active}</b>/<b>{total}</b>",
        "soon": "⏳ Expiring soon (3 days): <b>{soon}</b>",
        "expired": "🛑 Expired: <b>{expired}</b>",
        "today": "📊 Reports today: <b>{today}</b>",
        "month": "🗓️ This month: <b>{month}</b>",
        "pending_activation": "📥 Pending activations: <b>{pending_activation}</b>",
        "pending_reports": "⏳ Reports in progress: <b>{pending_reports}</b>",
        "recent_header": "⏱️ Latest reports:\n",
        "recent_total": " (total: {total})\n",
        "events_header": "📰 Latest events:\n",
        "no_events": "• No recent events",
        "footer": "⚙️ Use the buttons to refresh or hide.",
        "btn_refresh": "↻ Refresh",
        "btn_hide": "❌ Hide",
        "platforms": "📞 WhatsApp: <b>{wa}</b> | 💬 Telegram: <b>{tg}</b>",
        "compact_header": "\n\n📊 User status (top {cnt}):\n",
        "compact_entry": "• {name} — {platform} | {plan} | {expiry} | {daily} | {monthly}\n",
        "platform_wa": "📞 WA",
        "platform_tg": "💬 TG",
        "plan_label": "Plan: {plan}",
        "expiry_label": "Expires: {expiry}",
        "daily_label": "Daily {used}/{limit}",
        "monthly_label": "Monthly {used}/{limit}",
    },
    "ku": {
        "header": "🛡️ <b>داشبۆردی سوپەر ڕاستەوخۆ</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "active": "👥 چالاک: <b>{active}</b>/<b>{total}</b>",
        "soon": "⏳ دەکۆتایەوە لە ٣ ڕۆژدا: <b>{soon}</b>",
        "expired": "🛑 کۆتایی هاتوو: <b>{expired}</b>",
        "today": "📊 ڕاپۆرتەکانی ئەمڕۆ: <b>{today}</b>",
        "month": "🗓️ ئەم مانگە: <b>{month}</b>",
        "pending_activation": "📥 داواکاری چالاککردنی چاوەڕێکەر: <b>{pending_activation}</b>",
        "pending_reports": "⏳ ڕاپۆرتە هەڵکەوتووەکان: <b>{pending_reports}</b>",
        "recent_header": "⏱️ دوایین ڕاپۆرتەکان:\n",
        "recent_total": " (کۆ: {total})\n",
        "events_header": "📰 دوایین رووداوەکان:\n",
        "no_events": "• هیچ رووداوێکی نوێ نیە",
        "footer": "⚙️ دوگمەکان بەکاربهێنە بۆ نوێکردنەوە یان شاردنەوە.",
        "btn_refresh": "↻ نوێکردنەوە",
        "btn_hide": "❌ شاردنەوە",
        "platforms": "📞 واتساپ: <b>{wa}</b> | 💬 تیلیگرام: <b>{tg}</b>",
        "compact_header": "\n\n📊 دۆخی بەکارهێنەران (سەری {cnt}):\n",
        "compact_entry": "• {name} — {platform} | {plan} | {expiry} | {daily} | {monthly}\n",
        "platform_wa": "📞 WA",
        "platform_tg": "💬 TG",
        "plan_label": "پلانی: {plan}",
        "expiry_label": "دەکۆتایەوە: {expiry}",
        "daily_label": "ڕۆژانە {used}/{limit}",
        "monthly_label": "مانگانە {used}/{limit}",
    },
    "ckb": {
        "header": "🛡️ <b>داشبۆردی سووپەر بەردەوام</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "active": "👥 چالاک: <b>{active}</b>/<b>{total}</b>",
        "soon": "⏳ لە ٣ ڕۆژدا دەکۆتایەوە: <b>{soon}</b>",
        "expired": "🛑 بەسەرچوو: <b>{expired}</b>",
        "today": "📊 ڕاپۆرتەکانی ئەمڕۆ: <b>{today}</b>",
        "month": "🗓️ ئەم مانگە: <b>{month}</b>",
        "pending_activation": "📥 داواکاریی چالاککردن چاوەڕوان: <b>{pending_activation}</b>",
        "pending_reports": "⏳ ڕاپۆرتە جێبەجێبوونەکان: <b>{pending_reports}</b>",
        "recent_header": "⏱️ دوایین ڕاپۆرتەکان:\n",
        "recent_total": " (کۆی گشتی: {total})\n",
        "events_header": "📰 دوایین رووداوەکان:\n",
        "no_events": "• هیچ رووداوێکی نوێ نییە",
        "footer": "⚙️ دوگمەکان بەکاربهێنە بۆ نوێکردنەوە یان شاردنەوە.",
        "btn_refresh": "↻ نوێکردنەوە",
        "btn_hide": "❌ شاردنەوە",
        "platforms": "📞 واتساپ: <b>{wa}</b> | 💬 تێلەگرام: <b>{tg}</b>",
        "compact_header": "\n\n📊 دۆخی بەکارهێنەران (سەری {cnt}):\n",
        "compact_entry": "• {name} — {platform} | {plan} | {expiry} | {daily} | {monthly}\n",
        "platform_wa": "📞 WA",
        "platform_tg": "💬 TG",
        "plan_label": "پلانی: {plan}",
        "expiry_label": "کۆتایی: {expiry}",
        "daily_label": "ڕۆژانە {used}/{limit}",
        "monthly_label": "مانگانە {used}/{limit}",
    },
}


def _super_dashboard_locale(lang: str) -> Dict[str, str]:
    lang = _normalize_report_lang_code(lang)
    return SUPER_DASHBOARD_LOCALE.get(lang, SUPER_DASHBOARD_LOCALE["ar"])


def _super_dashboard_lang(chat_id: Any) -> str:
    """Force السوبر أدمن على العربية لضمان توحيد العرض للأزرار والإشعارات."""
    try:
        db = _load_db()
        user = db.get("users", {}).get(str(chat_id))
        if user and _is_super_admin(str(chat_id)):
            return "ar"
        if user:
            return _get_user_report_lang(user)
    except Exception:
        pass
    return "ar"


def _format_super_dashboard_text(snapshot: Dict[str, Any], events: List[str], *, lang: str) -> str:
    loc = _super_dashboard_locale(lang)
    lines = [
        loc["active"].format(active=snapshot["active_users"], total=snapshot["total_users"]),
        loc["soon"].format(soon=snapshot["expiring_soon"]),
        loc["expired"].format(expired=snapshot["expired_users"]),
        loc["today"].format(today=snapshot["reports_today"]),
        loc["month"].format(month=snapshot["reports_month"]),
        loc["platforms"].format(wa=snapshot["wa_users"], tg=snapshot["tg_users"]),
        loc["pending_activation"].format(pending_activation=snapshot["pending_activation"]),
        loc["pending_reports"].format(pending_reports=snapshot["pending_reports"]),
    ]
    text = loc["header"] + "\n".join(lines)

    if snapshot.get("users_compact"):
        text += loc["compact_header"].format(cnt=len(snapshot["users_compact"]))
        for entry in snapshot["users_compact"]:
            platform_label = loc["platform_wa"] if entry.get("platform") == "wa" else loc["platform_tg"]
            expiry_val = entry.get("days_left")
            expiry_display = entry.get("days_left") if expiry_val is not None else "—"
            daily_limit = entry.get("daily_limit")
            monthly_limit = entry.get("monthly_limit")
            daily_limit_disp = daily_limit if daily_limit > 0 else "∞"
            monthly_limit_disp = monthly_limit if monthly_limit > 0 else "∞"
            text += loc["compact_entry"].format(
                name=entry.get("name"),
                platform=platform_label,
                plan=loc["plan_label"].format(plan=entry.get("plan")),
                expiry=loc["expiry_label"].format(expiry=expiry_display),
                daily=loc["daily_label"].format(used=entry.get("daily_used"), limit=daily_limit_disp),
                monthly=loc["monthly_label"].format(used=entry.get("monthly_used"), limit=monthly_limit_disp),
            )

    if snapshot.get("top_recent"):
        text += "\n" + loc["recent_header"]
        for entry in snapshot["top_recent"]:
            text += (
                f"• {escape(entry['name'])} — <code>{entry['ts']}</code>"
                + loc["recent_total"].format(total=entry["total"])
            )

    if events:
        text += "\n" + loc["events_header"] + "\n".join(events)
    else:
        text += "\n" + loc["events_header"] + loc["no_events"]

    text += "\n\n" + loc["footer"]
    return text


def _super_dashboard_keyboard(lang: str) -> InlineKeyboardMarkup:
    loc = _super_dashboard_locale(lang)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(loc["btn_refresh"], callback_data="super_dash:refresh")],
        [InlineKeyboardButton(loc["btn_hide"], callback_data="super_dash:close")],
    ])


async def _refresh_super_dashboards(context: ContextTypes.DEFAULT_TYPE, state: Optional[Dict[str, Any]] = None) -> None:
    if not context or not getattr(context, "bot", None):
        return
    state = state or _super_dashboard_state(context)
    listeners = state.get("listeners") or {}
    if not listeners:
        return
    snapshot = _compute_super_dashboard_snapshot()
    state["last_snapshot"] = snapshot
    for chat_id, message_id in list(listeners.items()):
        try:
            lang = _super_dashboard_lang(chat_id)
            text = _format_super_dashboard_text(snapshot, list(state.get("events", [])), lang=lang)
            kb = _super_dashboard_keyboard(lang)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            listeners.pop(chat_id, None)


async def _super_dashboard_event(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if not context:
        return
    state = _super_dashboard_state(context)
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    safe_text = escape(text)
    state.setdefault("events", deque(maxlen=SUPER_DASHBOARD_EVENTS_LIMIT)).appendleft(
        f"• <b>{timestamp}</b> — {safe_text}"
    )
    await _refresh_super_dashboards(context, state=state)

def _get_user_report_lang(u: Dict[str, Any]) -> str:
    lang = (
        u.get("report_lang")
        or (u.get("prefs") or {}).get("report_lang")
        or get_report_default_lang()
    )
    return _normalize_report_lang_code(lang)


def _set_user_report_lang(u: Dict[str, Any], lang: str) -> None:
    lang = _normalize_report_lang_code(lang)
    prefs = u.setdefault("prefs", {})
    prefs["report_lang"] = lang
    # Keep system language in sync so menus/notifications match the selected report language.
    u["language"] = lang
    u["report_lang"] = lang
    u["lang"] = lang


async def _maybe_translate_html(html: Optional[str], lang: str) -> Optional[str]:
    if not html:
        return html
    lang_code = _normalize_report_lang_code(lang)
    if lang_code == "en":
        return html
    try:
        return await _translate_html(html, lang_code)
    except Exception:
        return _inject_rtl(html, lang=lang_code)


def _set_user_limits(u: Dict[str, Any], daily_limit: int, monthly_limit: int) -> None:
    limits = u.setdefault("limits", {})
    limits["daily"] = int(max(0, daily_limit))
    limits["monthly"] = int(max(0, monthly_limit))
    limits["today_used"] = 0
    limits["month_used"] = 0
    limits["last_day"] = None
    limits["last_month"] = None


def _build_vin_progress_header(
    vin: str,
    *,
    monthly_remaining: Optional[int],
    monthly_limit: int,
    today_used: int,
    daily_limit: int,
    days_left: Optional[int],
    lang: Optional[str] = None,
) -> str:
    lang = _normalize_report_lang_code(lang)
    if monthly_remaining is None or monthly_limit <= 0:
        monthly_line = _bridge.t("progress.vin.monthly.unlimited", lang)
    else:
        monthly_line = _bridge.t(
            "progress.vin.monthly.remaining", lang,
            remaining=monthly_remaining, limit=monthly_limit,
        )

    if daily_limit and daily_limit > 0:
        daily_line = _bridge.t("progress.vin.daily.remaining", lang, used=today_used, limit=daily_limit)
    else:
        daily_line = _bridge.t("progress.vin.daily.unlimited", lang, used=today_used)

    if days_left is None:
        days_txt = ""
    elif days_left > 0:
        days_txt = _bridge.t("progress.vin.days_left", lang, days=days_left)
    elif days_left == 0:
        days_txt = _bridge.t("progress.vin.days_left.today", lang)
    else:
        days_txt = _bridge.t("progress.vin.days_left.expired", lang)

    title = _bridge.t("progress.vin.title", lang, vin=vin, preserve_latin=True)
    body = _bridge.t("progress.vin.body", lang, monthly_line=monthly_line, days_line=days_txt, daily_line=daily_line, preserve_latin=True)
    return f"{title}\n{body}"


def _reserve_report_slot(u: Dict[str, Any]) -> None:
    limits = u.setdefault("limits", {})
    limits["today_used"] = _safe_int(limits.get("today_used")) + 1
    limits["month_used"] = _safe_int(limits.get("month_used")) + 1
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = stats.get("pending_reports", 0) + 1


async def _finalize_report_request(
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: str,
    *,
    delivered: bool,
    rid: Optional[str] = None,
) -> Dict[str, Any]:
    lock = _user_lock(tg_id)
    async with lock:
        if rid:
            # Exactly-once accounting (reserve/commit/refund) across retries/restarts.
            try:
                if delivered:
                    _commit_credit(tg_id, rid=rid, meta={"platform": "telegram"})
                else:
                    _refund_credit(tg_id, rid=rid, meta={"platform": "telegram"})
            except Exception:
                logger.warning("rid-aware finalize failed", exc_info=True)

        else:
            # Legacy path (no rid): mutate counters directly.
            db = _load_db()
            u = _ensure_user(db, tg_id, None)
            limits = u.setdefault("limits", {})
            stats = u.setdefault("stats", {})
            stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)

            if delivered:
                stats["total_reports"] = stats.get("total_reports", 0) + 1
                stats["last_report_ts"] = _now_str()
            else:
                limits["today_used"] = max(0, _safe_int(limits.get("today_used")) - 1)
                limits["month_used"] = max(0, _safe_int(limits.get("month_used")) - 1)

            if delivered:
                await _notify_usage_caps_if_needed(context, u)

            _save_db(db)

        # Refresh snapshot from disk after accounting (rid-aware path uses storage helpers).
        db = _load_db()
        u = _ensure_user(db, tg_id, None)
        limits = u.setdefault("limits", {})

        if delivered:
            try:
                await _notify_usage_caps_if_needed(context, u)
            except Exception:
                pass

        snapshot = {
            "user": copy.deepcopy(u),
            "monthly_remaining": _remaining_monthly_reports(u),
            "monthly_limit": _safe_int(limits.get("monthly")),
            "today_used": _safe_int(limits.get("today_used")),
            "daily_limit": _safe_int(limits.get("daily")),
            "days_left": _days_left(u.get("expiry_date")),
        }

    return snapshot


def _remove_pending_request(db: Dict[str, Any], tg_id: str) -> None:
    tg_str = str(tg_id)
    db["activation_requests"] = [
        req for req in db.get("activation_requests", []) if str(req.get("tg_id")) != tg_str
    ]

# =================== UI ===================

def build_main_menu(tg_id: str, lang: Optional[str] = None) -> InlineKeyboardMarkup:
    """قائمة رئيسية موحّدة باستخدام الأزرار الداخلية (مترجمة حسب لغة المستخدم)."""
    return build_reference_menu(tg_id, lang=lang)

def build_reference_menu(tg_id: str, lang: Optional[str] = None) -> InlineKeyboardMarkup:
    """بناء قائمة مرجعية شاملة تحتوي على جميع الخيارات (مترجمة)."""
    db = _load_db()
    u = _ensure_user(db, tg_id, None)
    lang_code = _normalize_report_lang_code(lang or _get_user_report_lang(u))

    is_super_admin = _is_super_admin(tg_id)
    is_admin = _is_admin_tg(tg_id)

    def L(key: str) -> str:
        # استخدم قاموس الترجمة في الجسر
        return _bridge.t(key, lang_code)

    rows: List[List[InlineKeyboardButton]] = []

    if not is_super_admin:
        rows.append([
            InlineKeyboardButton(L("menu.profile.label"), callback_data="main_menu:profile"),
            InlineKeyboardButton(L("menu.activation.label"), callback_data="main_menu:activation"),
        ])
        rows.append([
            InlineKeyboardButton(L("menu.help.label"), callback_data="main_menu:help"),
            InlineKeyboardButton(L("menu.language.label"), callback_data="main_menu:language"),
        ])

    if is_super_admin or is_admin:
        rows.append([
            InlineKeyboardButton(L("menu.users.label"), callback_data="main_menu:users"),
            InlineKeyboardButton(L("menu.stats.label"), callback_data="main_menu:stats"),
        ])
        rows.append([
            InlineKeyboardButton(L("menu.pending.label"), callback_data="main_menu:pending"),
            InlineKeyboardButton(L("menu.settings.label"), callback_data="main_menu:settings"),
        ])
    if is_super_admin:
        rows.append([
            InlineKeyboardButton(L("menu.notifications.label"), callback_data="main_menu:notifications"),
        ])

    return InlineKeyboardMarkup(rows)

def build_start_menu(lang: Optional[str] = None) -> ReplyKeyboardMarkup:
    btn_text = _main_menu_button_text(lang)
    placeholder = _bridge.t("start.keyboard.hint", _normalize_report_lang_code(lang))

    return ReplyKeyboardMarkup(
        [[KeyboardButton(btn_text)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def _bridge_menu_to_inline_keyboard(menu_action: Optional[Dict[str, Any]], tg_id: str) -> InlineKeyboardMarkup:
    if not isinstance(menu_action, dict):
        return build_reference_menu(tg_id)
    items = menu_action.get("items") or []
    if not items:
        return build_reference_menu(tg_id)
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for entry in items:
        row_key = int(entry.get("row") or 0)
        grouped[row_key].append(entry)
    rows: List[List[InlineKeyboardButton]] = []
    for row in sorted(grouped.keys()):
        row_entries = sorted(grouped[row], key=lambda e: (e.get("col", 0), e.get("order", 0)))
        buttons = [InlineKeyboardButton(entry["label"], callback_data=f"main_menu:{entry['id']}") for entry in row_entries]
        rows.append(buttons)
    return InlineKeyboardMarkup(rows)


def _apply_bridge_menu_actions(context: ContextTypes.DEFAULT_TYPE, actions: Optional[Dict[str, Any]]) -> None:
    if not actions or not context or not isinstance(context.user_data, dict):
        return
    if actions.get("await_activation_phone"):
        context.user_data["await"] = {"op": "activation_phone"}
        if actions.get("activation_cc"):
            context.user_data["activation_cc"] = actions["activation_cc"]
    if actions.get("await_language_choice"):
        context.user_data["await"] = {"op": "language_choice"}
    if actions.get("clear_activation_state") or actions.get("clear_state"):
        context.user_data.pop("await", None)
        context.user_data.pop("activation_cc", None)


async def _handle_general_menu_delegate(
    action: Optional[str],
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not action:
        return False
    if action in {"profile", "whoami"}:
        await whoami_command(update, context)
        return True
    if action in {"balance"}:
        await balance_command(update, context)
        return True
    if action in {"new_report", "report"}:
        await new_report_command(update, context)
        return True
    if action in {"request_activation", "activation"}:
        await request_activation_command(update, context)
        return True
    if action == "help":
        await help_command(update, context)
        return True
    if action == "lang_panel":
        await _open_language_panel(update, context)
        return True
    return False


async def _open_language_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = None
    if update.callback_query and update.callback_query.from_user:
        tg_id = str(update.callback_query.from_user.id)
    elif update.effective_user:
        tg_id = str(update.effective_user.id)
    if not tg_id:
        return

    username = None
    if update.effective_user:
        username = update.effective_user.username
    db = _load_db()
    u = _ensure_user(db, tg_id, username)
    current_lang = _get_user_report_lang(u)
    text = _bridge.t("language.panel", current_lang, label=_lang_label(current_lang))
    rows = _language_choice_rows(current_lang, lambda code: f"lang:user_set:{code}")
    rows.append([InlineKeyboardButton(_bridge.t("menu.header", current_lang), callback_data="main_menu:show")])
    markup = InlineKeyboardMarkup(rows)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=int(tg_id), text=text, parse_mode=ParseMode.HTML, reply_markup=markup)

def _usercard_kb(tg: str, lang: Optional[str] = None) -> InlineKeyboardMarkup:
    """Localised usercard keyboard for admin actions."""
    db = _load_db()
    u = _ensure_user(db, tg, None)
    lang = lang or _get_user_report_lang(_ensure_user(db, tg, None))
    contact_url = _tg_contact_url(u)
    contact_label = _bridge.t("usercard.buttons.contact", lang)
    if contact_url:
        contact_button = InlineKeyboardButton(contact_label, url=contact_url)
    else:
        contact_button = InlineKeyboardButton(contact_label, callback_data=f"user:contact:{tg}")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("usercard.buttons.monthly", lang), callback_data=f"ucard:monthly:{tg}")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.trial", lang), callback_data=f"ucard:trial:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.activate_custom", lang), callback_data=f"ucard:activate_custom:{tg}")],

        [InlineKeyboardButton(_bridge.t("usercard.buttons.quick_notify", lang), callback_data=f"ucard:quick_notify:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.balance_edit", lang), callback_data=f"ucard:balance_edit:{tg}")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.note", lang), callback_data=f"ucard:note:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.custom_name", lang), callback_data=f"ucard:set_name:{tg}")],

        [InlineKeyboardButton(_bridge.t("usercard.buttons.services", lang), callback_data=f"ucard:services:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.limits", lang), callback_data=f"ucard:limits:{tg}")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.report_lang", lang), callback_data=f"ucard:lang:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.audit", lang), callback_data=f"ucard:audit:{tg}")],

        [InlineKeyboardButton(_bridge.t("usercard.buttons.notify_user", lang), callback_data=f"ucard:notify:{tg}"),
         contact_button],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.disable", lang), callback_data=f"ucard:disable:{tg}"),
         InlineKeyboardButton(_bridge.t("usercard.buttons.delete", lang), callback_data=f"ucard:delete:{tg}")],

        [InlineKeyboardButton(_bridge.t("usercard.buttons.main_menu", lang), callback_data="main_menu:show")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.back_menu", lang), callback_data="main_menu:show")]
    ])

def _svc_kb(u: Dict[str, Any], lang: Optional[str] = None) -> InlineKeyboardMarkup:
    tg = u["tg_id"]
    s = u.get("services", {})
    lang = lang or _get_user_report_lang(u)

    def onoff(b):
        return "✅" if b else "⛔"

    carfax_label = _bridge.t("usercard.service.carfax", lang)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📄 {carfax_label} {onoff(s.get('carfax'))}", callback_data=f"svc:carfax:{tg}")],
        [InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.main_menu", lang), callback_data="main_menu:show")]
    ])

def _limits_kb(u: Dict[str, Any], lang: Optional[str] = None) -> InlineKeyboardMarkup:
    tg = u["tg_id"]
    lang = lang or _get_user_report_lang(u)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("limits.buttons.set_daily", lang), callback_data=f"limits:set_daily:{tg}")],
        [InlineKeyboardButton(_bridge.t("limits.buttons.set_monthly", lang), callback_data=f"limits:set_monthly:{tg}")],
        [InlineKeyboardButton(_bridge.t("limits.buttons.reset_today", lang), callback_data=f"limits:reset_today:{tg}")],
        [InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")],
        [InlineKeyboardButton(_bridge.t("usercard.buttons.main_menu", lang), callback_data="main_menu:show")]
    ])

def _render_usercard_text(u: Dict[str, Any], lang: Optional[str] = None) -> str:
    act = u.get("activation_date") or "-"
    exp = u.get("expiry_date") or "-"
    left = _days_left(u.get("expiry_date"))
    lang = lang or _get_user_report_lang(u)
    
    # تحسين عرض التواريخ
    act_display = _fmt_date(act) if act != "-" else "-"
    exp_display = _fmt_date(exp) if exp != "-" else "-"
    
    left_txt = ""
    if left is not None:
        if left > 0:
            left_txt = _bridge.t("usercard.left.days_remaining", lang, days=left)
        elif left == 0:
            left_txt = _bridge.t("usercard.left.today", lang)
        else:
            left_txt = _bridge.t("usercard.left.expired_days", lang, days=abs(left))
    
    s = u["services"]; lim = u["limits"]; st = u["stats"]
    monthly_limit = _safe_int(lim.get("monthly"))
    monthly_remaining = _remaining_monthly_reports(u)
    monthly_credit_label = (
        _bridge.t("usercard.unlimited", lang)
        if monthly_remaining is None
        else f"{monthly_remaining}/{monthly_limit}"
    )
    svc_line = _bridge.t(
        "usercard.services.line",
        lang,
        carfax="✅" if s.get("carfax") else "⛔",
    )
    limits_line = _bridge.t(
        "usercard.limits.line",
        lang,
        today_used=lim.get("today_used", 0),
        daily=lim.get("daily"),
        month_used=lim.get("month_used", 0),
        monthly=lim.get("monthly"),
    )
    
    # تحسين عرض الإحصائيات مع تاريخ آخر تقرير
    last_report = st.get('last_report_ts') or '-'
    if last_report != '-':
        try:
            # محاولة تنسيق التاريخ إذا كان بصيغة ISO
            if 'T' in last_report:
                dt = datetime.fromisoformat(last_report.replace('Z', '+00:00'))
                last_report = dt.strftime("%Y-%m-%d %H:%M")
        except:
            pass
    
    stats_line = _bridge.t("usercard.stats.line", lang, total=st.get("total_reports", 0), last=last_report)
    note_line = u.get("notes") or "—"
    if u.get("tg_username"):
        contact_line = _bridge.t("usercard.contact.username", lang, username=u.get("tg_username"))
    else:
        contact_line = _bridge.t("usercard.contact.id", lang, tg_id=u["tg_id"])
    if u.get("phone"):
        wa = u["phone"].lstrip("+")
        phone_line = _bridge.t("usercard.phone", lang, wa=wa, phone=u["phone"])
    else:
        phone_line = ""
    # لغة التقرير
    report_lang = _get_user_report_lang(u)
    lang_display = _lang_label(report_lang)
    parts: List[str] = [
        _bridge.t("usercard.header", lang),
        _bridge.t("usercard.name_line", lang, name=_display_name(u)),
        _bridge.t("usercard.tg_line", lang, tg=u["tg_id"], username=u.get("tg_username") or "—"),
        contact_line,
        phone_line,
        _bridge.t("usercard.plan_services", lang, plan=u.get("plan", "basic"), services=svc_line),
        _bridge.t("usercard.report_lang", lang, lang=lang_display),
        _bridge.t("usercard.sections.stats", lang),
        f"• {stats_line}\n",
        f"• {limits_line}\n\n",
        _bridge.t("usercard.sections.subscription", lang),
        f"• {_bridge.t('usercard.status.active', lang) if u.get('is_active') else _bridge.t('usercard.status.inactive', lang)}\n",
        _bridge.t("usercard.subscription.start", lang, start=act_display),
        _bridge.t("usercard.subscription.end", lang, end=exp_display, left=left_txt),
        _bridge.t("usercard.balance", lang, balance=monthly_credit_label),
        _bridge.t("usercard.note", lang, note=escape(note_line)),
    ]

    return "".join(parts)
# =================== PDF Helpers ===================


# =================== Carfax API ===================

async def _progress_updater(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    stop_event: asyncio.Event,
    *,
    state: Optional[Dict[str, Any]] = None,
    header: str = "",
):
    try:
        # Smooth progress feel without misleading 90%: cap at 80% until the
        # handler explicitly raises the cap when we start sending the PDF.
        p = 0
        last_sent_p: Optional[int] = None
        last_edit_ts = 0.0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                pass

            cap = 80
            try:
                if isinstance(state, dict) and state.get("cap") is not None:
                    cap = int(state.get("cap") or 80)
                else:
                    cap = int(context.chat_data.get("progress_cap", 80) or 80)
            except Exception:
                cap = 80
            cap = max(10, min(cap, 95))
            step = 5 if cap <= 80 else 3
            p = min(cap, p + step)

            # Avoid Telegram edit spam (can cause flicker/"blinking" and rate limits).
            # Only edit when progress changes, with an occasional heartbeat.
            now = time.perf_counter()
            if last_sent_p == p and (now - last_edit_ts) < 5.0:
                continue

            bar = _make_progress_bar(p)
            hdr = header or ""
            try:
                if isinstance(state, dict) and state.get("header"):
                    hdr = str(state.get("header") or hdr)
                elif context and isinstance(getattr(context, "chat_data", None), dict) and context.chat_data.get("progress_header"):
                    hdr = str(context.chat_data.get("progress_header") or hdr)
            except Exception:
                hdr = header or ""
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=hdr + "\n" + bar,
                    parse_mode=ParseMode.HTML,
                )
                last_sent_p = p
                last_edit_ts = now
            except Exception as exc:
                try:
                    logger.warning("progress_update_failed_html", extra={"err": str(exc)})
                except Exception:
                    pass
                try:
                    header_plain = re.sub(r"<[^>]+>", "", hdr or "")
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=header_plain + "\n" + bar,
                    )
                except Exception:
                    pass

                last_sent_p = p
                last_edit_ts = now
    finally:
        # Nothing to return; the loop exits naturally once stop_event is set.
        pass


async def _tg_progress_updater(
    context: ContextTypes.DEFAULT_TYPE,
    tg_id: str,
    vin: str,
    stop_event: asyncio.Event,
    *,
    state: Optional[Dict[str, Any]] = None,
    header: str = "",
):
    try:
        p = 0
        last_sent_p: Optional[int] = None
        last_edit_ts = 0.0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                pass

            cap = 80
            try:
                if isinstance(state, dict) and state.get("cap") is not None:
                    cap = int(state.get("cap") or 80)
                else:
                    cap = int(context.chat_data.get("progress_cap", 80) or 80)
            except Exception:
                cap = 80
            cap = max(10, min(cap, 95))
            step = 5 if cap <= 80 else 3
            p = min(cap, p + step)

            now = time.perf_counter()
            if last_sent_p == p and (now - last_edit_ts) < 5.0:
                continue

            bar = _make_progress_bar(p)
            hdr = header or ""
            try:
                if isinstance(state, dict) and state.get("header"):
                    hdr = str(state.get("header") or hdr)
                elif context and isinstance(getattr(context, "chat_data", None), dict) and context.chat_data.get("progress_header"):
                    hdr = str(context.chat_data.get("progress_header") or hdr)
            except Exception:
                hdr = header or ""

            try:
                await _tg_edit_inflight_messages(
                    context,
                    tg_id,
                    vin,
                    text=hdr + "\n" + bar,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            last_sent_p = p
            last_edit_ts = now
    finally:
        pass

# =================== Commands ===================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة ترحيبية مع تصميم بصري محسّن"""
    tg_id = str(update.effective_user.id)
    chat_state = context.chat_data if isinstance(context.chat_data, dict) else {}
    force_keyboard = _should_force_reply_keyboard(chat_state, ttl_seconds=0)
    await _ensure_main_reply_keyboard(update, context, force=force_keyboard, notice=None)

    bridge_user_ctx = _build_bridge_user_context(update, context)
    raw_payload = _bridge_raw_payload(update)
    welcome_text = None
    menu_response = None

    if bridge_user_ctx:
        incoming = _bridge.IncomingMessage(
            platform="telegram",
            user_id=bridge_user_ctx.user_id,
            text="/start",
            raw=raw_payload,
        )
        try:
            bridge_response = await _bridge.handle_text(bridge_user_ctx, incoming, context=context)
        except Exception:
            bridge_response = None
        if isinstance(bridge_response, _bridge.BridgeResponse) and bridge_response.messages:
            welcome_text = bridge_response.messages[0]
        try:
            menu_response = await _bridge.render_main_menu(bridge_user_ctx)
        except Exception:
            menu_response = None

    if not welcome_text:
        lang = _normalize_report_lang_code((bridge_user_ctx.language if bridge_user_ctx else None) or "ar")
        name = _bridge._infer_username(bridge_user_ctx) if bridge_user_ctx else (update.effective_user.first_name or "")
        welcome_text = _bridge.t("start.greeting", lang, name=name) + "\n\n" + _bridge.t("start.footer.telegram", lang)

    reply_markup = _bridge_menu_to_inline_keyboard(
        menu_response.actions.get("menu") if isinstance(menu_response, _bridge.BridgeResponse) else None,
        tg_id,
    ) if menu_response else build_main_menu(tg_id, lang)

    await _panel_message(
        update,
        context,
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )

    if bridge_response and isinstance(bridge_response, _bridge.BridgeResponse):
        extra_messages = bridge_response.messages[1:]
        if extra_messages:
            await _send_bridge_responses(update, extra_messages, context=context)

    if menu_response:
        await _send_bridge_responses(update, menu_response, context=context)

async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض بيانات المستخدم بتصميم بصري محسّن"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    _bump_usage(u); _save_db(db)
    lang = (u.get("language") or "ar").lower()
    text = _bridge._compose_profile_overview(u, lang)

    kb_rows: List[List[InlineKeyboardButton]] = []
    add_phone_label = _bridge.t("profile.add_phone", lang)
    back_label = _bridge.t("common.main_menu", lang)

    phone = (u.get("phone") or "").strip()
    if not phone:
        kb_rows.append([InlineKeyboardButton(add_phone_label, callback_data="user:phone:open")])
    kb_rows.append([InlineKeyboardButton(back_label, callback_data="main_menu:show")])
    await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows))

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض الرصيد بتصميم بصري محسّن"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    _bump_usage(u); _save_db(db)
    lang = (u.get("language") or "ar").lower()
    text = _bridge._compose_balance_overview(u, lang)

    back_label = _bridge.t("common.main_menu", lang)

    await _panel_message(
        update,
        context,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(back_label, callback_data="main_menu:show")]]),
    )

def _refresh_env() -> bool:
    try:
        _reload_env()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Error reloading env: %s", exc)
        return False
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Localized debug command for permissions and environment summary."""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None)
    lang = _get_user_report_lang(u)

    env_reloaded = _refresh_env()
    env_admins = _env_super_admins()
    db_admins = _db_super_admins(db)
    is_super = _is_super_admin(tg_id)
    is_admin = _is_admin_tg(tg_id)
    is_ultimate = _is_ultimate_super(tg_id)

    yes_text = _bridge.t("common.status.yes", lang)
    no_text = _bridge.t("common.status.no", lang)
    def yn(flag: bool) -> str:
        return yes_text if flag else no_text

    env_supers_val = ", ".join(env_admins) if env_admins else _bridge.t("common.unset", lang)
    db_admins_val = ", ".join(db_admins) if db_admins else _bridge.t("common.unset", lang)
    bot_token_val = _bridge.t("common.set", lang) if BOT_TOKEN else _bridge.t("common.unset", lang)
    username_val = f"@{update.effective_user.username}" if update and update.effective_user and update.effective_user.username else _bridge.t("common.unavailable", lang)

    lines = [
        _bridge.t("admin.debug.title", lang),
        _bridge.t("admin.debug.user_id", lang, tg_id=tg_id),
        _bridge.t("admin.debug.username", lang, username=username_val),
        "",
        _bridge.t("admin.debug.roles.header", lang),
        _bridge.t("admin.debug.roles.super", lang, value=yn(is_super)),
        _bridge.t("admin.debug.roles.admin", lang, value=yn(is_admin)),
        _bridge.t("admin.debug.roles.ultimate", lang, value=yn(is_ultimate)),
        "",
        _bridge.t("admin.debug.env.header", lang),
        _bridge.t("admin.debug.env.telegram_supers", lang, env_supers=env_supers_val),
        _bridge.t("admin.debug.env.dotenv_loaded", lang, value=yn(env_reloaded)),
        _bridge.t("admin.debug.env.bot_token", lang, value=bot_token_val),
        _bridge.t("admin.debug.env.db_path", lang, value=DB_PATH),
        "",
        _bridge.t("admin.debug.env.supers_env", lang, env_admins=env_supers_val),
        _bridge.t("admin.debug.env.supers_db", lang, db_admins=db_admins_val),
        "",
        _bridge.t("admin.debug.tip", lang),
    ]

    text = "\n".join(lines)
    await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id, lang))

# قائمة المستخدمين (مكررة - تم دمجها مع _users_keyboard أعلاه)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await _panel_message(update, context, _unauthorized(lang), parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
    context.user_data["last_users_page"] = 0
    users_panel_text = _bridge.t("users.panel.header", lang)
    return await _panel_message(update, context, users_panel_text, parse_mode=ParseMode.HTML, reply_markup=_users_keyboard(db, 0, 8, lang))
async def users_pager_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = str(q.from_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, q.from_user.username if q and q.from_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await q.edit_message_text(_unauthorized(lang), parse_mode=ParseMode.HTML)
    try:
        _, _, page = q.data.split(":")
        page = int(page)
    except Exception:
        page = 0
    db = db or _load_db()
    context.user_data["last_users_page"] = page
    users_panel_text = _bridge.t("users.panel.header", lang)
    return await q.edit_message_text(users_panel_text, parse_mode=ParseMode.HTML, reply_markup=_users_keyboard(db, page, 8, lang))

def _collect_stats(db: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    users = list(db.get("users", {}).values())
    users.sort(
        key=lambda x: (
            not x.get("is_active", False),
            (x.get("custom_name") or x.get("tg_username") or str(x.get("tg_id") or "")),
        )
    )

    today = date.today()
    stats = {
        "total": len(users),
        "active": sum(1 for u in users if u.get("is_active")),
        "pending": len(db.get("activation_requests", [])),
        "expiring_soon": 0,
        "expired": 0,
        "total_balance": 0,
        "total_reports": 0,
    }
    stats["inactive"] = stats["total"] - stats["active"]

    for u in users:
        stats["total_balance"] += max(0, _current_balance(u))
        stats["total_reports"] += u.get("stats", {}).get("total_reports", 0)
        exp = u.get("expiry_date")
        if not exp:
            continue
        try:
            exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
            days_left = (exp_d - today).days
            if 0 < days_left <= 7:
                stats["expiring_soon"] += 1
            elif days_left <= 0:
                stats["expired"] += 1
        except Exception:
            continue

    return users, stats

def _format_stats_header(stats: Dict[str, int]) -> str:
    return (
        "📊 <b>الإحصائيات العامة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>👥 المستخدمون:</b>\n"
        f"• إجمالي: <b>{stats['total']}</b>\n"
        f"• مفعلون: <b>{stats['active']}</b>\n"
        f"• غير مفعلين: <b>{stats['inactive']}</b>\n"
        f"• طلبات بانتظار: <b>{stats['pending']}</b>\n\n"
        "<b>⏰ الاشتراكات:</b>\n"
        f"• تنتهي قريباً (≤7 أيام): <b>{stats['expiring_soon']}</b>\n"
        f"• منتهية: <b>{stats['expired']}</b>\n\n"
        "<b>💰 الأرصدة الشهرية:</b>\n"
        f"• إجمالي الرصيد المتبقي: <b>{stats['total_balance']}</b>\n"
        f"• إجمالي التقارير: <b>{stats['total_reports']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>المستخدمون:</b>"
    )

def _stats_keyboard(users: List[Dict[str, Any]], page: int = 0, per_page: int = 8, selected_ids: Optional[set] = None) -> Tuple[InlineKeyboardMarkup, int, int]:
    total = len(users)
    per_page = max(1, per_page)
    if total == 0:
        rows = [
            [InlineKeyboardButton("لا يوجد مستخدمون حالياً", callback_data="stats:none")],
            [InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu:show")],
        ]
        return InlineKeyboardMarkup(rows), 0, 0

    max_page = max(0, (total - 1) // per_page)
    page = max(0, min(page, max_page))
    start = page * per_page
    chunk = users[start : start + per_page]

    if selected_ids is None:
        selected_ids = set()

    rows: List[List[InlineKeyboardButton]] = []
    # صفوف الإجراءات الجماعية
    rows.append([
        InlineKeyboardButton("🟢 تفعيل الكل", callback_data="stats:bulk:activate_all"),
        InlineKeyboardButton("⛔ تعطيل الكل", callback_data="stats:bulk:deactivate_all"),
    ])
    rows.append([
        InlineKeyboardButton("🟢 تفعيل المحدد", callback_data=f"stats:bulk:activate_selected"),
        InlineKeyboardButton("⛔ تعطيل المحدد", callback_data=f"stats:bulk:deactivate_selected"),
    ])
    rows.append([
        InlineKeyboardButton("☑️ تحديد الكل (صفحة)", callback_data=f"stats:select_all_page:{page}"),
        InlineKeyboardButton("⬜ إلغاء الكل (صفحة)", callback_data=f"stats:deselect_all_page:{page}"),
    ])
    for u in chunk:
        tg_raw = u.get("tg_id")
        tg = str(tg_raw).strip() if tg_raw is not None else ""
        if not tg:
            continue

        status = "✅" if u.get("is_active") else "⛔"
        username = (u.get("tg_username") or "").strip().lstrip("@")
        display_name = (
            u.get("custom_name")
            or (f"@{username}" if username else (u.get("full_name") or u.get("name") or f"TG:{tg}"))
        )
        if len(display_name) > 22:
            display_name = display_name[:19] + "..."

        ph = u.get("phone")
        row = [InlineKeyboardButton(f"{status} {display_name}", callback_data=f"ucard:open:{tg}")]
        if username:
            row.append(InlineKeyboardButton("✉️ مراسلة", url=f"https://t.me/{username}"))
        else:
            row.append(InlineKeyboardButton("✉️ إشعار سريع", callback_data=f"ucard:quick_notify:{tg}"))
        if ph:
            row.append(InlineKeyboardButton("📞 واتساب", url=f"https://wa.me/{ph.lstrip('+')}"))
        # زر تحديد للمستخدم
        is_sel = tg in selected_ids
        sel_label = "☑️ محدد" if is_sel else "⬜ تحديد"
        row.append(InlineKeyboardButton(sel_label, callback_data=f"stats:select:toggle:{tg}:{page}"))

        rows.append(row)

    if not rows:
        rows.append([InlineKeyboardButton("لا يوجد بيانات في هذه الصفحة", callback_data="stats:none")])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("« السابق", callback_data=f"stats:page:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton("التالي »", callback_data=f"stats:page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu:show")])

    return InlineKeyboardMarkup(rows), page, max_page
async def admin_stats_command(update: Update, context):
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await _panel_message(update, context, _unauthorized(lang), parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))

    users, stats_data = _collect_stats(db)
    header = _format_stats_header(stats_data)
    selected = set(context.user_data.get("stats_selected", []))
    markup, current_page, max_page = _stats_keyboard(users, 0, 8, selected)

    if max_page > 0:
        header += f"\n\n📄 الصفحة: <b>{current_page + 1}</b> / <b>{max_page + 1}</b>"

    await _panel_message(update, context, header, parse_mode=ParseMode.HTML, reply_markup=markup)

def _pending_keyboard(db: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for req in db.get("activation_requests", []):
        tg = str(req.get("tg_id"))
        user = _ensure_user(db, tg, None)
        phone = req.get("phone") or user.get("phone") or "—"
        contact_url = _tg_contact_url(user)
        if contact_url:
            phone_btn = InlineKeyboardButton(f"📞 {phone}", url=contact_url)
        else:
            phone_btn = InlineKeyboardButton(f"📞 {phone}", callback_data=f"pending:phone:{tg}")
        rows.append([
            phone_btn,
            InlineKeyboardButton("🧾 تفعيل مخصّص", callback_data=f"pending:activate_custom:{tg}"),
            InlineKeyboardButton("⛔ رفض", callback_data=f"pending:deny:{tg}")
        ])
    if not rows:
        rows = [[InlineKeyboardButton("لا توجد طلبات حالياً", callback_data="pending:none")]]
    # إضافة زر رجوع
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu:show")])
    return InlineKeyboardMarkup(rows)


def _pending_quick_actions_kb(db: Dict[str, Any], tg_id: str) -> InlineKeyboardMarkup:
    trial = _resolve_activation_preset(db, "trial")
    monthly = _resolve_activation_preset(db, "monthly")
    user = _ensure_user(db, tg_id, None)
    limits = user.get("limits", {}) if user else {}
    daily_current = _safe_int(limits.get("daily"), monthly["daily"])
    monthly_current = _safe_int(limits.get("monthly"), monthly["monthly"])
    trial_label = f"🧪 تجربة {trial['days']} يوم — {trial['daily']} تقرير يومي"
    monthly_label = (
        f"🟢 تفعيل {monthly['days']} يوم — حد يومي {monthly['daily']} / عدد التقارير {monthly['monthly']}"
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(trial_label, callback_data=f"pending:trial:{tg_id}"),
            InlineKeyboardButton(monthly_label, callback_data=f"pending:monthly:{tg_id}"),
            InlineKeyboardButton("🧾 تفعيل مخصّص", callback_data=f"pending:activate_custom:{tg_id}"),
        ],
        [
            InlineKeyboardButton(f"📅 حد يومي ({daily_current})", callback_data=f"limits:set_daily:{tg_id}"),
            InlineKeyboardButton(f"📊 عدد التقارير ({monthly_current})", callback_data=f"limits:set_monthly:{tg_id}"),
        ],
        [
            InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"pending:open:{tg_id}"),
            InlineKeyboardButton("⛔ رفض", callback_data=f"pending:deny:{tg_id}"),
        ],
    ])

# =================== Users list helpers ===================

def _auto_suspend_if_expired(u: Dict[str, Any]) -> bool:
    exp_raw = u.get("expiry_date")
    if not exp_raw:
        return False
    try:
        exp_date = datetime.strptime(exp_raw, "%Y-%m-%d").date()
    except Exception:
        return False
    if exp_date < date.today() and u.get("is_active"):
        u["is_active"] = False
        return True
    return False


def _user_status_meta(u: Dict[str, Any]) -> Tuple[str, str]:
    limits = u.get("limits", {}) or {}
    daily = _safe_int(limits.get("daily"))
    monthly = _safe_int(limits.get("monthly"))
    today_used = _safe_int(limits.get("today_used"))
    month_used = _safe_int(limits.get("month_used"))
    if (daily and today_used >= daily) or (monthly and month_used >= monthly):
        return ("📈 وصل الحد", "limit")
    exp_raw = u.get("expiry_date")
    expired = False
    if exp_raw:
        try:
            exp_date = datetime.strptime(exp_raw, "%Y-%m-%d").date()
            expired = exp_date < date.today()
        except Exception:
            expired = False
    if not u.get("is_active") or expired:
        return ("⛔ متوقف", "stopped")
    return ("🟢 فعال", "active")


def _auto_notice_store(u: Dict[str, Any]) -> Dict[str, Any]:
    return u.setdefault("last_auto_notifications", {})


async def _notify_usage_caps_if_needed(context: ContextTypes.DEFAULT_TYPE, u: Dict[str, Any]) -> None:
    if not context:
        return
    tg = str(u.get("tg_id") or "").strip()
    if not tg:
        return
    limits = u.get("limits", {}) or {}
    today_used = _safe_int(limits.get("today_used"))
    month_used = _safe_int(limits.get("month_used"))
    daily_limit = _safe_int(limits.get("daily"))
    monthly_limit = _safe_int(limits.get("monthly"))
    lang = _get_user_report_lang(u)
    store = _auto_notice_store(u)
    today_stamp = date.today().strftime("%Y-%m-%d")
    month_stamp = date.today().strftime("%Y-%m")

    if daily_limit and today_used >= daily_limit:
        key = "limit_daily_hit"
        if store.get(key) != today_stamp:
            store[key] = today_stamp
            await _notify_user(
                context,
                tg,
                _bridge.t("limits.hit.daily.user", lang, used=today_used, limit=daily_limit),
            )
        super_key = "limit_daily_hit_super"
        if store.get(super_key) != today_stamp:
            store[super_key] = today_stamp
            user_label = _fmt_tg_with_phone(tg)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(_bridge.t("limits.buttons.reset_today", "ar"), callback_data=f"limits:reset_today:{tg}")]
            ])
            await _notify_supers(
                context,
                _bridge.t("limits.super.daily_hit", "ar", user=user_label, used=today_used, limit=daily_limit),
                kb,
            )

    if monthly_limit and month_used >= monthly_limit:
        key = "limit_monthly_hit"
        if store.get(key) != month_stamp:
            store[key] = month_stamp
            await _notify_user(
                context,
                tg,
                _bridge.t("limits.hit.monthly.user", lang, used=month_used, limit=monthly_limit),
            )

async def pending_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await _panel_message(update, context, _unauthorized(lang), parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
    await _panel_message(update, context, _bridge.t("pending.list.title", lang), parse_mode=ParseMode.HTML, reply_markup=_pending_keyboard(db))

# =================== نظام الإشعارات بالأزرار ===================

def _broadcast_keyboard() -> InlineKeyboardMarkup:
    """لوحة مفاتيح اختيار نوع الإشعار"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 للجميع", callback_data="broadcast:all"),
            InlineKeyboardButton("🔢 عدد محدد", callback_data="broadcast:count")
        ],
        [
            InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")
        ]
    ])

def _broadcast_users_keyboard(db: Dict[str, Any], page: int = 0, per_page: int = 10, selected_users: set = None) -> InlineKeyboardMarkup:
    """لوحة مفاتيح اختيار المستخدمين للإشعار"""
    if selected_users is None:
        selected_users = set()
    
    users = list(db.get("users", {}).values())
    users.sort(key=lambda x: (not x.get("is_active"), x.get("custom_name") or x.get("tg_username") or x.get("tg_id")))
    
    start = page * per_page
    end = start + per_page
    chunk = users[start:end]
    
    rows: List[List[InlineKeyboardButton]] = []
    
    # عرض المستخدمين مع checkboxes
    for user in chunk:
        tg_id = user.get("tg_id")
        if not tg_id:
            continue
        
        status = "✅" if user.get("is_active") else "⛔"
        name = user.get("custom_name") or (("@" + user.get("tg_username")) if user.get("tg_username") else f"TG:{tg_id}")
        
        # تقصير الاسم إذا كان طويلاً
        if len(name) > 20:
            name = name[:17] + "..."
        
        # إضافة علامة ✓ إذا كان مختاراً
        is_selected = str(tg_id) in selected_users
        prefix = "☑️" if is_selected else "☐"
        
        rows.append([
            InlineKeyboardButton(
                f"{prefix} {status} {name}",
                callback_data=f"broadcast:toggle:{tg_id}"
            )
        ])
    
    # أزرار التنقل
    nav: List[InlineKeyboardButton] = []
    if start > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"broadcast:users_page:{page-1}"))
    if end < len(users):
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"broadcast:users_page:{page+1}"))
    if nav:
        rows.append(nav)
    
    # أزرار الإجراءات
    action_rows = []
    if selected_users:
        action_rows.append([
            InlineKeyboardButton(f"✅ إرسال للمختارين ({len(selected_users)})", callback_data="broadcast:send_selected")
        ])
    action_rows.append([
        InlineKeyboardButton("🔄 تحديد الكل", callback_data="broadcast:select_all"),
        InlineKeyboardButton("🔄 إلغاء الكل", callback_data="broadcast:deselect_all")
    ])
    action_rows.append([
        InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")
    ])
    rows.extend(action_rows)
    return InlineKeyboardMarkup(rows)

async def broadcast_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج زر الإشعارات من القائمة الرئيسية"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await _panel_message(
            update,
            context,
            _bridge.t("common.unauthorized", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(tg_id)
        )
    
    db = _load_db()
    all_users = list(db.get("users", {}).values())
    total_users = len(all_users)
    
    text = _bridge.t("broadcast.panel.intro", lang, total=total_users)
    await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_keyboard())

async def broadcast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج callback للإشعارات"""
    q = update.callback_query
    await q.answer()
    
    tg_id = str(q.from_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, q.from_user.username if q and q.from_user else None)
    lang = _get_user_report_lang(u)
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return await q.edit_message_text(_bridge.t("common.unauthorized", lang), parse_mode=ParseMode.HTML)
    
    try:
        _, action = q.data.split(":", 1)
    except ValueError:
        return await q.edit_message_text(_bridge.t("common.invalid_data", lang), parse_mode=ParseMode.HTML)
    
    if action == "cancel" or action == "back":
        # العودة للقائمة الرئيسية
        return await q.delete_message()
    
    if action == "all":
        context.user_data["broadcast"] = {"type": "all"}
        text = _bridge.t("broadcast.send_all.prompt", lang)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    
    if action == "count":
        # عرض قائمة المستخدمين للاختيار
        db = _load_db()
        all_users = list(db.get("users", {}).values())
        total_users = len(all_users)
        
        # تهيئة قائمة المستخدمين المختارين
        if "broadcast" not in context.user_data:
            context.user_data["broadcast"] = {"type": "selected", "selected_users": []}
        elif "selected_users" not in context.user_data["broadcast"]:
            context.user_data["broadcast"]["selected_users"] = []
        
        # تحويل list إلى set للاستخدام
        selected_users = set(context.user_data["broadcast"]["selected_users"])
        
        text = _bridge.t("broadcast.select.title", lang, total=total_users, selected=len(selected_users))
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_users_keyboard(db, 0, 10, selected_users))
        return
    
    if action.startswith("toggle:"):
        # تبديل اختيار مستخدم
        try:
            user_tg_id = action.split(":")[1]
            db = _load_db()
            
            if "broadcast" not in context.user_data:
                context.user_data["broadcast"] = {"type": "selected", "selected_users": []}
            
            selected_users = set(context.user_data["broadcast"].get("selected_users", []))
            
            # تبديل الاختيار
            if user_tg_id in selected_users:
                selected_users.remove(user_tg_id)
            else:
                selected_users.add(user_tg_id)
            
            # حفظ كـ list
            context.user_data["broadcast"]["selected_users"] = list(selected_users)
            
            # الحصول على رقم الصفحة الحالية
            current_page = context.user_data.get("broadcast_page", 0)
            
            # تحديث القائمة
            all_users = list(db.get("users", {}).values())
            total_users = len(all_users)
            text = _bridge.t("broadcast.select.title", lang, total=total_users, selected=len(selected_users))
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_users_keyboard(db, current_page, 10, selected_users))
        except Exception as e:
            await q.answer(_bridge.t("broadcast.error.toggle", lang), show_alert=True)
        return
    
    if action.startswith("users_page:"):
        # التنقل بين الصفحات
        try:
            page = int(action.split(":")[1])
            context.user_data["broadcast_page"] = page
            db = _load_db()
            
            selected_users = set(context.user_data.get("broadcast", {}).get("selected_users", []))
            
            all_users = list(db.get("users", {}).values())
            total_users = len(all_users)
            text = _bridge.t("broadcast.select.title", lang, total=total_users, selected=len(selected_users))
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_users_keyboard(db, page, 10, selected_users))
        except Exception:
            await q.answer(_bridge.t("broadcast.error.page", lang), show_alert=True)
        return
    
    if action == "select_all":
        # تحديد جميع المستخدمين
        db = _load_db()
        all_users = list(db.get("users", {}).values())
        selected_users = {str(u.get("tg_id")) for u in all_users if u.get("tg_id")}
        
        if "broadcast" not in context.user_data:
            context.user_data["broadcast"] = {"type": "selected", "selected_users": []}
        context.user_data["broadcast"]["selected_users"] = list(selected_users)
        
        current_page = context.user_data.get("broadcast_page", 0)
        total_users = len(all_users)
        text = _bridge.t("broadcast.select.title", lang, total=total_users, selected=len(selected_users)) + "\n" + _bridge.t("broadcast.select.all_selected", lang)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_users_keyboard(db, current_page, 10, selected_users))
        return
    
    if action == "deselect_all":
        # إلغاء تحديد جميع المستخدمين
        if "broadcast" in context.user_data:
            context.user_data["broadcast"]["selected_users"] = []
        
        db = _load_db()
        current_page = context.user_data.get("broadcast_page", 0)
        all_users = list(db.get("users", {}).values())
        total_users = len(all_users)
        selected_users = set()
        
        text = _bridge.t("broadcast.select.title", lang, total=total_users, selected=len(selected_users)) + "\n" + _bridge.t("broadcast.select.cleared", lang)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_broadcast_users_keyboard(db, current_page, 10, selected_users))
        return
    
    if action == "send_selected":
        # الانتقال لإدخال الرسالة
        selected_users = context.user_data.get("broadcast", {}).get("selected_users", [])
        if not selected_users:
            await q.answer(_bridge.t("broadcast.error.none_selected", lang), show_alert=True)
            return
        
        context.user_data["broadcast"]["type"] = "selected"
        text = _bridge.t("broadcast.send_selected.prompt", lang, count=len(selected_users))
        await q.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    
    await q.answer(_bridge.t("common.invalid_data", lang), show_alert=True)

async def broadcast_send_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج إرسال الإشعارات بعد إدخال الرسالة"""
    tg_id = str(update.effective_user.id)
    admin_lang = _get_user_report_lang(_ensure_user(_load_db(), tg_id, update.effective_user.username if update.effective_user else None))
    if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
        return
    
    broadcast_data = context.user_data.get("broadcast")
    if not broadcast_data:
        return
    
    message_text = (update.message.text or "").strip()
    if not message_text:
        await update.message.reply_text(_bridge.t("broadcast.error.empty_message", admin_lang), parse_mode=ParseMode.HTML)
        return
    
    db = _load_db()
    all_users = list(db.get("users", {}).values())
    
    if not all_users:
        await update.message.reply_text(_bridge.t("broadcast.error.no_users", admin_lang), parse_mode=ParseMode.HTML)
        context.user_data.pop("broadcast", None)
        return
    
    # تحديد قائمة المستخدمين
    broadcast_type = broadcast_data.get("type")
    target_users = []
    
    if broadcast_type == "all":
        target_users = all_users
        status_text = _bridge.t("broadcast.status.all", admin_lang, count=len(target_users))
    elif broadcast_type == "selected":
        # استخدام المستخدمين المختارين
        selected_user_ids = set(broadcast_data.get("selected_users", []))
        if not selected_user_ids:
            await update.message.reply_text(_bridge.t("broadcast.error.none_selected", admin_lang), parse_mode=ParseMode.HTML)
            context.user_data.pop("broadcast", None)
            return
        
        # تصفية المستخدمين المختارين
        target_users = [u for u in all_users if str(u.get("tg_id")) in selected_user_ids]
        status_text = _bridge.t("broadcast.status.selected", admin_lang, count=len(target_users))
    else:
        await update.message.reply_text(_bridge.t("broadcast.error.type", admin_lang), parse_mode=ParseMode.HTML)
        context.user_data.pop("broadcast", None)
        return
    
    # إرسال رسالة الحالة
    status_msg = await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)
    
    # إرسال الرسائل
    success_count = 0
    failed_count = 0
    failed_users = []
    
    # إضافة تنسيق للرسالة
    formatted_message = _bridge.t("broadcast.message.header", admin_lang, body=message_text)
    
    for user in target_users:
        user_tg_id = user.get("tg_id")
        if not user_tg_id:
            failed_count += 1
            continue
        
        if await _notify_user(context, user_tg_id, formatted_message):
            success_count += 1
        else:
            failed_count += 1
            user_name = user.get("custom_name") or user.get("tg_username") or user_tg_id
            failed_users.append(f"• {user_name} ({user_tg_id})")
            
        # تجنب الإفراط في الإرسال
        await asyncio.sleep(0.05)
    
    # إرسال تقرير النتائج
    result_text = _bridge.t(
        "broadcast.result.summary",
        admin_lang,
        success=success_count,
        failed=failed_count,
        total=len(target_users),
    )
    
    if failed_users and len(failed_users) <= 10:
        result_text += "\n" + _bridge.t("broadcast.result.failed_list", admin_lang, users="\n".join(failed_users[:10]))
    elif failed_count > 0:
        result_text += "\n" + _bridge.t("broadcast.result.failed_count", admin_lang, count=failed_count)
    
    try:
        await status_msg.edit_text(result_text, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(result_text, parse_mode=ParseMode.HTML)
    
    # مسح بيانات الإشعار
    context.user_data.pop("broadcast", None)

async def pending_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    admin_tg = str(q.from_user.id)
    db = _load_db()
    u_admin = _ensure_user(db, admin_tg, q.from_user.username if q and q.from_user else None)
    lang = _get_user_report_lang(u_admin)
    if not (_is_admin_tg(admin_tg) or _is_super_admin(admin_tg)):
        return await q.edit_message_text(_bridge.t("common.unauthorized", lang), parse_mode=ParseMode.HTML)

    parts = q.data.split(":")
    if len(parts) < 2:
        return await q.edit_message_text("زر غير صالح.", parse_mode=ParseMode.HTML)
    action = parts[1]
    target_tg = parts[2] if len(parts) > 2 else None

    db = _load_db()
    u = _ensure_user(db, target_tg, None) if target_tg else None

    if action == "list":
        # Return to pending list
        return await q.edit_message_text(
            _bridge.t("pending.list.title", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=_pending_keyboard(db),
        )

    if action == "phone" and target_tg:
        phone = None
        platform = None
        for req in db.get("activation_requests", []):
            if str(req.get("tg_id")) == target_tg:
                phone = req.get("phone")
                platform = req.get("platform")
                break
        phone = phone or (u.get("phone") if u else None) or "—"
        platform_label = (platform or "unknown").upper()

        preset_trial = _resolve_activation_preset(db, "trial")
        preset_monthly = _resolve_activation_preset(db, "monthly")

        card_text = (
            "🛂 <b>بطاقة طلب التفعيل</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• المستخدم: <code>{escape(str(target_tg))}</code>\n"
            f"• الرقم: <code>{escape(str(phone))}</code>\n"
            f"• المنصة: <b>{escape(platform_label)}</b>\n\n"
            "اختر نوع التفعيل:\n"
        )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"🧪 تجربة {preset_trial['days']} يوم — {preset_trial['daily']}/{preset_trial['monthly']}",
                    callback_data=f"pending:trial:{target_tg}",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🟢 شهري {preset_monthly['days']} يوم — {preset_monthly['daily']}/{preset_monthly['monthly']}",
                    callback_data=f"pending:monthly:{target_tg}",
                )
            ],
            [
                InlineKeyboardButton("🧾 تفعيل مخصّص", callback_data=f"pending:activate_custom:{target_tg}"),
                InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"pending:open:{target_tg}"),
            ],
            [
                InlineKeyboardButton("⛔ رفض", callback_data=f"pending:deny:{target_tg}"),
                InlineKeyboardButton("↩️ رجوع", callback_data="pending:list"),
            ],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu:show")],
        ])

        return await q.edit_message_text(card_text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # فتح بطاقة
    if action == "open" and target_tg:
        return await _render_usercard(update, context, target_tg)

    # اشتراك شهري
    if action == "monthly":
        first_activation = not bool(u.get("activation_date"))
        phone, platform = _activation_request_info(db, target_tg, u)
        is_whatsapp = _is_probable_whatsapp_user(target_tg=target_tg, user=u, platform=platform, phone=phone)

        _remove_pending_request(db, target_tg)
        preset = _resolve_activation_preset(db, "monthly")
        days = preset["days"]
        daily_limit = preset["daily"]
        monthly_limit = preset["monthly"]
        today = datetime.utcnow().date()
        exp = u.get("expiry_date")
        if exp:
            try:
                exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
            except Exception:
                exp_d = today
        else:
            exp_d = today
        base = max(today, exp_d)
        u["expiry_date"] = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        u["is_active"] = True
        u["plan"] = "monthly"
        _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
        if not u.get("activation_date"):
            u["activation_date"] = today.strftime("%Y-%m-%d")
        if first_activation:
            _set_user_report_lang(u, "en")
        _audit(u, admin_tg, "monthly_activate_from_pending", add_days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
        _save_db(db)

        activation_msg = (
            f"🟢 تم تفعيل اشتراكك لمدة <b>{days}</b> يوم.\n"
            f"• حد يومي: <b>{daily_limit}</b> تقرير\n"
            f"• حد شهري: <b>{monthly_limit}</b> تقرير\n"
            f"• تاريخ الانتهاء: <code>{u['expiry_date']}</code>"
        )
        await _notify_user(context, target_tg, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
        await _post_activation_admin_notice_if_needed(
            context,
            db=db,
            user=u,
            target_tg=target_tg,
            first_activation=first_activation,
            is_whatsapp=is_whatsapp,
        )
        await _notify_supers(
            context,
            f"🟢 (Admin:{admin_tg}) فعّل اشتراك {days} يوم ({daily_limit}/{monthly_limit}) للمستخدم {target_tg}."
        )
        return await q.edit_message_text(
            f"✅ تم التفعيل ({days}يوم — حد {daily_limit}/{monthly_limit}).",
            parse_mode=ParseMode.HTML,
        )

    # تجربة مجانية
    if action == "trial":
        first_activation = not bool(u.get("activation_date"))
        phone, platform = _activation_request_info(db, target_tg, u)
        is_whatsapp = _is_probable_whatsapp_user(target_tg=target_tg, user=u, platform=platform, phone=phone)

        _remove_pending_request(db, target_tg)
        preset = _resolve_activation_preset(db, "trial")
        days = preset["days"]
        daily_limit = preset["daily"]
        monthly_limit = preset["monthly"]
        today = datetime.utcnow().date()
        u["is_active"] = True
        u["plan"] = "trial"
        u["activation_date"] = today.strftime("%Y-%m-%d")
        u["expiry_date"] = (today + timedelta(days=days)).strftime("%Y-%m-%d")
        _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
        if first_activation:
            _set_user_report_lang(u, "en")
        _audit(u, admin_tg, "trial_activate", days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
        _save_db(db)

        activation_msg = (
            f"🧪 تم تفعيل تجربة لمدة <b>{days}</b> يوم.\n"
            f"• الحد الأقصى اليومي: <b>{daily_limit}</b> تقرير\n"
            f"• الحد الشهري: <b>{monthly_limit}</b> تقرير\n"
            f"• ينتهي في: <code>{u['expiry_date']}</code>"
        )
        await _notify_user(context, target_tg, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
        await _post_activation_admin_notice_if_needed(
            context,
            db=db,
            user=u,
            target_tg=target_tg,
            first_activation=first_activation,
            is_whatsapp=is_whatsapp,
        )
        await _notify_supers(
            context,
            f"🧪 (Admin:{admin_tg}) فعّل تجربة {days} يوم ({daily_limit}/{monthly_limit}) للمستخدم {target_tg}.",
        )
        return await q.edit_message_text(
            f"✅ تمت الموافقة (تجربة {days}يوم — حد {daily_limit}/{monthly_limit}).",
            parse_mode=ParseMode.HTML,
        )

    # تفعيل مخصّص (نطلب الإدخال)
    if action == "activate_custom":
        context.user_data["await"] = {"op": "activate_custom", "target": target_tg, "from": "pending"}
        return await q.edit_message_text(
            "🧾 أرسل: <b>أيام,حد_يومي,عدد_التقارير[,تقارير_إضافية]</b> مثال <code>30,25,500</code>",
            parse_mode=ParseMode.HTML,
        )

    # رفض
    if action == "deny":
        _remove_pending_request(db, target_tg)
        _save_db(db)
        target_lang = _get_user_report_lang(_ensure_user(db, target_tg, None))
        await _notify_user(context, target_tg, _bridge.t("pending.denied.user", target_lang))
        await _notify_supers(context, f"⛔ (Admin:{admin_tg}) رفض طلب {target_tg}.")
        return await q.edit_message_text("⛔ تم الرفض.", parse_mode=ParseMode.HTML)

# =================== Usercard Rendering / Actions ===================
async def _render_usercard(update: Update, context: ContextTypes.DEFAULT_TYPE, target_tg: str):
    db = _load_db()
    u = _ensure_user(db, target_tg, None)
    _bump_usage(u); _save_db(db)
    admin_lang = "ar"
    if update.effective_user:
        admin_lang = _get_user_report_lang(_ensure_user(db, str(update.effective_user.id), update.effective_user.username))
    text = _render_usercard_text(u, admin_lang)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_usercard_kb(target_tg, admin_lang))
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_usercard_kb(target_tg, admin_lang))
async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    caller_tg = str(q.from_user.id)
    lang = _get_user_report_lang(_ensure_user(_load_db(), caller_tg, q.from_user.username if q and q.from_user else None))
    try:
        parts = q.data.split(":")
        action = parts[1] if len(parts) > 1 else "show"
    except Exception:
        await q.answer(_bridge.t("common.invalid_data", lang), show_alert=True)
        return

    tg_id = caller_tg
    lang = _get_user_report_lang(_ensure_user(_load_db(), tg_id, q.from_user.username if q and q.from_user else None))

    chat_state = context.chat_data if isinstance(context.chat_data, dict) else {}
    raw_payload = _bridge_raw_payload(update)
    bridge_user_ctx = _build_bridge_user_context(update, context)
    legacy_map = {
        "whoami": "profile",
        "request_activation": "activation",
        "lang": "language",
    }
    action = legacy_map.get(action, action)

    if action == "show":
        force_keyboard = _should_force_reply_keyboard(chat_state, ttl_seconds=0)
        await _ensure_main_reply_keyboard(
            update,
            context,
            force=force_keyboard,
            notice=None,
        )
        text = _main_menu_prompt_text(lang)
        await _panel_message(
            update,
            context,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(tg_id),
        )
        return
    if action == "keyboard":
        force_keyboard = _should_force_reply_keyboard(chat_state, ttl_seconds=0)
        await _ensure_main_reply_keyboard(
            update,
            context,
            force=force_keyboard,
            notice=None,
        )
        await q.answer(_bridge.t("keyboard.enabled", lang), show_alert=False)
        return
    
    response = None
    delegate_action = None
    
    if bridge_user_ctx and action not in {"menu"}:
        incoming = _bridge.IncomingMessage(
            platform="telegram",
            user_id=bridge_user_ctx.user_id,
            text=action,
            raw=raw_payload,
        )
        try:
            response = await _bridge.handle_menu_selection(bridge_user_ctx, incoming, context=context)
        except Exception:
            response = None

    if isinstance(response, _bridge.BridgeResponse):
        menu_actions = response.actions or {}
        _apply_bridge_menu_actions(context, menu_actions)
        delegate_action = menu_actions.get("delegate")

    target_action = delegate_action or action

    # If we have a delegate action, execute it immediately without showing intermediate bridge response
    if await _handle_general_menu_delegate(target_action, update, context):
        return

    # If not handled by delegate, show bridge response if available
    if isinstance(response, _bridge.BridgeResponse):
        markup = _bridge_menu_to_inline_keyboard(response.actions.get("menu"), tg_id)
        if response.messages:
            await _panel_message(
                update,
                context,
                response.messages[0],
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            await _send_bridge_responses(update, response.messages[1:], context=context)
            return
        elif markup and not delegate_action:
             # Only show menu if no delegate action was found (fallback)
            await _panel_message(
                update,
                context,
                _main_menu_prompt_text(lang),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return

    if target_action == "users":
        if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
            lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
            await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
            return

        db_users = _load_db()
        lang = _get_user_report_lang(_ensure_user(db_users, tg_id, q.from_user.username if q and q.from_user else None))
        text = _bridge.t("admin.users.list.intro", lang)
        try:
            kb = _users_keyboard(db_users, 0, 8, lang)
            context.user_data["last_users_page"] = 0
        except Exception as e:
            logging.error(f"Error creating users keyboard: {e}")
            await q.answer(_bridge.t("admin.users.load_error", lang), show_alert=True)
            return

        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            logging.error(f"Error editing message: {e}")
            try:
                await q.delete_message()
            except Exception:
                pass
            await context.bot.send_message(chat_id=int(tg_id), text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if target_action == "stats":
        if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
            lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
            await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
            return
        db_users = _load_db()
        lang = _get_user_report_lang(_ensure_user(db_users, tg_id, q.from_user.username if q and q.from_user else None))
        try:
            kb = _users_keyboard(db_users, 0, 8, lang)
            await q.edit_message_text(_bridge.t("users.panel.header", lang), parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as exc:
            logging.error(f"Error opening users stats panel: {exc}")
            await q.answer(_bridge.t("admin.stats.open_error", lang), show_alert=True)
            return
        return
    if target_action == "pending":
        if not (_is_admin_tg(tg_id) or _is_super_admin(tg_id)):
            lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
            await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
            return
        db_pending = _load_db()
        lang = _get_user_report_lang(_ensure_user(db_pending, tg_id, q.from_user.username if q and q.from_user else None))
        text = _bridge.t("pending.list.title", lang)
        kb = _pending_keyboard(db_pending)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await q.delete_message()
            await context.bot.send_message(chat_id=int(tg_id), text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if target_action == "settings":
        if not _is_super_admin(tg_id):
            lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
            await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
            return
        return await open_settings_panel(update, context)
    if target_action == "notifications":
        if not _is_super_admin(tg_id):
            lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
            await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
            return
        lang = _get_user_report_lang(_ensure_user(_load_db(), tg_id, q.from_user.username if q and q.from_user else None))
        text = _bridge.t("notifications.panel", lang)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(_bridge.t("notifications.buttons.all", lang), callback_data="notify:all")],
            [InlineKeyboardButton(_bridge.t("notifications.buttons.active", lang), callback_data="notify:active"),
             InlineKeyboardButton(_bridge.t("notifications.buttons.inactive", lang), callback_data="notify:inactive")],
            [InlineKeyboardButton(_bridge.t("notifications.buttons.select", lang), callback_data="notify:select")],
            [InlineKeyboardButton(_bridge.t("menu.header", lang), callback_data="main_menu:show")]
        ])
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            logging.error(f"Error editing message in notifications handler: {e}")
            try:
                await q.delete_message()
            except Exception:
                pass
            await context.bot.send_message(chat_id=int(tg_id), text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if action in ("menu",):
        return

    lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username if q and q.from_user else None))
    await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)


async def usercard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    
    import logging
    logging.info(f"usercard_cb called with data: {q.data}")
    
    caller_tg = str(q.from_user.id)
    lang = _get_user_report_lang(_ensure_user(_load_db(), caller_tg, q.from_user.username if q and q.from_user else None))
    try:
        parts = q.data.split(":")
        root = parts[0]
    except Exception as e:
        logging.error(f"Error parsing callback data: {e}, data: {q.data}")
        try:
            await q.answer(_bridge.t("common.invalid_data", lang), show_alert=True)
        except Exception:
            pass
        return

    db = _load_db()
    
    # ===== نظام الإشعارات الجديد =====
    if root == "notify":
        logging.info(f"Processing notify action: {parts}, caller: {caller_tg}")
        
        # التحقق من الصلاحيات أولاً
        if not (_is_admin_tg(caller_tg) or _is_super_admin(caller_tg)):
            logging.warning(f"Unauthorized access attempt: {caller_tg}")
            lang = _get_user_report_lang(_ensure_user(db, caller_tg, q.from_user.username if q and q.from_user else None))
            try:
                await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
                return await q.edit_message_text(_bridge.t("common.unauthorized", lang), parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.error(f"Error in unauthorized response: {e}")
                try:
                    return await q.message.reply_text(_bridge.t("common.unauthorized", lang), parse_mode=ParseMode.HTML)
                except Exception:
                    return
        
        if len(parts) < 2:
            logging.warning(f"Invalid notify callback data: {q.data}")
            try:
                await q.answer(_bridge.t("common.invalid_data", _get_user_report_lang(_ensure_user(db, caller_tg, q.from_user.username if q and q.from_user else None))), show_alert=True)
                return await q.edit_message_text(_bridge.t("common.invalid_data", _get_user_report_lang(_ensure_user(db, caller_tg, q.from_user.username if q and q.from_user else None))), parse_mode=ParseMode.HTML)
            except Exception:
                return await q.message.reply_text(_bridge.t("common.invalid_data", _get_user_report_lang(_ensure_user(db, caller_tg, q.from_user.username if q and q.from_user else None))), parse_mode=ParseMode.HTML)
        
        action = parts[1] if len(parts) > 1 else None
        
        if not action:
            logging.warning(f"No action in notify callback: {q.data}")
            try:
                await q.answer(_bridge.t("common.invalid_data", _get_user_report_lang(_ensure_user(db, caller_tg, q.from_user.username if q and q.from_user else None))), show_alert=True)
            except Exception:
                pass
            return
        
        logging.info(f"Notify action: {action}")
        
        # إجابة على callback_query
        try:
            await q.answer()
        except Exception as e:
            logging.error(f"Error answering callback: {e}")
            pass
        
        try:
            if action == "all":
                users_count = len(db["users"])
                context.user_data["await"] = {"op": "notify_bulk", "targets": "all", "count": users_count}
                try:
                    return await q.edit_message_text(
                        f"📢 <b>إشعار لجميع المستخدمين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
                except Exception:
                    return await q.message.reply_text(
                        f"📢 <b>إشعار لجميع المستخدمين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
            
            elif action == "active":
                active_users = [u for u in db["users"].values() if u.get("is_active")]
                users_count = len(active_users)
                context.user_data["await"] = {"op": "notify_bulk", "targets": "active", "count": users_count}
                try:
                    return await q.edit_message_text(
                        f"✅ <b>إشعار للمفعّلين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
                except Exception:
                    return await q.message.reply_text(
                        f"✅ <b>إشعار للمفعّلين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
            
            elif action == "inactive":
                inactive_users = [u for u in db["users"].values() if not u.get("is_active")]
                users_count = len(inactive_users)
                context.user_data["await"] = {"op": "notify_bulk", "targets": "inactive", "count": users_count}
                try:
                    return await q.edit_message_text(
                        f"⛔ <b>إشعار للمعطّلين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
                except Exception:
                    return await q.message.reply_text(
                        f"⛔ <b>إشعار للمعطّلين</b>\n\n"
                        f"عدد المستلمين: <b>{users_count}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
            
            elif action == "select":
                users = list(db["users"].values())
                users.sort(key=lambda x: (not x.get("is_active"), x.get("custom_name") or x.get("tg_username") or x.get("tg_id")))
                
                if "notify_selection" not in context.user_data:
                    context.user_data["notify_selection"] = {"selected": []}
                
                selected = context.user_data["notify_selection"].get("selected", [])
                
                text = f"👥 <b>اختيار المستخدمين</b>\n\nالمحددون: <b>{len(selected)}</b>\n\nانقر على المستخدم:"
                rows = []
                
                for u in users[:20]:
                    tg = u["tg_id"]
                    name = u.get("custom_name") or (("@" + u.get("tg_username")) if u.get("tg_username") else f"TG:{tg}")
                    if len(name) > 15:
                        name = name[:12] + "..."
                    status = "✅" if u.get("is_active") else "⛔"
                    is_selected = tg in selected
                    prefix = "☑️" if is_selected else "☐"
                    rows.append([InlineKeyboardButton(f"{prefix} {status} {name}", callback_data=f"notify:toggle:{tg}")])
                
                if selected:
                    rows.append([InlineKeyboardButton(f"📤 إرسال ({len(selected)})", callback_data="notify:send")])
                
                rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")])
                
                try:
                    return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
                except Exception:
                    return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            
            elif action == "toggle":
                if len(parts) >= 3:
                    target_tg = parts[2]
                    selection = context.user_data.get("notify_selection", {"selected": []})
                    selected = selection.get("selected", [])
                    
                    if target_tg in selected:
                        selected.remove(target_tg)
                        await q.answer("✅ تم إلغاء التحديد")
                    else:
                        selected.append(target_tg)
                        await q.answer("✅ تم التحديد")
                    
                    context.user_data["notify_selection"] = {"selected": selected}
                    
                    # إعادة عرض القائمة
                    users = list(db["users"].values())
                    users.sort(key=lambda x: (not x.get("is_active"), x.get("custom_name") or x.get("tg_username") or x.get("tg_id")))
                    
                    text = f"👥 <b>اختيار المستخدمين</b>\n\nالمحددون: <b>{len(selected)}</b>\n\nانقر على المستخدم:"
                    rows = []
                    
                    for u in users[:20]:
                        tg = u["tg_id"]
                        name = u.get("custom_name") or (("@" + u.get("tg_username")) if u.get("tg_username") else f"TG:{tg}")
                        if len(name) > 15:
                            name = name[:12] + "..."
                        status = "✅" if u.get("is_active") else "⛔"
                        is_selected = tg in selected
                        prefix = "☑️" if is_selected else "☐"
                        rows.append([InlineKeyboardButton(f"{prefix} {status} {name}", callback_data=f"notify:toggle:{tg}")])
                    
                    if selected:
                        rows.append([InlineKeyboardButton(f"📤 إرسال ({len(selected)})", callback_data="notify:send")])
                    
                    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")])
                    
                    try:
                        return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
                    except Exception:
                        return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            
            elif action == "send":
                selection = context.user_data.get("notify_selection", {})
                selected = selection.get("selected", [])
                
                if not selected:
                    await q.answer("❌ لم يتم تحديد أي مستخدمين", show_alert=True)
                    return
                
                context.user_data["await"] = {"op": "notify_bulk", "targets": selected, "count": len(selected)}
                try:
                    return await q.edit_message_text(
                        f"👥 <b>إشعار لمستخدمين محددين</b>\n\n"
                        f"عدد المستلمين: <b>{len(selected)}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
                except Exception:
                    return await q.message.reply_text(
                        f"👥 <b>إشعار لمستخدمين محددين</b>\n\n"
                        f"عدد المستلمين: <b>{len(selected)}</b>\n\n"
                        f"💡 أرسل نص الإشعار:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                        ])
                    )
            
            elif action == "menu":
                users = list(db["users"].values())
                total = len(users)
                active = sum(1 for u in users if u.get("is_active"))
                inactive = total - active
                
                text = (
                    "📢 <b>نظام الإشعارات</b>\n\n"
                    f"📊 الإحصائيات:\n"
                    f"• إجمالي: <b>{total}</b>\n"
                    f"• مفعّلين: <b>{active}</b>\n"
                    f"• معطّلين: <b>{inactive}</b>\n\n"
                    f"اختر نوع الإشعار:"
                )
                
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 للجميع", callback_data="notify:all")],
                    [InlineKeyboardButton("✅ للمفعّلين", callback_data="notify:active"),
                     InlineKeyboardButton("⛔ للمعطّلين", callback_data="notify:inactive")],
                    [InlineKeyboardButton("👥 اختيار مستخدمين", callback_data="notify:select")]
                ])
                
                try:
                    return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception:
                    return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            
            else:
                logging.warning(f"Unknown notify action: {action}, data: {q.data}")
                await q.answer("❌ إجراء غير معروف", show_alert=True)
                return
                
        except Exception as e:
            logging.error(f"Error in notify handler: {e}", exc_info=True)
            try:
                await q.answer("❌ حدث خطأ", show_alert=True)
            except Exception:
                pass
            try:
                await q.edit_message_text(f"❌ حدث خطأ: {str(e)}", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

    if root in ("main_menu", "ref"):
        return
    if root == "stats":
        if len(parts) > 1 and parts[1] == "page":
            await q.answer()
            try:
                page = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                page = 0

            db_stats = _load_db()
            users, stats_snapshot = _collect_stats(db_stats)
            selected = set(context.user_data.get("stats_selected", []))
            markup, current_page, max_page = _stats_keyboard(users, page, 8, selected)
            text = _format_stats_header(stats_snapshot)
            if max_page > 0:
                text += f"\n\n📄 الصفحة: <b>{current_page + 1}</b> / <b>{max_page + 1}</b>"

            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        if len(parts) > 1 and parts[1] == "bulk":
            await q.answer()
            try:
                op = parts[2]  # activate_all | deactivate_all | activate_page | deactivate_page
            except Exception:
                return await q.answer("❌ أمر غير صالح", show_alert=True)
            db_stats = _load_db()
            users_all, _ = _collect_stats(db_stats)
            per_page = 8
            target_users: List[Dict[str, Any]] = []
            page = 0
            if op in ("activate_all", "deactivate_all"):
                target_users = users_all
            elif op in ("activate_page", "deactivate_page"):
                try:
                    page = int(parts[3])
                except Exception:
                    page = 0
                total = len(users_all)
                max_page = max(0, (total - 1) // per_page)
                page = max(0, min(page, max_page))
                start = page * per_page
                target_users = users_all[start:start+per_page]
            elif op in ("activate_selected", "deactivate_selected"):
                selected_ids = set(context.user_data.get("stats_selected", []))
                if selected_ids:
                    target_users = [u for u in users_all if str(u.get("tg_id")) in selected_ids]
                else:
                    target_users = []
            else:
                return await q.answer("❌ إجراء غير معروف", show_alert=True)
            # تنفيذ العملية
            activated = 0
            deactivated = 0
            for u in target_users:
                if op.startswith("activate"):
                    if not u.get("is_active"):
                        u["is_active"] = True
                        activated += 1
                        try:
                            await _notify_user(
                                context,
                                u["tg_id"],
                                "✅ تم تفعيل حسابك.\nلأي مساعدة: واتساب: <a href='https://wa.me/962795378832'>+962 7 9537 8832</a>",
                            )
                        except Exception:
                            pass
                else:
                    if u.get("is_active"):
                        u["is_active"] = False
                        deactivated += 1
                        try:
                            await _notify_user(
                                context,
                                u["tg_id"],
                                "⛔ تم تعطيل حسابك.\nللتواصل مع الدعم: واتساب: <a href='https://wa.me/962795378832'>+962 7 9537 8832</a>",
                            )
                        except Exception:
                            pass
            _save_db(db_stats)
            # إعادة العرض
            users_new, stats_new = _collect_stats(db_stats)
            selected = set(context.user_data.get("stats_selected", []))
            markup, current_page, max_page = _stats_keyboard(users_new, page, 8, selected)
            text = _format_stats_header(stats_new)
            if max_page > 0:
                text += f"\n\n📄 الصفحة: <b>{current_page + 1}</b> / <b>{max_page + 1}</b>"
            summary = f"\n\n✅ تم التحديث: "
            if op.startswith("activate"):
                summary += f"تفعيل <b>{activated}</b>"
            else:
                summary += f"تعطيل <b>{deactivated}</b>"
            text += summary
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        if len(parts) > 1 and parts[1] == "none":
            return await q.answer("لا توجد بيانات في هذه الصفحة.", show_alert=True)

        if len(parts) > 1 and parts[1] == "back":
            await q.answer()
            tg_id = str(q.from_user.id)
            lang_back = _resolve_lang_for_tg(tg_id)
            text = _main_menu_prompt_text(lang_back)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
            except Exception:
                await q.delete_message()
                await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
            return
        if len(parts) > 1 and parts[1] == "select":
            # stats:select:toggle:{tg}:{page}
            await q.answer()
            if len(parts) >= 4 and parts[2] == "toggle":
                tg_to_toggle = parts[3]
                try:
                    page = int(parts[4]) if len(parts) > 4 else 0
                except Exception:
                    page = 0
                sel = set(context.user_data.get("stats_selected", []))
                if tg_to_toggle in sel:
                    sel.remove(tg_to_toggle)
                else:
                    sel.add(tg_to_toggle)
                context.user_data["stats_selected"] = list(sel)
                db_stats = _load_db()
                users, stats_snapshot = _collect_stats(db_stats)
                selected = sel
                markup, current_page, max_page = _stats_keyboard(users, page, 8, selected)
                text = _format_stats_header(stats_snapshot)
                if max_page > 0:
                    text += f"\n\n📄 الصفحة: <b>{current_page + 1}</b> / <b>{max_page + 1}</b>"
                try:
                    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
                except Exception:
                    await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
                return
        if len(parts) > 1 and parts[1] in ("select_all_page", "deselect_all_page"):
            await q.answer()
            try:
                page = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                page = 0
            db_stats = _load_db()
            users_all, stats_snapshot = _collect_stats(db_stats)
            per_page = 8
            total = len(users_all)
            max_page = max(0, (total - 1) // per_page)
            page = max(0, min(page, max_page))
            start = page * per_page
            chunk = users_all[start:start+per_page]
            sel = set(context.user_data.get("stats_selected", []))
            if parts[1] == "select_all_page":
                for u in chunk:
                    tg = str(u.get("tg_id"))
                    if tg:
                        sel.add(tg)
            else:
                for u in chunk:
                    tg = str(u.get("tg_id"))
                    if tg in sel:
                        sel.remove(tg)
            context.user_data["stats_selected"] = list(sel)
            markup, current_page, max_page = _stats_keyboard(users_all, page, 8, sel)
            text = _format_stats_header(stats_snapshot)
            if max_page > 0:
                text += f"\n\n📄 الصفحة: <b>{current_page + 1}</b> / <b>{max_page + 1}</b>"
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
    
    if root == "pending":
        # معالج زر رجوع قائمة المنتظرين
        if len(parts) > 1 and parts[1] == "back":
            await q.answer()
            tg_id = str(q.from_user.id)
            lang_back = _resolve_lang_for_tg(tg_id)
            text = _main_menu_prompt_text(lang_back)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
            except Exception:
                await q.delete_message()
                await context.bot.send_message(chat_id=q.from_user.id, text=text, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
            return
    
    if root == "users":
        # معالجة أزرار التنقل في قائمة المستخدمين
        lang_users = _resolve_lang_for_tg(caller_tg)
        if not (_is_admin_tg(caller_tg) or _is_super_admin(caller_tg)):
            return await q.edit_message_text(_unauthorized(lang_users), parse_mode=ParseMode.HTML)
        
        if len(parts) >= 2 and parts[1] == "none":
            return await q.answer(_bridge.t("admin.users.page.empty", lang_users), show_alert=True)

        list_text = _bridge.t("users.panel.header", lang_users)

        if len(parts) >= 2 and parts[1] == "page":
            try:
                page = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                page = 0
            page = max(0, page)
            context.user_data["last_users_page"] = page
            return await _refresh_users_overview(q, db, page, lang_users)

        if len(parts) >= 2 and parts[1] == "back":
            page = max(0, context.user_data.get("last_users_page", 0))
            return await _refresh_users_overview(q, db, page, lang_users)
        
        if len(parts) >= 2 and parts[1] == "open":
            target_tg = parts[2] if len(parts) > 2 else None
            if target_tg:
                return await _render_usercard(update, context, target_tg)

    # ===== طلب رفع الحد من المستخدم =====
    if root == "limitreq":
        try:
            _, kind, target_tg = parts
        except Exception:
            return await q.edit_message_text("زر غير صالح.", parse_mode=ParseMode.HTML)

        # Safety: only the caller can request for their own id
        if target_tg != caller_tg:
            return await q.edit_message_text("⚠️ هذا الطلب غير صالح.", parse_mode=ParseMode.HTML)

        u = _ensure_user(db, target_tg, None)
        lim = u.get("limits", {})
        today_used = lim.get("today_used", 0)
        daily = lim.get("daily", 0)
        month_used = lim.get("month_used", 0)
        monthly = lim.get("monthly", 0)

        if kind == "both":
            user_msg = "بلغت حد <b>اليوم</b> و<b>الشهر</b>. تم إرسال طلب رفع الحد للإدارة."
            kind_human = "كلاهما"
        elif kind == "daily":
            user_msg = "بلغت <b>حد اليوم</b>. تم إرسال طلب رفع الحد للإدارة."
            kind_human = "اليومي"
        else:
            user_msg = "بلغت <b>حد الشهر</b>. تم إرسال طلب رفع الحد للإدارة."
            kind_human = "الشهري"

        admin_text = (
            "📈 <b>طلب رفع حد</b>\n"
            f"• المستخدم: <b>{_display_name(u)}</b> ({_fmt_tg_with_phone(target_tg)})\n"
            f"• اليومي: {today_used}/{daily}\n"
            f"• الشهري: {month_used}/{monthly}\n"
            f"• النوع: <b>{kind_human}</b>"
        )
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 إدارة الحدود", callback_data=f"ucard:limits:{target_tg}"),
             InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"ucard:open:{target_tg}")]
        ])
        await _notify_supers(context, admin_text, admin_kb)

        try:
            await q.edit_message_text(f"✅ {user_msg}", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    
    # ===== Language change handler (allowed for regular users) =====
    if root == "lang":
        # معالج تغيير لغة التقرير للمستخدمين العاديين
        if len(parts) == 3 and parts[1] == "user_set":
            # تغيير اللغة من قبل المستخدم نفسه
            lang_code = parts[2]
            user_tg_id = str(q.from_user.id)
            u = _ensure_user(db, user_tg_id, q.from_user.username)
            old_lang = _get_user_report_lang(u)

            if old_lang == lang_code:
                await q.answer("ℹ️ هذه اللغة مفعلة بالفعل", show_alert=True)
                return

            _set_user_report_lang(u, lang_code)
            _save_db(db)
            logger.info(
                "telegram language updated",
                extra={"tg_id": user_tg_id, "old_lang": old_lang, "new_lang": lang_code},
            )

            # Rebuild context to reflect the new language everywhere (menus, notifications)
            bridge_ctx = _build_bridge_user_context(update, context)
            new_lang = bridge_ctx.language if bridge_ctx else _normalize_report_lang_code(lang_code)
            confirm_text = _bridge.t("language.changed", new_lang, label=_lang_name(new_lang))

            menu_resp = None
            try:
                if bridge_ctx:
                    menu_resp = await _bridge.render_main_menu(bridge_ctx)
            except Exception:
                menu_resp = None

            menu_text = ""
            if isinstance(menu_resp, _bridge.BridgeResponse) and menu_resp.messages:
                menu_text = menu_resp.messages[0]

            combined_text = confirm_text if not menu_text else f"{confirm_text}\n\n{menu_text}"

            reply_markup = _bridge_menu_to_inline_keyboard(
                menu_resp.actions.get("menu") if isinstance(menu_resp, _bridge.BridgeResponse) else None,
                bridge_ctx.user_id if bridge_ctx else user_tg_id,
            )

            await q.answer(f"✅ {_lang_name(new_lang)}")
            try:
                await q.edit_message_text(combined_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            except Exception:
                await context.bot.send_message(
                    chat_id=int(user_tg_id),
                    text=combined_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            return
        
        elif len(parts) == 2 and parts[1] == "cancel":
            # إلغاء - حذف الرسالة
            await q.answer()
            await q.delete_message()
            return

    # From here: admin only
    admin_tg = caller_tg
    if not (_is_admin_tg(admin_tg) or _is_super_admin(admin_tg)):
        return await q.edit_message_text("غير مصرح.", parse_mode=ParseMode.HTML)

    if root == "ucard":
        _, action, target_tg = parts
        if action == "delete":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"ucard:confirm_delete:{target_tg}"),
                InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")
            ]])
            return await q.edit_message_text("⚠️ تأكيد حذف المستخدم نهائيًا؟", parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "confirm_delete":
            # لا تحذف نفسك كسوبر أدمن بالخطأ
            db2 = _load_db()
            if target_tg in _env_super_admins() or target_tg in _db_super_admins(db2):
                return await q.edit_message_text("❌ لا يمكن حذف سوبر أدمن.", parse_mode=ParseMode.HTML)

            users = db2.get("users", {})
            removed = users.pop(target_tg, None)
            db2["users"] = users
            _save_db(db2)
            await _notify_supers(context, f"🗑️ تم حذف المستخدم {_fmt_tg_with_phone(target_tg)} من قِبل Admin:{admin_tg}.")
            return await q.edit_message_text("🗑️ تم حذف المستخدم بنجاح.", parse_mode=ParseMode.HTML)

        u = _ensure_user(db, target_tg, None)

        if action == "open":
            return await _render_usercard(update, context, target_tg)

        if action == "monthly":
            first_activation = not bool(u.get("activation_date"))
            phone, platform = _activation_request_info(db, target_tg, u)
            is_whatsapp = _is_probable_whatsapp_user(target_tg=target_tg, user=u, platform=platform, phone=phone)

            preset = _resolve_activation_preset(db, "monthly")
            days = preset["days"]
            daily_limit = preset["daily"]
            monthly_limit = preset["monthly"]
            today = datetime.utcnow().date()
            exp = u.get("expiry_date")
            if exp:
                try:
                    exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
                except Exception:
                    exp_d = today
            else:
                exp_d = today
            base = max(today, exp_d)
            u["expiry_date"] = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            u["is_active"] = True
            u["plan"] = "monthly"
            _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
            if not u.get("activation_date"):
                u["activation_date"] = today.strftime("%Y-%m-%d")
            if first_activation:
                _set_user_report_lang(u, "en")
            _audit(u, admin_tg, "monthly_activate", add_days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
            _remove_pending_request(db, target_tg)
            _save_db(db)

            activation_msg = (
                f"🟢 تم تفعيل اشتراك لمدة <b>{days}</b> يوم.\n"
                f"• حد يومي: <b>{daily_limit}</b> تقرير\n"
                f"• حد شهري: <b>{monthly_limit}</b> تقرير\n"
                f"• تاريخ الانتهاء: <code>{u['expiry_date']}</code>"
            )
            await _notify_user(context, target_tg, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
            await _post_activation_admin_notice_if_needed(
                context,
                db=db,
                user=u,
                target_tg=target_tg,
                first_activation=first_activation,
                is_whatsapp=is_whatsapp,
            )
            await _notify_supers(
                context,
                f"🟢 (Admin:{admin_tg}) فعّل اشتراك {days} يوم ({daily_limit}/{monthly_limit}) للمستخدم {_fmt_tg_with_phone(target_tg)}."
            )
            return await _render_usercard(update, context, target_tg)

        if action == "trial":
            first_activation = not bool(u.get("activation_date"))
            phone, platform = _activation_request_info(db, target_tg, u)
            is_whatsapp = _is_probable_whatsapp_user(target_tg=target_tg, user=u, platform=platform, phone=phone)

            preset = _resolve_activation_preset(db, "trial")
            days = preset["days"]
            daily_limit = preset["daily"]
            monthly_limit = preset["monthly"]
            today = datetime.utcnow().date()
            u["is_active"] = True
            u["plan"] = "trial"
            u["activation_date"] = today.strftime("%Y-%m-%d")
            u["expiry_date"] = (today + timedelta(days=days)).strftime("%Y-%m-%d")
            _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
            if first_activation:
                _set_user_report_lang(u, "en")
            _audit(u, admin_tg, "trial_activate", days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
            _remove_pending_request(db, target_tg)
            _save_db(db)

            activation_msg = (
                f"🧪 تم تفعيل تجربة لمدة <b>{days}</b> يوم.\n"
                f"• الحد اليومي: <b>{daily_limit}</b> تقرير\n"
                f"• الحد الشهري: <b>{monthly_limit}</b> تقرير\n"
                f"• ينتهي في: <code>{u['expiry_date']}</code>"
            )
            await _notify_user(context, target_tg, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
            await _post_activation_admin_notice_if_needed(
                context,
                db=db,
                user=u,
                target_tg=target_tg,
                first_activation=first_activation,
                is_whatsapp=is_whatsapp,
            )
            await _notify_supers(
                context,
                f"🧪 (Admin:{admin_tg}) فعّل تجربة {days} يوم ({daily_limit}/{monthly_limit}) للمستخدم {_fmt_tg_with_phone(target_tg)}."
            )
            return await _render_usercard(update, context, target_tg)

        if action == "enable30":
            first_activation = not bool(u.get("activation_date"))
            phone, platform = _activation_request_info(db, target_tg, u)
            is_whatsapp = _is_probable_whatsapp_user(target_tg=target_tg, user=u, platform=platform, phone=phone)

            preset = _resolve_activation_preset(db, "monthly")
            days = preset["days"]
            daily_limit = preset["daily"]
            monthly_limit = preset["monthly"]
            u["is_active"] = True
            if not u.get("activation_date"):
                u["activation_date"] = datetime.utcnow().strftime("%Y-%m-%d")
            u["expiry_date"] = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
            _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
            if first_activation:
                _set_user_report_lang(u, "en")
            _audit(u, admin_tg, "enable30", days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
            _remove_pending_request(db, target_tg)
            _save_db(db)

            activation_msg = (
                f"✅ تم تفعيل حسابك لمدة <b>{days}</b> يوم.\n"
                f"• حد يومي: <b>{daily_limit}</b> تقرير\n"
                f"• حد شهري: <b>{monthly_limit}</b> تقرير\n"
                f"• تاريخ الانتهاء: <code>{u['expiry_date']}</code>"
            )
            await _notify_user(context, target_tg, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
            await _post_activation_admin_notice_if_needed(
                context,
                db=db,
                user=u,
                target_tg=target_tg,
                first_activation=first_activation,
                is_whatsapp=is_whatsapp,
            )
            await _notify_supers(
                context,
                f"✅ (Admin:{admin_tg}) فعّل {_fmt_tg_with_phone(target_tg)} ({days}يوم حد {daily_limit}/{monthly_limit})."
            )
            return await _render_usercard(update, context, target_tg)

        if action == "activate_custom":
            context.user_data["await"] = {"op": "activate_custom", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]])
            return await q.edit_message_text(
                "🧾 أرسل: <b>أيام,حد_يومي,عدد_التقارير[,تقارير_إضافية]</b> مثال <code>30,25,500</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        if action == "renew30":
            preset = _resolve_activation_preset(db, "monthly")
            days = preset["days"]
            today = datetime.utcnow().date()
            exp = u.get("expiry_date")
            if exp:
                try: exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
                except Exception: exp_d = today
            else:
                exp_d = today
            base = max(today, exp_d)
            u["expiry_date"] = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            u["is_active"] = True
            if not u.get("activation_date"):
                u["activation_date"] = today.strftime("%Y-%m-%d")
            _audit(u, admin_tg, "renew30", add_days=days)
            _remove_pending_request(db, target_tg)
            _save_db(db)
            await _notify_user(context, target_tg, f"♻️ تم تجديد {days} يوم. الانتهاء: <code>{u['expiry_date']}</code>")
            await _notify_supers(context, f"♻️ (Admin:{admin_tg}) جدد {days}يوم للمستخدم {_fmt_tg_with_phone(target_tg)}.")
            return await _render_usercard(update, context, target_tg)

        if action == "renew_custom":
            context.user_data["await"] = {"op": "renew_custom", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("usercard.prompt.renew_custom", lang), parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "balance_edit":
            context.user_data["await"] = {"op": "balance_edit", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("usercard.prompt.balance_edit", lang), parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "set_name":
            context.user_data["await"] = {"op": "set_name", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("usercard.prompt.custom_name", lang), parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "disable":
            u["is_active"] = False
            _audit(u, admin_tg, "disable")
            _save_db(db)
            target_lang = _get_user_report_lang(u)
            support_link = "<a href='https://wa.me/962795378832'>+962 7 9537 8832</a>"
            await _notify_user(context, target_tg, _bridge.t("usercard.notify.disabled", target_lang, support=support_link))
            await _notify_supers(context, f"⛔ (Admin:{admin_tg}) عطّل {_fmt_tg_with_phone(target_tg)}.")
            lang_rows.append([InlineKeyboardButton(_bridge.t("admin.users.main", current_lang), callback_data="main_menu:show")])

        if action == "services":
            return await q.edit_message_text(_bridge.t("services.manage.title", lang), parse_mode=ParseMode.HTML, reply_markup=_svc_kb(u, lang))

        if action == "limits":
            return await q.edit_message_text(_bridge.t("limits.manage.title", lang), parse_mode=ParseMode.HTML, reply_markup=_limits_kb(u, lang))

        if action == "note":
            admin_lang = _get_user_report_lang(_ensure_user(db, tg_id, update.effective_user.username))
            await _panel_message(update, context, _bridge.t("users.panel.header", admin_lang), parse_mode=ParseMode.HTML, reply_markup=_users_keyboard(db, 0, 8, admin_lang))
            context.user_data["await"] = {"op": "note_set", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("usercard.prompt.note", lang), parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "audit":
            logs = u.get("audit", [])[-10:]
            if not logs:
                txt = "لا يوجد سجل عمليات بعد."
            else:
                lines = [f"{i+1}. [{x['ts']}] {x['op']} — by {x['admin']}" for i, x in enumerate(logs)]
                txt = "📊 آخر 10 عمليات:\n" + "\n".join(lines)
            return await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")]]))

        if action == "quick_notify":
            context.user_data["await"] = {"op": "notify_user", "target": target_tg}
            return await q.edit_message_text(
                f"📬 <b>إشعار سريع</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>المستخدم:</b> {_display_name(u)}\n"
                f"<b>TG ID:</b> <code>{target_tg}</code>\n"
                f"<b>📞 الهاتف:</b> " + (f"<a href='https://wa.me/{u.get('phone','').lstrip('+')}'>{u.get('phone')}</a>" if u.get('phone') else "—") + "\n\n"
                f"💡 <i>أرسل نص الإشعار:</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]
                ])
            )

        if action == "notify":
            context.user_data["await"] = {"op": "notify_user", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ إلغاء", callback_data="main_menu:show")]])
            return await q.edit_message_text("📬 أرسل نص التنبيه الذي سيصل للمستخدم:", parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "lang":
            # عرض لوحة اختيار اللغة
            current_lang = _get_user_report_lang(u)
            text = (
                "🌐 <b>تغيير لغة التقرير</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>اللغة الحالية:</b> {_lang_label(current_lang)}\n\n"
                f"اختر اللغة الجديدة:"
            )
            lang_rows = _language_choice_rows(current_lang, lambda code: f"lang:set:{target_tg}:{code}")
            lang_rows.append([InlineKeyboardButton("↩️ رجوع للبطاقة", callback_data="main_menu:show")])
            kb = InlineKeyboardMarkup(lang_rows)
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif root == "svc":
        _, svc, target_tg = parts
        if svc != "carfax":
            u = _ensure_user(db, target_tg, None)
            await update.callback_query.edit_message_text(
                _bridge.t("services.manage.title", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=_svc_kb(u, lang),
            )
            return
        u = _ensure_user(db, target_tg, None)
        cur = u["services"].get(svc, False)
        u["services"][svc] = not cur
        _audit(u, admin_tg, "toggle_service", service=svc, value=not cur)
        _save_db(db)
        svc_names = {
            "carfax": "usercard.service.carfax",
        }
        target_lang = _get_user_report_lang(u)
        svc_key = svc_names.get(svc, svc)
        svc_label_user = _bridge.t(svc_key, target_lang) if svc_key.startswith("usercard.service") else svc
        status_txt = _bridge.t("services.status.enabled" if u["services"][svc] else "services.status.disabled", target_lang)
        svc_label_admin = _bridge.t(svc_key, lang) if svc_key.startswith("usercard.service") else svc
        try:
            await _notify_user(context, target_tg, _bridge.t("services.notify.user", target_lang, status=status_txt, service=svc_label_user))
        except Exception:
            pass
        try:
            action_txt = _bridge.t("services.action.enable" if u["services"][svc] else "services.action.disable", lang)
            await _notify_supers(context, _bridge.t("services.notify.super", lang, admin=admin_tg, action=action_txt, service=svc_label_admin, user=_fmt_tg_with_phone(target_tg)))
        except Exception:
            pass
        await update.callback_query.edit_message_text(
            _bridge.t("services.manage.title", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=_svc_kb(u, lang),
        )

    elif root == "lang":
        # تبديل اللغة (مستخدم أو أدمن)
        if len(parts) == 3 and parts[1] == "user_set":
            lang_code = parts[2]
            u = _ensure_user(db, tg_id, q.from_user.username if q.from_user else None)
            old_lang = _get_user_report_lang(u)
            _set_user_report_lang(u, lang_code)
            _audit(u, tg_id, "change_report_lang_self", old_lang=old_lang, new_lang=lang_code)
            _save_db(db)

            lang_text = _lang_name(lang_code)
            confirmation = _bridge.t("language.changed", lang_code, label=lang_text)
            await q.edit_message_text(confirmation, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(_main_menu_button_text(lang_code), callback_data="main_menu:show")]
            ]))
            return

        # معالج تغيير اللغة من قبل الإدارة
        _, action, target_tg, lang_code = parts
        u = _ensure_user(db, target_tg, None)
        old_lang = _get_user_report_lang(u)
        _set_user_report_lang(u, lang_code)
        _audit(u, admin_tg, "change_report_lang", old_lang=old_lang, new_lang=lang_code)
        _save_db(db)
        logger.info(
            "admin language updated",
            extra={"tg_id": target_tg, "old_lang": old_lang, "new_lang": lang_code, "by": admin_tg},
        )
        
        lang_text = _lang_name(lang_code)
        old_lang_text = _lang_name(old_lang)
        
        text = (
            "🌐 <b>تم تغيير لغة التقرير</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>تم التغيير بنجاح!</b>\n\n"
            f"• اللغة السابقة: <b>{old_lang_text}</b>\n"
            f"• اللغة الجديدة: <b>{lang_text}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <i>سيتم استخدام اللغة الجديدة في التقارير والقوائم والإشعارات لهذا المستخدم</i>"
        )
        
        # إشعار المستخدم
        await _notify_user(context, target_tg, 
            f"🌐 تم تغيير لغة التقرير إلى <b>{lang_text}</b> من قبل الإدارة.")
        
        return await q.edit_message_text(text, parse_mode=ParseMode.HTML, 
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("↩️ رجوع إلى البطاقة", callback_data="main_menu:show")]
                                        ]))

    elif root == "limits":
        _, action, target_tg = parts
        u = _ensure_user(db, target_tg, None)
        if action == "set_daily":
            context.user_data["await"] = {"op": "set_daily", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("limits.prompt.set_daily", lang), parse_mode=ParseMode.HTML, reply_markup=kb)
        if action == "set_monthly":
            context.user_data["await"] = {"op": "set_monthly", "target": target_tg}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            return await q.edit_message_text(_bridge.t("limits.prompt.set_monthly", lang), parse_mode=ParseMode.HTML, reply_markup=kb)
        if action == "reset_today":
            u["limits"]["today_used"] = 0
            _audit(u, admin_tg, "reset_today")
            _save_db(db)
            target_lang = _get_user_report_lang(u)
            await _notify_user(
                context,
                target_tg,
                _bridge.t("limits.reset.user_notify", target_lang, admin=admin_tg),
            )
            return await q.edit_message_text(_bridge.t("limits.reset.done", lang), parse_mode=ParseMode.HTML, reply_markup=_limits_kb(u, lang))

# =================== Admin Settings Panel ===================
def _mask(s: str, keep: int = 3):
    if not s: return "—"
    if len(s) <= keep: return "*" * len(s)
    return s[:keep] + "…" + "*" * max(0, len(s) - keep - 1)

def _settings_kb(lang: Optional[str] = None) -> InlineKeyboardMarkup:
    """لوحة مفاتيح إعدادات السوبر أدمن مع التنقيح حسب اللغة."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("settings.buttons.secrets_policy", lang), callback_data="settings:secrets_policy")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.activation_presets", lang), callback_data="settings:activation_presets")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.add_super_admin", lang), callback_data="settings:add_super_admin"),
         InlineKeyboardButton(_bridge.t("settings.buttons.manage_supers", lang), callback_data="settings:manage_supers")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.reload_env", lang), callback_data="settings:reload_env")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.main_menu", lang), callback_data="main_menu:show")]
    ])


def _activation_presets_kb(lang: Optional[str] = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("settings.buttons.edit_trial", lang), callback_data="settings:activation_edit:trial")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.edit_monthly", lang), callback_data="settings:activation_edit:monthly")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.reset_presets", lang), callback_data="settings:activation_reset")],
        [InlineKeyboardButton(_bridge.t("settings.buttons.back_settings", lang), callback_data="settings:back")]
    ])


def _activation_presets_text(db: Dict[str, Any], lang: Optional[str] = None) -> str:
    trial = _resolve_activation_preset(db, "trial")
    monthly = _resolve_activation_preset(db, "monthly")
    return _bridge.t(
        "settings.activation_presets.body",
        lang,
        trial_days=trial["days"],
        trial_daily=trial["daily"],
        trial_monthly=trial["monthly"],
        monthly_days=monthly["days"],
        monthly_daily=monthly["daily"],
        monthly_monthly=monthly["monthly"],
    )
async def settings_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    
    try:
        await q.answer()
    except Exception:
        pass
    
    tg_id = str(q.from_user.id)
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    db = _load_db()
    admin_user = _ensure_user(db, tg_id, q.from_user.username if q.from_user else None)
    lang = _get_user_report_lang(admin_user)
    env_admins = _env_super_admins()
    db_admins = _db_super_admins(db)

    if not _is_super_admin(tg_id):
        debug_info = _bridge.t(
            "settings.unauthorized.debug",
            lang,
            tg_id=tg_id,
            env_admins=", ".join(map(str, env_admins)) if env_admins else _bridge.t("common.invalid_data", lang),
            db_admins=", ".join(db_admins) if db_admins else _bridge.t("common.invalid_data", lang),
        )
        await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
        try:
            return await q.edit_message_text(debug_info, parse_mode=ParseMode.HTML)
        except Exception:
            return await q.message.reply_text(debug_info, parse_mode=ParseMode.HTML)

    supers = _db_super_admins(db)

    if action in {"set_api_token", "secrets_policy"}:
        context.user_data.pop("await", None)
        env_map = {
            "set_api_token": "API_TOKEN",
        }
        if action == "secrets_policy":
            text = _bridge.t("settings.secrets_policy.text", lang)
        else:
            env_var = env_map[action]
            text = _bridge.t("settings.env.locked", lang, env_var=env_var)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(_main_menu_button_text(lang), callback_data="main_menu:show")]])
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if action == "activation_presets":
        text = _activation_presets_text(db, lang)
        kb = _activation_presets_kb(lang)
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if action == "activation_edit":
        target = parts[2] if len(parts) > 2 else ""
        if target not in DEFAULT_ACTIVATION_PRESETS:
            await q.answer(_bridge.t("settings.activation_edit.unknown", lang), show_alert=True)
            return
        preset = _resolve_activation_preset(db, target)
        context.user_data["await"] = {"op": "activation_preset_edit", "preset": target}
        title = _bridge.t("settings.activation_edit.title.trial", lang) if target == "trial" else _bridge.t("settings.activation_edit.title.monthly", lang)
        text = _bridge.t(
            "settings.activation_edit.prompt",
            lang,
            title=title,
            days=preset["days"],
            daily=preset["daily"],
            monthly=preset["monthly"],
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="settings:activation_presets")]])
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if action == "activation_reset":
        db.pop("activation_presets", None)
        _save_db(db)
        text = _bridge.t("settings.activation_reset.done", lang) + "\n\n" + _activation_presets_text(db, lang)
        kb = _activation_presets_kb(lang)
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if action == "add_super_admin":
        context.user_data["await"] = {"op": "add_super_admin"}
        text = _bridge.t("settings.supers.add.prompt", lang)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if action == "supers" or action == "manage_supers":
        # تحسين عرض قائمة السوبر أدمن
        env_admins = _env_super_admins()
        rows = []
        
        # زر إضافة سوبر أدمن جديد
        rows.append([InlineKeyboardButton(_bridge.t("settings.buttons.add_super_admin", lang), callback_data="settings:add_super_admin")])
        
        if not supers:
            rows.append([InlineKeyboardButton(_bridge.t("settings.buttons.back_settings", lang), callback_data="main_menu:show")])
            text = (
                _bridge.t("settings.supers.manage.header", lang, count=len(supers))
                + _bridge.t("settings.supers.manage.empty", lang)
            )
            try:
                return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            except Exception:
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        
        # عرض السوبر أدمن من db.json
        for sid in supers:
            is_ultimate = sid in env_admins
            status_icon = "🔒" if is_ultimate else "👤"
            status_text = _bridge.t("settings.supers.status.env_suffix", lang) if is_ultimate else ""
            btn_text = f"{status_icon} {sid}{status_text}"
            # فقط السوبر أدمن المطلق يمكنه الحذف
            if _is_ultimate_super(tg_id) and not is_ultimate:
                rows.append([InlineKeyboardButton(f"🗑️ {btn_text}", callback_data=f"settings:supers_del:{sid}")])
            else:
                rows.append([InlineKeyboardButton(f"🔒 {btn_text}{_bridge.t('settings.supers.button.blocked_suffix', lang)}", callback_data="settings:noop")])
        
        rows.append([InlineKeyboardButton(_bridge.t("settings.buttons.back_settings", lang), callback_data="main_menu:show")])
        
        text = _bridge.t("settings.supers.manage.header", lang, count=len(supers))
        for i, sid in enumerate(supers, 1):
            is_ultimate = sid in env_admins
            status = _bridge.t("settings.supers.status.env_label", lang) if is_ultimate else _bridge.t("settings.supers.status.db_label", lang)
            text += f"{i}. <code>{sid}</code> — {status}\n"
        
        text += _bridge.t("settings.supers.manage.footer", lang)
        
        try:
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                             reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            return await q.message.reply_text(text, parse_mode=ParseMode.HTML,
                                             reply_markup=InlineKeyboardMarkup(rows))

    if action == "supers_del":
        target = parts[2] if len(parts) > 2 else ""
        if not target:
            try:
                return await q.edit_message_text(_bridge.t("settings.supers.delete.missing_target", lang), parse_mode=ParseMode.HTML)
            except Exception:
                return await q.message.reply_text(_bridge.t("settings.supers.delete.missing_target", lang), parse_mode=ParseMode.HTML)
        if not _is_ultimate_super(tg_id):
            try:
                return await q.edit_message_text(_bridge.t("settings.supers.delete.only_ultimate", lang), parse_mode=ParseMode.HTML)
            except Exception:
                return await q.message.reply_text(_bridge.t("settings.supers.delete.only_ultimate", lang), parse_mode=ParseMode.HTML)
        if target in _env_super_admins():
            try:
                return await q.edit_message_text(_bridge.t("settings.supers.delete.not_env_deletable", lang), parse_mode=ParseMode.HTML)
            except Exception:
                return await q.message.reply_text(_bridge.t("settings.supers.delete.not_env_deletable", lang), parse_mode=ParseMode.HTML)
        if target in supers:
            supers.remove(target)
            _save_db(db)
            await _notify_supers(context, _bridge.t("settings.supers.delete.notify", lang, target=target, by=tg_id))
            
            # إعادة بناء القائمة بعد الحذف
            env_admins = _env_super_admins()
            rows = []
            for sid in supers:
                is_ultimate = sid in env_admins
                status_icon = "🔒" if is_ultimate else "👤"
                status_text = _bridge.t("settings.supers.status.env_suffix", lang) if is_ultimate else ""
                btn_text = f"{status_icon} {sid}{status_text}"
                if _is_ultimate_super(tg_id) and not is_ultimate:
                    rows.append([InlineKeyboardButton(f"🗑️ {btn_text}", callback_data=f"settings:supers_del:{sid}")])
                else:
                    rows.append([InlineKeyboardButton(f"🔒 {btn_text}{_bridge.t('settings.supers.button.blocked_suffix', lang)}", callback_data="settings:noop")])
            
            rows.append([InlineKeyboardButton(_bridge.t("settings.buttons.back_settings", lang), callback_data="main_menu:show")])
            
            text = _bridge.t("settings.supers.delete.success", lang, target=target, remaining=len(supers))
            
            try:
                return await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                                 reply_markup=InlineKeyboardMarkup(rows))
            except Exception:
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML,
                                                 reply_markup=InlineKeyboardMarkup(rows))
        try:
            return await q.edit_message_text(_bridge.t("settings.supers.delete.not_found", lang), parse_mode=ParseMode.HTML)
        except Exception:
            return await q.message.reply_text(_bridge.t("settings.supers.delete.not_found", lang), parse_mode=ParseMode.HTML)

    if action == "reload_env":
        try:
            _refresh_env()
            text = _bridge.t("settings.reload.success", lang)
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
            except Exception:
                await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
            return
        except Exception as e:
            text = _bridge.t("settings.reload.error", lang, error=str(e))
            try:
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
            except Exception:
                await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
            return

    if action == "cancel":
        # إلغاء العملية الحالية
        context.user_data.pop("await", None)
        await q.answer(_bridge.t("common.cancelled", lang))
        return await open_settings_panel(update, context)
    
    if action == "back":
        return await open_settings_panel(update, context)
    
    if action == "back_main":
        # العودة للقائمة الرئيسية
        tg_id = str(q.from_user.id)
        lang_main = _resolve_lang_for_tg(tg_id)
        text = _main_menu_prompt_text(lang_main)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=build_reference_menu(tg_id))
        except Exception:
            await q.delete_message()
            await context.bot.send_message(chat_id=tg_id, text=text, parse_mode=ParseMode.HTML, reply_markup=build_reference_menu(tg_id))
        return
    
    if action == "noop":
        # زر غير نشط (للأزرار المحظورة)
        await q.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
        return

    try:
        return await q.edit_message_text(_bridge.t("settings.unknown_action", lang), parse_mode=ParseMode.HTML)
    except Exception:
        return await q.message.reply_text(_bridge.t("settings.unknown_action", lang), parse_mode=ParseMode.HTML)

async def open_settings_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فتح لوحة إعدادات النظام للسوبر أدمن"""
    try:
        # الحصول على tg_id بشكل صحيح
        if update.callback_query:
            tg_id = str(update.callback_query.from_user.id)
        elif update.effective_user:
            tg_id = str(update.effective_user.id)
        else:
            tg_id = ""
        
        db = _load_db()
        admin_user = _ensure_user(db, tg_id, update.effective_user.username if update.effective_user else None) if tg_id else None
        lang = _get_user_report_lang(admin_user) if admin_user else None
        
        if not tg_id:
            if update.callback_query:
                await update.callback_query.answer(_bridge.t("settings.error.no_user_id", lang), show_alert=True)
                return
            else:
                return await update.message.reply_text(_bridge.t("settings.error.no_user_id", lang), parse_mode=ParseMode.HTML)
        
        # التحقق من الصلاحيات
        is_super = _is_super_admin(tg_id)
        env_admins = _env_super_admins()
        db_admins = _db_super_admins(db)
        
        if not is_super:
            debug_info = _bridge.t(
                "settings.unauthorized.debug",
                lang,
                tg_id=tg_id,
                env_admins=", ".join(map(str, env_admins)) if env_admins else _bridge.t("common.invalid_data", lang),
                db_admins=", ".join(db_admins) if db_admins else _bridge.t("common.invalid_data", lang),
            )
            if update.callback_query:
                await update.callback_query.answer(_bridge.t("common.unauthorized", lang), show_alert=True)
                try:
                    return await update.callback_query.edit_message_text(debug_info, parse_mode=ParseMode.HTML)
                except Exception:
                    return await update.callback_query.message.reply_text(debug_info, parse_mode=ParseMode.HTML)
            else:
                return await _panel_message(update, context, debug_info, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))
        
        cfg = get_env()
        env_admins_count = len(env_admins)
        db_admins_count = len(db_admins)
        total_supers = env_admins_count + db_admins_count
        
        text = _bridge.t(
            "settings.menu.summary",
            lang,
            api_token=_mask(cfg.api_token),
            env_count=env_admins_count,
            db_count=db_admins_count,
            total=total_supers,
        )
        if update.callback_query:
            try:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
            except Exception:
                # إذا فشل التعديل، أرسل رسالة جديدة
                await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
        else:
            await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=_settings_kb(lang))
    except Exception as e:
        logger.error(f"Error in open_settings_panel: {e}", exc_info=True)
        error_msg = f"❌ حدث خطأ: {str(e)}"
        if update.callback_query:
            try:
                await update.callback_query.answer(_bridge.t("common.invalid_data", lang), show_alert=True)
                await update.callback_query.message.reply_text(error_msg, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        elif update.message:
            await _panel_message(update, context, error_msg, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))

async def request_activation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    lang = _get_user_report_lang(u)
    # منع تكرار التفعيل إذا كان المستخدم مُفعلاً بالفعل
    expiry_str = u.get("expiry_date")
    is_active = u.get("is_active")
    is_valid_subscription = False
    
    if is_active:
        if not expiry_str:
            is_valid_subscription = True
        else:
            try:
                exp_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                if exp_date >= date.today():
                    is_valid_subscription = True
            except Exception:
                pass

    if is_valid_subscription:
        await _panel_message(
            update,
            context,
            _bridge.t("activation.already_active", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(tg_id)
        )
        return

    pending_requests = db.get("activation_requests", [])
    already_pending = any(str(req.get("tg_id")) == tg_id for req in pending_requests)
    # حتى لو كان هناك طلب معلق، استمر في جمع رقم الهاتف لتحديثه وإعادة الإشعار
    # بدء جمع رقم الهاتف مع مفتاح البلد
    context.user_data["await"] = {"op": "activation_phone"}
    cancel_label = _bridge.t("action.cancel", lang)
    cc_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇯🇴 +962", callback_data="activation:cc:+962"),
         InlineKeyboardButton("🇸🇦 +966", callback_data="activation:cc:+966"),
         InlineKeyboardButton("🇦🇪 +971", callback_data="activation:cc:+971")],
        [InlineKeyboardButton("🇮🇶 +964", callback_data="activation:cc:+964"),
         InlineKeyboardButton("🇪🇬 +20", callback_data="activation:cc:+20"),
         InlineKeyboardButton("🌍 رمز آخر", callback_data="activation:cc:other")],
        [InlineKeyboardButton(cancel_label, callback_data="main_menu:show")]
    ])
    pending_note = _bridge.t("activation.request_pending", lang) if already_pending else ""
    txt = _bridge.t("activation.prompt.cc", lang)
    full_txt = f"{pending_note}\n\n{txt}" if pending_note else txt
    await _panel_message(update, context, full_txt, parse_mode=ParseMode.HTML, reply_markup=cc_kb)

# =================== VIN flow (send PDF only) ===================
async def _send_pdf_file(context: ContextTypes.DEFAULT_TYPE, chat_id: int, filename: str, caption: str):
    with open(filename, "rb") as fh:
        try:
            from bot_core.telemetry import atimed
            try:
                size = os.path.getsize(filename)
            except Exception:
                size = 0
            async with atimed("tg.send_document", bytes=size, via="file"):
                await context.bot.send_document(chat_id=chat_id, document=fh, filename=os.path.basename(filename), caption=caption, parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_document(chat_id=chat_id, document=fh, filename=os.path.basename(filename), caption=caption, parse_mode=ParseMode.HTML)


def _tg_extract_all_vins(text: str, *, primary: Optional[str] = None) -> List[str]:
    raw = (text or "").strip()
    candidates: List[str] = []
    if primary:
        candidates.append(primary)
    try:
        for m in VIN_RE.finditer(raw.upper()):
            s = (m.group(0) or "").strip()
            if s:
                candidates.append(s)
    except Exception:
        pass
    # fallback: normalize the whole text
    try:
        whole = _norm_vin(raw)
        if whole:
            candidates.append(whole)
    except Exception:
        pass

    out: List[str] = []
    seen: set[str] = set()
    for cand in candidates:
        try:
            v = (_norm_vin(cand) or "").strip().upper()
        except Exception:
            v = ""
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


async def _tg_run_vin_report_job(context: ContextTypes.DEFAULT_TYPE, job: Dict[str, Any]) -> None:
    tg_id = str(job.get("tg_id") or "")
    chat_id = int(job.get("chat_id") or 0)
    username = job.get("username")
    first_name = job.get("first_name")
    vin = str(job.get("vin") or "").strip().upper()
    raw_payload = job.get("raw_payload") or {}
    request_key = str(job.get("request_key") or "") or None
    progress_message_id = int(job.get("progress_message_id") or 0)

    if not (tg_id and chat_id and vin and progress_message_id):
        return

    # Ensure the primary progress message is registered as a subscriber.
    try:
        await _tg_register_inflight(tg_id, vin, chat_id=chat_id, message_id=progress_message_id)
    except Exception:
        pass

    lock = _user_lock(tg_id)
    header_snapshot: Optional[Dict[str, Any]] = None
    report_lang = get_report_default_lang() or "en"
    rid_charge: Optional[str] = None
    bridge_user_ctx: Optional[_bridge.UserContext] = None

    # Build header snapshot first (pre-reserve), then update after reserve.
    async with lock:
        db = _load_db()
        u = _ensure_user(db, tg_id, username)
        report_lang = _get_user_report_lang(u)

        bridge_user_ctx = _bridge.UserContext(
            user_id=tg_id,
            language=report_lang,
            phone=u.get("phone"),
            metadata={
                "username": username,
                "first_name": first_name,
                "user_data": {},
                "db_user": u,
            },
        )

        limit_allowed, limit_message, limit_reason = await _bridge.check_user_limits(
            bridge_user_ctx,
            storage=db,
        )
        _save_db(db)

        if not limit_allowed:
            # Edit the already-sent progress message with the limit text.
            body = (limit_message or "").strip()
            if not body and limit_reason in {"daily", "monthly", "both"}:
                try:
                    limit_response = await _bridge.request_limit_increase(
                        bridge_user_ctx,
                        storage=db,
                        notifications=context,
                        reason=limit_reason,
                    )
                    if isinstance(limit_response, _bridge.BridgeResponse) and limit_response.messages:
                        body = "\n\n".join([m for m in limit_response.messages if m])
                except Exception:
                    body = ""
            if not body:
                body = "Limit reached."
            try:
                await _tg_edit_inflight_messages(
                    context,
                    tg_id,
                    vin,
                    text=re.sub(r"<[^>]+>", "", body),
                    parse_mode=None,
                )
            except Exception:
                pass
            return

        # Deterministic rid for exactly-once accounting.
        rid_charge = compute_request_id(
            platform="telegram",
            user_id=str(tg_id),
            vin=vin,
            language=report_lang,
            options={"product": "carfax_vhr"},
            request_key=request_key,
        )
        try:
            _reserve_credit(tg_id, rid=rid_charge, meta={"platform": "telegram", "vin": vin, "lang": report_lang})
        except Exception:
            logger.warning("reserve_credit failed", exc_info=True)

        db2 = _load_db()
        u2 = _ensure_user(db2, tg_id, username)
        header_snapshot = {
            "monthly_remaining": _remaining_monthly_reports(u2),
            "monthly_limit": _safe_int((u2.get("limits", {}) or {}).get("monthly")),
            "today_used": _safe_int((u2.get("limits", {}) or {}).get("today_used")),
            "daily_limit": _safe_int((u2.get("limits", {}) or {}).get("daily")),
            "days_left": _days_left(u2.get("expiry_date")),
        }

    if not header_snapshot:
        header_snapshot = {
            "monthly_remaining": None,
            "monthly_limit": 0,
            "today_used": 0,
            "daily_limit": 0,
            "days_left": None,
        }

    header = _build_vin_progress_header(vin, lang=report_lang, **header_snapshot)
    progress_payload = header + "\n" + _make_progress_bar(0)
    try:
        await _tg_edit_inflight_messages(
            context,
            tg_id,
            vin,
            text=progress_payload,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            await _tg_edit_inflight_messages(
                context,
                tg_id,
                vin,
                text=re.sub(r"<[^>]+>", "", progress_payload),
                parse_mode=None,
            )
        except Exception:
            pass

    progress_state: Dict[str, Any] = {
        "header": header,
        "cap": 80,
    }
    cleanup_paths: List[str] = []

    def _register_cleanup(path: Optional[str]) -> None:
        if path and path not in cleanup_paths:
            cleanup_paths.append(path)

    stop_event = asyncio.Event()
    progress_task = asyncio.create_task(
        _tg_progress_updater(context, tg_id, vin, stop_event, state=progress_state, header=header)
    )
    report_result: Optional[_ReportResult] = None
    bridge_temp_files: List[str] = []

    tg_total_timeout_s = float(os.getenv("TG_REPORT_TOTAL_TIMEOUT_SEC", "120") or 120)
    tg_total_timeout_s = max(10.0, min(tg_total_timeout_s, 300.0))
    tg_send_timeout_s = float(os.getenv("TG_REPORT_SEND_TIMEOUT_SEC", "60") or 60)
    tg_send_timeout_s = max(10.0, min(tg_send_timeout_s, 300.0))
    tg_t0 = time.perf_counter()

    def _tg_remaining_s(floor: float = 1.0) -> float:
        return max(floor, tg_total_timeout_s - (time.perf_counter() - tg_t0))

    try:
        try:
            from bot_core.telemetry import new_rid as _new_rid, set_rid as _set_rid
        except Exception:
            _new_rid = None  # type: ignore
            _set_rid = None  # type: ignore

        rid = (_new_rid("tg-") if _new_rid else None)
        rid_cm = None
        try:
            if _set_rid and rid:
                rid_cm = _set_rid(rid)
            else:
                rid_cm = None
            if rid_cm:
                rid_cm.__enter__()

            if bridge_user_ctx:
                try:
                    bridge_user_ctx.language = report_lang
                except Exception:
                    pass
                vin_incoming = _bridge.IncomingMessage(
                    platform="telegram",
                    user_id=bridge_user_ctx.user_id,
                    text=vin,
                    raw=raw_payload,
                )
                try:
                    vin_bridge_response = await asyncio.wait_for(
                        _bridge.handle_text(
                            bridge_user_ctx,
                            vin_incoming,
                            context=context,
                            skip_limit_validation=True,
                            deduct_credit=False,
                        ),
                        timeout=_tg_remaining_s(),
                    )
                except Exception as exc:
                    logger.error("Bridge VIN handler failed: %s", exc, exc_info=True)
                    vin_bridge_response = None
                if isinstance(vin_bridge_response, _bridge.BridgeResponse):
                    bridge_temp_files = list(vin_bridge_response.actions.get("temp_files", []))
                    for temp_path in bridge_temp_files:
                        _register_cleanup(temp_path)
                    report_result = vin_bridge_response.actions.get("report_result")

            if not report_result:
                gen_attempts = int(os.getenv("TG_REPORT_RETRIES", "3") or 3)
                gen_attempts = max(1, min(gen_attempts, 6))
                gen_backoffs = [0.0, 1.0, 3.0, 7.0, 12.0, 20.0]
                for i in range(gen_attempts):
                    if i > 0:
                        try:
                            await asyncio.sleep(gen_backoffs[min(i, len(gen_backoffs) - 1)])
                        except Exception:
                            pass
                    try:
                        report_result = await asyncio.wait_for(
                            _generate_vin_report(vin, language=report_lang, fast_mode=True, user_id=str(tg_id)),
                            timeout=_tg_remaining_s(),
                        )
                    except asyncio.TimeoutError:
                        report_result = _ReportResult(
                            success=False,
                            user_message=_bridge.t("report.error.timeout", report_lang),
                            errors=["timeout"],
                            vin=vin,
                        )
                    except Exception as exc:
                        logger.error(
                            "tg_report_generation_failed",
                            extra={"vin": vin, "tg_id": tg_id, "attempt": i + 1},
                            exc_info=True,
                        )
                        report_result = _ReportResult(
                            success=False,
                            user_message=_bridge.t("report.error.fetch", report_lang),
                            errors=[f"exception:{type(exc).__name__}"],
                            vin=vin,
                        )

                    if report_result and getattr(report_result, "success", False) and getattr(report_result, "pdf_bytes", None):
                        break
                    if not _is_retryable_report_failure(report_result):
                        break
        except asyncio.TimeoutError:
            report_result = _ReportResult(
                success=False,
                user_message=_bridge.t("report.error.timeout", report_lang),
                errors=["sla_timeout"],
                vin=vin,
            )
        finally:
            try:
                if rid_cm:
                    rid_cm.__exit__(None, None, None)
            except Exception:
                pass
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(progress_task, timeout=2)
        except Exception:
            progress_task.cancel()
            try:
                await progress_task
            except Exception:
                pass

    if not report_result:
        report_result = _ReportResult(
            success=False,
            user_message=_bridge.t("report.error.generic", report_lang),
            errors=["bridge_failed"],
            vin=vin,
        )

    try:
        if not report_result.success:
            err_text = _report_failure_user_message(report_result, report_lang)
            refund_snapshot = await _finalize_report_request(context, tg_id, delivered=False, rid=rid_charge)
            refreshed_header = _build_vin_progress_header(
                vin,
                monthly_remaining=refund_snapshot["monthly_remaining"],
                monthly_limit=refund_snapshot["monthly_limit"],
                today_used=refund_snapshot["today_used"],
                daily_limit=refund_snapshot["daily_limit"],
                days_left=refund_snapshot["days_left"],
                lang=report_lang,
            )
            refund_note = _bridge.t("report.refund.note", report_lang)
            failure_note = f"\n\n{escape(err_text)}{refund_note}\n\n❌ Failed + refunded"
            try:
                await asyncio.wait_for(
                    _tg_edit_inflight_messages(
                        context,
                        tg_id,
                        vin,
                        text=refreshed_header + "\n" + _make_progress_bar(100) + failure_note,
                        parse_mode=ParseMode.HTML,
                    ),
                    timeout=_tg_remaining_s(floor=1.0),
                )
            except Exception:
                pass
            user_label = f"TG:{tg_id}"
            await _super_dashboard_event(
                context,
                _bridge.t("report.dashboard.failure", None, vin=vin, user=user_label, error=err_text),
            )
            return

        delivered = False
        delivery_note = ""

        if report_result.pdf_bytes:
            try:
                progress_state["cap"] = 95
                pdf_bytes = bytes(report_result.pdf_bytes)

                max_send_attempts = int(os.getenv("TG_DELIVERY_RETRIES", "3") or 3)
                max_send_attempts = max(1, min(max_send_attempts, 6))
                send_backoffs = [0.0, 1.0, 3.0, 7.0, 12.0, 20.0]
                last_send_exc: Optional[BaseException] = None
                sent_ok = False
                for attempt in range(max_send_attempts):
                    if attempt > 0:
                        try:
                            await asyncio.sleep(send_backoffs[min(attempt, len(send_backoffs) - 1)])
                        except Exception:
                            pass
                    try:
                        bio = BytesIO(pdf_bytes)
                        bio.name = report_result.pdf_filename or f"{vin}.pdf"
                        await asyncio.wait_for(
                            context.bot.send_document(
                                chat_id=chat_id,
                                document=bio,
                                filename=bio.name,
                                caption=f"📄 تقرير VIN <code>{vin}</code>",
                                parse_mode=ParseMode.HTML,
                            ),
                            timeout=min(tg_send_timeout_s, _tg_remaining_s(floor=1.0)),
                        )
                        sent_ok = True
                        break
                    except asyncio.TimeoutError as exc:
                        last_send_exc = exc
                    except Exception as exc:
                        last_send_exc = exc
                        logger.warning("tg_send_document_failed vin=%s attempt=%s", vin, attempt + 1, exc_info=True)

                if not sent_ok:
                    raise (last_send_exc or RuntimeError("tg_send_document_failed"))
            except asyncio.TimeoutError:
                report_result = _ReportResult(
                    success=False,
                    user_message=_bridge.t("report.error.timeout", report_lang),
                    errors=["send_timeout"],
                    vin=vin,
                )
            except Exception as exc:
                report_result = _ReportResult(
                    success=False,
                    user_message=_bridge.t("report.error.fetch", report_lang),
                    errors=[f"send_failed:{exc}"],
                    vin=vin,
                )
            delivery_note = _bridge.t("report.success.pdf_note", report_lang)
            delivered = bool(report_result and report_result.success)

        if delivered:
            finalize_snapshot = await _finalize_report_request(context, tg_id, delivered=True, rid=rid_charge)
            success_header = _build_vin_progress_header(
                vin,
                monthly_remaining=finalize_snapshot["monthly_remaining"],
                monthly_limit=finalize_snapshot["monthly_limit"],
                today_used=finalize_snapshot["today_used"],
                daily_limit=finalize_snapshot["daily_limit"],
                days_left=finalize_snapshot["days_left"],
                lang=report_lang,
            )
            note = delivery_note or _bridge.t("report.success.note", report_lang)
            try:
                await asyncio.wait_for(
                    _tg_edit_inflight_messages(
                        context,
                        tg_id,
                        vin,
                        text=success_header + "\n" + _make_progress_bar(100) + "\n\n" + (note or ""),
                        parse_mode=ParseMode.HTML,
                    ),
                    timeout=_tg_remaining_s(floor=1.0),
                )
            except Exception:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=success_header + "\n" + _make_progress_bar(100) + "\n\n" + (note or ""),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            remaining_credit = finalize_snapshot["monthly_remaining"]
            remaining_txt = _bridge.t("report.summary.unlimited", report_lang) if remaining_credit is None else str(remaining_credit)
            user_label = f"TG:{tg_id}"
            await _super_dashboard_event(
                context,
                _bridge.t("report.dashboard.success", None, vin=vin, user=user_label, remaining=remaining_txt),
            )
            return

        refund_snapshot = await _finalize_report_request(context, tg_id, delivered=False, rid=rid_charge)
        refreshed_header = _build_vin_progress_header(
            vin,
            monthly_remaining=refund_snapshot["monthly_remaining"],
            monthly_limit=refund_snapshot["monthly_limit"],
            today_used=refund_snapshot["today_used"],
            daily_limit=refund_snapshot["daily_limit"],
            days_left=refund_snapshot["days_left"],
            lang=report_lang,
        )
        try:
            pdf_failure_note = "\n\n" + _bridge.t("report.error.pdf", report_lang) + _bridge.t("report.refund.note", report_lang) + "\n\n❌ Failed + refunded"
            await asyncio.wait_for(
                _tg_edit_inflight_messages(
                    context,
                    tg_id,
                    vin,
                    text=refreshed_header + "\n" + _make_progress_bar(100) + pdf_failure_note,
                    parse_mode=ParseMode.HTML,
                ),
                timeout=_tg_remaining_s(floor=1.0),
            )
        except Exception:
            pass
        user_label = f"TG:{tg_id}"
        await _super_dashboard_event(
            context,
            _bridge.t("report.dashboard.pdf_failure", None, vin=vin, user=user_label),
        )
    finally:
        for path in cleanup_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        _cleanup_temp_files(bridge_temp_files)
def _normalize_phone(raw: str, cc: Optional[str]) -> Optional[str]:
    s = (raw or "").strip().replace(" ", "").replace("-", "")
    if s.startswith("+") and s[1:].isdigit() and 9 <= len(s) <= 16:
        return s
    if cc and s.isdigit():
        local = s.lstrip("0")
        if not local:
            return None
        cand = f"{cc}{local}" if cc.startswith("+") else f"+{cc}{local}"
        if cand.startswith("++"):
            cand = cand[1:]
        if cand[1:].isdigit() and 9 <= len(cand) <= 16:
            return cand
    return None
async def activation_cc_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = str(q.from_user.id)
    try:
        db_lang = _load_db()
        u_lang = _ensure_user(db_lang, tg_id, q.from_user.username if q.from_user else None)
        lang = _get_user_report_lang(u_lang)
    except Exception:
        lang = None
    try:
        _, action, value = q.data.split(":", 2)
    except Exception:
        return
    if action == "cc":
        if value == "other":
            context.user_data["activation_cc"] = None
            return await q.edit_message_text(
                _bridge.t("activation.cc.enter_full", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            )
        context.user_data["activation_cc"] = value
        return await q.edit_message_text(
            _bridge.t("activation.cc.selected", lang, cc=value),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
        )
    # دعم اختيار مفتاح للهاتف من شاشة بياناتي
    if action == "phone_cc":
        if value == "other":
            context.user_data["set_phone_cc"] = None
            return await q.edit_message_text(
                _bridge.t("activation.cc.enter_full", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
            )
        context.user_data["set_phone_cc"] = value
        return await q.edit_message_text(
            _bridge.t("activation.cc.selected", lang, cc=value),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
        )
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Minimal plain-text main menu fallback (avoids heavy HTML) throttled per chat
    aw = context.user_data.get("await")
    op = aw.get("op") if isinstance(aw, dict) else None

    tg_id = str(update.effective_user.id)

    try:
        _lang_db = _load_db()
        _lang_user = _ensure_user(_lang_db, tg_id, update.effective_user.username)
        lang = _get_user_report_lang(_lang_user)
    except Exception:
        lang = None

    chat_data = context.chat_data if isinstance(context.chat_data, dict) else {}

    async def _show_main_menu_single() -> None:
        """Always reopen the main menu by editing/creating one panel message and hide the user text."""
        if isinstance(chat_data, dict):
            chat_data["suppress_fallback"] = True
        try:
            await _panel_message(
                update,
                context,
                _main_menu_prompt_text(lang),
                parse_mode=ParseMode.HTML,
                reply_markup=build_main_menu(tg_id, lang),
            )
        except Exception:
            pass

    is_super = _is_super_admin(str(update.effective_user.id))
    is_admin = _is_admin_tg(str(update.effective_user.id))
    txt = (update.message.text or "").strip()
    # Open main menu only on explicit user request
    try:
        lower_txt = (txt or "").strip().lower()
    except Exception:
        lower_txt = ""
    if lower_txt in MENU_SHOW_KEYWORDS:
        await _show_main_menu_single()
        return
    raw_payload = _bridge_raw_payload(update)
    bridge_user_ctx = _build_bridge_user_context(update, context)
    
    # معالج الإلغاء - يعمل مع أي نص يحتوي على "إلغاء" أو "cancel"
    if txt.lower() in ("إلغاء", "cancel", "الغاء", "↩️ إلغاء") and aw:
        context.user_data.pop("await", None)
        await _panel_message(
            update,
            context,
            _bridge.t("common.cancelled", lang),
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(tg_id)
        )
        return

    # سوبر أدمن: إدخال رقم يبدأ بـ + (واتساب) لإنشاء/فتح بطاقة تفعيل مباشرة
    if _is_super_admin(tg_id):
        normalized_input = (txt or "").strip().replace(" ", "").replace("-", "")
        if normalized_input.startswith("00") and normalized_input[2:].isdigit():
            normalized_input = f"+{normalized_input[2:]}"

        wa_phone = _normalize_phone(normalized_input, None)
        if wa_phone:
            target_tg = wa_phone.lstrip("+")
            db_admin = _load_db()
            target_user = _ensure_user(db_admin, target_tg, None)
            target_user["phone"] = wa_phone

            pending = db_admin.setdefault("activation_requests", [])
            existing_req = next((r for r in pending if str(r.get("tg_id")) == str(target_tg)), None)
            if existing_req:
                existing_req["phone"] = wa_phone
                existing_req["platform"] = existing_req.get("platform") or "whatsapp"
                existing_req["ts"] = _now_str()
                created = False
            else:
                pending.append({"tg_id": str(target_tg), "ts": _now_str(), "phone": wa_phone, "platform": "whatsapp"})
                created = True

            _save_db(db_admin)

            preset_trial = _resolve_activation_preset(db_admin, "trial")
            preset_monthly = _resolve_activation_preset(db_admin, "monthly")

            info_lines = [
                "🛂 <b>طلب تفعيل (واتساب)</b>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📞 الرقم: <code>{escape(wa_phone)}</code>",
            ]
            if created:
                info_lines.append("✅ تم إنشاء طلب تفعيل لهذا الرقم.")
            info_lines.append("\nاختر نوع التفعيل:")
            card_text = "\n".join(info_lines)

            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"🧪 تجربة {preset_trial['days']} يوم — {preset_trial['daily']}/{preset_trial['monthly']}",
                        callback_data=f"pending:trial:{target_tg}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🟢 شهري {preset_monthly['days']} يوم — {preset_monthly['daily']}/{preset_monthly['monthly']}",
                        callback_data=f"pending:monthly:{target_tg}",
                    )
                ],
                [
                    InlineKeyboardButton("🧾 تفعيل مخصّص", callback_data=f"pending:activate_custom:{target_tg}"),
                    InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"pending:open:{target_tg}"),
                ],
                [
                    InlineKeyboardButton("⛔ رفض", callback_data=f"pending:deny:{target_tg}"),
                    InlineKeyboardButton("📝 قائمة المنتظرين", callback_data="pending:list"),
                ],
                [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu:show")],
            ])

            await _panel_message(update, context, card_text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        # fallback: فتح بطاقة مستخدم موجود عبر رقم/معرّف (بدون إنشاء طلب)
        phone_candidate = re.sub(r"[^\d+]", "", txt)
        if phone_candidate and len(phone_candidate) >= 6:
            db_lookup = _load_db()
            users_map = db_lookup.get("users", {}) or {}
            target_entry = None
            for uid, user_obj in users_map.items():
                if phone_candidate in (str(user_obj.get("phone")) or "") or phone_candidate == str(uid):
                    target_entry = uid
                    break
            if target_entry:
                try:
                    await _render_usercard(update, context, str(target_entry))
                    return
                except Exception:
                    pass

    # Handle broadcast message input
    broadcast_data = context.user_data.get("broadcast")
    if broadcast_data:
        # إرسال الرسالة
        await broadcast_send_handler(update, context)
        return

    # Handle Add Super Admin input (await flow)
    if op == "add_super_admin" and update.message and update.message.text:
        new_id = ''.join(filter(str.isdigit, update.message.text.strip()))
        if not new_id:
            await update.message.reply_text(_bridge.t("settings.add_super_admin.id_digits_only", lang), parse_mode=ParseMode.HTML)
            context.user_data.pop("await", None)
            return
        db = _load_db()
        supers = db.setdefault("super_admins", [])
        if new_id in supers:
            await update.message.reply_text(_bridge.t("settings.add_super_admin.exists_db", lang), parse_mode=ParseMode.HTML)
            context.user_data.pop("await", None)
            return
        supers.append(new_id)
        _save_db(db)
        await update.message.reply_text(_bridge.t("settings.add_super_admin.added_db", lang, tg_id=new_id), parse_mode=ParseMode.HTML)
        await _notify_supers(context, _bridge.t("settings.supers.add.notify", lang, target=new_id, by=tg_id))
        context.user_data.pop("await", None)
        return

    if not aw and bridge_user_ctx:
        if txt in MAIN_MENU_TEXTS:
            try:
                menu_response = await _bridge.render_main_menu(bridge_user_ctx)
            except Exception:
                menu_response = None
            if menu_response:
                await _send_bridge_responses(update, menu_response, context=context)
            return
        alias_id = MENU_TEXT_TO_ID.get(txt)
        if alias_id:
            incoming_menu = _bridge.IncomingMessage(
                platform="telegram",
                user_id=bridge_user_ctx.user_id,
                text=alias_id,
                raw=raw_payload,
            )
            try:
                menu_response = await _bridge.handle_menu_selection(bridge_user_ctx, incoming_menu, context=context)
            except Exception:
                menu_response = None
            if menu_response:
                await _send_bridge_responses(update, menu_response, context=context)
                menu_actions = menu_response.actions or {} if isinstance(menu_response, _bridge.BridgeResponse) else {}
                _apply_bridge_menu_actions(context, menu_actions)
                delegate_action = menu_actions.get("delegate") or alias_id
                if await _handle_general_menu_delegate(delegate_action, update, context):
                    return
            return

    if op == "activation_preset_edit" and update.message and update.message.text:
        preset_key = (aw or {}).get("preset")
        if preset_key not in DEFAULT_ACTIVATION_PRESETS:
            context.user_data.pop("await", None)
            await update.message.reply_text(_bridge.t("settings.activation_edit.unknown", lang), parse_mode=ParseMode.HTML)
            return
        if not _is_super_admin(tg_id):
            context.user_data.pop("await", None)
            await update.message.reply_text(_bridge.t("common.error.super_only", lang), parse_mode=ParseMode.HTML)
            return
        parts = [p.strip() for p in update.message.text.split(",") if p.strip()]
        if len(parts) < 3:
            await update.message.reply_text(
                _bridge.t("settings.activation_edit.format_hint", lang),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            days = max(1, int(parts[0]))
            daily_limit = max(1, int(parts[1]))
            monthly_limit = max(1, int(parts[2]))
        except ValueError:
            await update.message.reply_text(_bridge.t("settings.activation_edit.invalid_numbers", lang), parse_mode=ParseMode.HTML)
            return
        db = _load_db()
        _set_activation_preset(db, preset_key, days=days, daily=daily_limit, monthly=monthly_limit)
        context.user_data.pop("await", None)
        title = _bridge.t("settings.activation_edit.title.trial", lang) if preset_key == "trial" else _bridge.t("settings.activation_edit.title.monthly", lang)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="settings:activation_presets")]])
        await update.message.reply_text(
            _bridge.t("settings.activation_edit.updated", lang, title=title, days=days, daily=daily_limit, monthly=monthly_limit),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # Handle activation phone input
    if op == "activation_phone":
        if bridge_user_ctx:
            incoming = _bridge.IncomingMessage(
                platform="telegram",
                user_id=bridge_user_ctx.user_id,
                text=txt,
                raw=raw_payload,
            )
            response = await _bridge.handle_text(bridge_user_ctx, incoming, context=context)
            if response:
                await _send_bridge_responses(update, response, context=context)
                if isinstance(response, _bridge.BridgeResponse):
                    actions = response.actions or {}
                    if actions.get("clear_activation_state"):
                        context.user_data["await"] = None
                        context.user_data.pop("activation_cc", None)
            return
        context.user_data["await"] = None
        context.user_data.pop("activation_cc", None)
        await update.message.reply_text(
            _bridge.t("activation.error.retry", lang),
            parse_mode=ParseMode.HTML,
        )
        return

    if op == "language_choice":
        if bridge_user_ctx:
            incoming_lang = _bridge.IncomingMessage(
                platform="telegram",
                user_id=bridge_user_ctx.user_id,
                text=txt,
                raw=raw_payload,
            )
            response = await _bridge.handle_text(bridge_user_ctx, incoming_lang, context=context)
            if response:
                await _send_bridge_responses(update, response, context=context)
                if isinstance(response, _bridge.BridgeResponse):
                    actions = response.actions or {}
                    _apply_bridge_menu_actions(context, actions)
            return
        context.user_data.pop("await", None)
        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء اختيار اللغة. أعد فتح قائمة اللغات من القائمة الرئيسية.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Handle set phone from whoami
    if op == "set_phone":
        cc = context.user_data.get("set_phone_cc")
        phone = _normalize_phone(txt, cc)
        if not phone:
            cc_hint = _bridge.t("activation.invalid_cc_hint", lang, cc=cc) if cc else ""
            warn = _bridge.t("activation.invalid", lang, cc_hint=cc_hint)
            await update.message.reply_text(warn, parse_mode=ParseMode.HTML)
            return
        db = _load_db()
        tg_id = str(update.effective_user.id)
        u = _ensure_user(db, tg_id, update.effective_user.username)
        u["phone"] = phone
        _save_db(db)
        context.user_data["await"] = None
        context.user_data.pop("set_phone_cc", None)
        await update.message.reply_text(_bridge.t("whoami.phone.saved", lang, phone=phone), parse_mode=ParseMode.HTML)
        # إعادة عرض بياناتي
        await whoami_command(update, context)
        return

    # ===== إدخال الأدمن المُنتظر =====
    if _is_admin_tg(tg_id) and context.user_data.get("await"):
        pending = context.user_data["await"]
        op = pending.get("op"); target = pending.get("target")
        db = _load_db(); u = None
        if target:
            u = _ensure_user(db, target, None)

        # ===== Settings inputs =====
        if op in ("set_api_token",):
            context.user_data["await"] = None
            env_map = {
                "set_api_token": "API_TOKEN",
            }
            env_var = env_map.get(op, "")
            await update.message.reply_text(
                _bridge.t("settings.env.locked", lang, env_var=env_var),
                parse_mode=ParseMode.HTML,
            )
            return

        if op == "add_super_admin":
            try:
                new_id = ''.join(filter(str.isdigit, txt.strip()))
                if not new_id:
                    await update.message.reply_text(_bridge.t("settings.await.add_super_admin.id_digits_example", lang), parse_mode=ParseMode.HTML)
                    return
                
                # التحقق من أن المستخدم موجود وفعال في Telegram
                try:
                    user_chat = await context.bot.get_chat(chat_id=int(new_id))
                    user_name = user_chat.first_name or user_chat.username or "غير معروف"
                    user_username = f"@{user_chat.username}" if user_chat.username else "لا يوجد"
                except Exception as chat_error:
                    await update.message.reply_text(
                        _bridge.t("settings.await.add_super_admin.verify_failed", lang, tg_id=new_id),
                        parse_mode=ParseMode.HTML
                    )
                    context.user_data["await"] = None
                    return
                
                db = _load_db()
                supers = _db_super_admins(db)
                
                if new_id in supers:
                    await update.message.reply_text(
                        _bridge.t("settings.await.add_super_admin.already_super", lang, tg_id=new_id, name=user_name, username=user_username),
                        parse_mode=ParseMode.HTML
                    )
                    context.user_data["await"] = None
                    return
                
                if new_id in _env_super_admins():
                    await update.message.reply_text(
                        _bridge.t("settings.await.add_super_admin.env_exists", lang, tg_id=new_id, name=user_name, username=user_username),
                        parse_mode=ParseMode.HTML
                    )
                    context.user_data["await"] = None
                    return
                
                # إضافة السوبر أدمن الجديد
                if "super_admins" not in db:
                    db["super_admins"] = []
                if new_id not in db["super_admins"]:
                    db["super_admins"].append(new_id)
                    _save_db(db)
                    await update.message.reply_text(
                        _bridge.t("settings.await.add_super_admin.added_detail", lang, tg_id=new_id, name=user_name, username=user_username),
                        parse_mode=ParseMode.HTML
                    )
                    await _notify_supers(context, _bridge.t("settings.supers.add.notify", lang, target=new_id, by=tg_id))
                else:
                    await update.message.reply_text(
                        _bridge.t("settings.await.add_super_admin.already_db_detail", lang, tg_id=new_id, name=user_name, username=user_username),
                        parse_mode=ParseMode.HTML
                    )
                
                context.user_data["await"] = None
                return
            except ValueError:
                await update.message.reply_text(_bridge.t("settings.await.add_super_admin.id_digits_example", lang), parse_mode=ParseMode.HTML)
                context.user_data["await"] = None
                return
            except Exception as e:
                await update.message.reply_text(
                    _bridge.t("settings.await.add_super_admin.error", lang, error=str(e)),
                    parse_mode=ParseMode.HTML
                )
                context.user_data["await"] = None
                return

        if op == "activate_custom":
            try:
                first_activation = not bool(u.get("activation_date"))
                raw_parts = [p.strip() for p in txt.split(",")]
                parts = [p for p in raw_parts if p]
                add_bal = 0
                if len(parts) >= 3:
                    days = int(parts[0])
                    daily_limit = int(parts[1])
                    monthly_limit = int(parts[2])
                    if len(parts) >= 4:
                        add_bal = int(parts[3])
                elif len(parts) == 2:
                    # تنسيق قديم: أيام,رصيد — نحتفظ بالحدود الحالية
                    days = int(parts[0])
                    add_bal = int(parts[1])
                    limits = u.get("limits", {})
                    daily_limit = int(limits.get("daily", 200))
                    monthly_limit = int(limits.get("monthly", 500))
                else:
                    raise ValueError
                u["is_active"] = True
                u["plan"] = "custom"
                u["activation_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                u["expiry_date"] = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
                extra_reports = max(0, add_bal)
                effective_monthly = monthly_limit + extra_reports
                _set_user_limits(u, daily_limit=daily_limit, monthly_limit=effective_monthly)
                if first_activation:
                    _set_user_report_lang(u, "en")
                _audit(
                    u,
                    tg_id,
                    "activate_custom",
                    days=days,
                    daily_limit=daily_limit,
                    monthly_limit=effective_monthly,
                    extra_reports=extra_reports,
                )
                _remove_pending_request(db, target)
                _save_db(db)
                await update.message.reply_text(_bridge.t("admin.activation.custom.done", lang), parse_mode=ParseMode.HTML)
                bonus_line = (
                    f"\n• تمت إضافة <b>{extra_reports}</b> تقرير إلى حدك الشهري (الآن <b>{effective_monthly}</b>)."
                    if extra_reports
                    else ""
                )

                phone, platform = _activation_request_info(db, target, u)
                is_whatsapp = _is_probable_whatsapp_user(target_tg=target, user=u, platform=platform, phone=phone)
                activation_msg = (
                    f"✅ تفعيل {days} يوم.\n"
                    f"• حد يومي: <b>{daily_limit}</b> تقرير\n"
                    f"• حد شهري: <b>{effective_monthly}</b> تقرير\n"
                    f"• الانتهاء: <code>{u['expiry_date']}</code>"
                    + bonus_line
                )
                await _notify_user(context, target, activation_msg, preferred_channel=("wa" if is_whatsapp else "tg"))
                await _post_activation_admin_notice_if_needed(
                    context,
                    db=db,
                    user=u,
                    target_tg=target,
                    first_activation=first_activation,
                    is_whatsapp=is_whatsapp,
                )
                await _notify_supers(context, f"✅ (Admin:{tg_id}) تفعيل مخصّص للمستخدم {_fmt_tg_with_phone(target)}.")
                context.user_data["await"] = None
                return
            except Exception:
                await update.message.reply_text(
                    _bridge.t("admin.activation.custom.format_hint", lang),
                    parse_mode=ParseMode.HTML,
                )
                return

        if op == "renew_custom":
            try:
                add_days = int(txt.strip())
                today = datetime.utcnow().date()
                exp = u.get("expiry_date")
                if exp:
                    try: exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
                    except Exception: exp_d = today
                else:
                    exp_d = today
                base = max(today, exp_d)
                u["expiry_date"] = (base + timedelta(days=add_days)).strftime("%Y-%m-%d")
                u["is_active"] = True
                if not u.get("activation_date"):
                    u["activation_date"] = today.strftime("%Y-%m-%d")
                _audit(u, tg_id, "renew_custom", add_days=add_days)
                _save_db(db)
                await update.message.reply_text(_bridge.t("admin.renew.custom.done", lang), parse_mode=ParseMode.HTML)
                await _notify_user(context, target, f"♻️ تجديد {add_days} يوم. الانتهاء: <code>{u['expiry_date']}</code>")
                await _notify_supers(context, f"♻️ (Admin:{tg_id}) جدد {add_days} يوم للمستخدم {_fmt_tg_with_phone(target)}.")
                context.user_data["await"] = None
                return
            except Exception:
                await update.message.reply_text(_bridge.t("admin.renew.invalid_days", lang), parse_mode=ParseMode.HTML)
                return

        if op == "balance_edit":
            try:
                new_remaining = max(0, int(txt.strip()))
                old_balance = max(0, _current_balance(u))
                limits = u.setdefault("limits", {})
                monthly_limit = _safe_int(limits.get("monthly"))
                if monthly_limit <= 0 or new_remaining > monthly_limit:
                    monthly_limit = new_remaining
                limits["monthly"] = monthly_limit
                limits["month_used"] = max(0, monthly_limit - new_remaining)
                delta = new_remaining - old_balance
                _audit(
                    u,
                    tg_id,
                    "balance_edit",
                    old_remaining=old_balance,
                    new_remaining=new_remaining,
                    delta=delta,
                    monthly_limit=monthly_limit,
                )
                _save_db(db)
                await update.message.reply_text(
                    _bridge.t("admin.balance.updated", lang, old=old_balance, new=new_remaining, delta=(f"+{delta}" if delta >= 0 else str(delta))),
                    parse_mode=ParseMode.HTML,
                )
                await _notify_user(
                    context,
                    target,
                    (
                        "💳 <b>تم تحديث رصيدك</b>\n"
                        f"• الرصيد المتبقي الآن: <b>{new_remaining}</b> تقرير\n"
                        f"• الحد الشهري الإجمالي: <b>{monthly_limit}</b> تقرير"
                    ),
                )
                await _notify_supers(
                    context,
                    f"💳 (Admin:{tg_id}) ضبط الرصيد لـ {_fmt_tg_with_phone(target)} من {old_balance} إلى {new_remaining} (الحد الآن {monthly_limit}).",
                )
                context.user_data["await"] = None
                return
            except Exception:
                await update.message.reply_text(_bridge.t("admin.balance.invalid_number", lang), parse_mode=ParseMode.HTML)
                return

        if op == "set_name":
            name = txt.strip()
            u["custom_name"] = name
            _audit(u, tg_id, "set_name", value=name)
            _save_db(db)
            await update.message.reply_text(_bridge.t("admin.name_set", lang), parse_mode=ParseMode.HTML)
            await _notify_user(context, target, f"🏷️ تم تعيين اسمك إلى: <b>{name}</b>")
            await _notify_supers(context, f"🏷️ (Admin:{tg_id}) عيّن اسم {_fmt_tg_with_phone(target)} إلى: {name}")
            context.user_data["await"] = None
            return

        if op == "note_set":
            note = txt.strip()
            if not note or note.lower() in ("حذف", "delete", "مسح"):
                u["notes"] = ""
                _audit(u, tg_id, "note_delete")
                await update.message.reply_text(_bridge.t("admin.note_deleted", lang), parse_mode=ParseMode.HTML)
            else:
                u["notes"] = note
                _audit(u, tg_id, "note_set", text=note[:100])
                await update.message.reply_text(_bridge.t("admin.note_saved", lang), parse_mode=ParseMode.HTML)
            _save_db(db)
            context.user_data["await"] = None
            return

        if op == "notify_user":
            msg = txt.strip()
            _audit(u, tg_id, "notify_user", text=msg[:120])
            _save_db(db)
            await _notify_user(context, target, f"📬 رسالة من الإدارة:\n{escape(msg)}")
            await update.message.reply_text(_bridge.t("admin.notify.sent", lang), parse_mode=ParseMode.HTML)
            context.user_data["await"] = None
            return

        if op == "notify_bulk":
            # دعم الإرسال كنص فقط أو كصورة مع تعليق (Caption)
            incoming_text = (update.message.text or update.message.caption or "") or ""
            msg = incoming_text.strip()
            has_photo = bool(getattr(update.message, "photo", None))
            photo_file_id = None
            if has_photo:
                try:
                    # نأخذ أعلى دقة من الصور المرسلة
                    photo_sizes = update.message.photo or []
                    if photo_sizes:
                        photo_file_id = photo_sizes[-1].file_id
                except Exception:
                    photo_file_id = None
            if not msg and not photo_file_id:
                await update.message.reply_text(_bridge.t("admin.notify_bulk.empty", lang), parse_mode=ParseMode.HTML)
                return
            
            targets = aw.get("targets")
            count = aw.get("count", 0)
            db = _load_db()
            users = list(db["users"].values())
            sent = 0
            failed = 0
            
            if targets == "all":
                target_users = users
            elif targets == "active":
                target_users = [u for u in users if u.get("is_active")]
            elif targets == "inactive":
                target_users = [u for u in users if not u.get("is_active")]
            elif isinstance(targets, list):
                target_users = [u for u in users if u.get("tg_id") in targets]
            else:
                target_users = []
            
            for u in target_users:
                try:
                    user_lang = _get_user_report_lang(u)
                    if photo_file_id:
                        # إرسال صورة مع تعليق
                        await context.bot.send_photo(
                            chat_id=int(u["tg_id"]),
                            photo=photo_file_id,
                            caption=_bridge.t("broadcast.message.header", user_lang, body=escape(msg)) if msg else _bridge.t("broadcast.message.header", user_lang, body=""),
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        # إرسال نص فقط
                        await _notify_user(context, u["tg_id"], _bridge.t("broadcast.message.header", user_lang, body=escape(msg)))
                    sent += 1
                except Exception:
                    failed += 1
            
            result_text = _bridge.t("admin.notify_bulk.result", lang, sent=sent, failed=failed, total=len(target_users))
            await update.message.reply_text(result_text, parse_mode=ParseMode.HTML, reply_markup=build_reference_menu(tg_id))
            context.user_data["await"] = None
            return

        if op == "set_daily":
            try:
                newv = int(txt.strip())
            except Exception:
                await update.message.reply_text(_bridge.t("common.enter_valid_number", lang), parse_mode=ParseMode.HTML)
                return

            limits = u.setdefault("limits", {})
            limits["daily"] = max(0, newv)
            _audit(u, tg_id, "set_daily", value=newv)
            _save_db(db)
            context.user_data.pop("await", None)

            target_u = _ensure_user(db, target, None)
            target_lang = _get_user_report_lang(target_u)
            await _notify_user(
                context,
                target,
                _bridge.t("limits.updated.daily.user", target_lang, value=limits["daily"], admin=tg_id),
            )

            success = _bridge.t("limits.updated.daily", lang)
            page = (pending or {}).get("users_page")
            if page is None:
                await update.message.reply_text(success, parse_mode=ParseMode.HTML)
            else:
                page_idx = max(0, _safe_int(page, 0))
                await _panel_message(
                    update,
                    context,
                    f"{success}\n\n{_bridge.t('users.panel.header', lang)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_users_keyboard(db, page_idx, 8, lang)
                )
            return

        if op == "set_monthly":
            try:
                newv = int(txt.strip())
            except Exception:
                await update.message.reply_text(_bridge.t("common.enter_valid_number", lang), parse_mode=ParseMode.HTML)
                return

            limits = u.setdefault("limits", {})
            limits["monthly"] = max(0, newv)
            _audit(u, tg_id, "set_monthly", value=newv)
            _save_db(db)
            context.user_data.pop("await", None)

            target_u = _ensure_user(db, target, None)
            target_lang = _get_user_report_lang(target_u)
            await _notify_user(
                context,
                target,
                _bridge.t("limits.updated.monthly.user", target_lang, value=limits["monthly"], admin=tg_id),
            )

            success = _bridge.t("limits.updated.monthly", lang)
            page = (pending or {}).get("users_page")
            if page is None:
                await update.message.reply_text(success, parse_mode=ParseMode.HTML)
            else:
                page_idx = max(0, _safe_int(page, 0))
                await _panel_message(
                    update,
                    context,
                    f"{success}\n\n{_bridge.t('users.panel.header', lang)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_users_keyboard(db, page_idx, 8, lang)
                )
            return

    # ===== Bridge delegation (platform-agnostic flows) =====
    # IMPORTANT: VIN text should always start report extraction immediately.
    # If we delegate to bridge first, it may respond with a menu action and the user
    # experiences an unexpected main-menu open instead of report generation.

    bridge_text_payload = txt or ((update.message.caption or "").strip() if update and update.message else "")
    bridge_vin_candidate = _bridge._extract_vin_candidate(bridge_text_payload) if bridge_text_payload else None
    if bridge_user_ctx and bridge_user_ctx.language == "ku":
        try:
            logger.debug("ku.trace.telegram", extra={"tg_id": tg_id, "payload": bridge_text_payload, "vin": bridge_vin_candidate})
        except Exception:
            pass

    if bridge_user_ctx and update.message and getattr(update.message, "photo", None):
        try:
            file_id = update.message.photo[-1].file_id if update.message.photo else None
        except Exception:
            file_id = None
        if file_id:
            photo_text = (update.message.caption or txt or "").strip() or None
            incoming_photo = _bridge.IncomingMessage(
                platform="telegram",
                user_id=bridge_user_ctx.user_id,
                text=photo_text,
                caption=update.message.caption,
                media_url=file_id,
                raw=raw_payload,
            )
            media_fetcher = _build_telegram_media_fetcher(context)
            bridge_photo_responses = await _bridge.handle_photo(
                bridge_user_ctx,
                incoming_photo,
                media_fetcher=media_fetcher,
            )
            if bridge_photo_responses:
                await _send_bridge_responses(update, bridge_photo_responses, context=context)
                return

    # Detect VIN before routing plain text to bridge.
    vin = bridge_vin_candidate or _bridge._extract_vin_candidate(txt) or _norm_vin(txt)

    if bridge_user_ctx and not vin:
        incoming_text = _bridge.IncomingMessage(
            platform="telegram",
            user_id=bridge_user_ctx.user_id,
            text=bridge_text_payload or None,
            raw=raw_payload,
        )
        bridge_text_responses = await _bridge.handle_text(bridge_user_ctx, incoming_text, context=context)
        if bridge_text_responses:
            # Intercept menu action to render native Telegram keyboard (Old System)
            if isinstance(bridge_text_responses, _bridge.BridgeResponse) and bridge_text_responses.actions.get("menu"):
                lang_menu = bridge_user_ctx.language or lang
                await _panel_message(
                    update,
                    context,
                    _main_menu_prompt_text(lang_menu),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_main_menu(tg_id)
                )
                if isinstance(chat_data, dict):
                    chat_data["suppress_fallback"] = True
                return

            await _send_bridge_responses(update, bridge_text_responses, context=context)
            if isinstance(chat_data, dict):
                chat_data["suppress_fallback"] = True
            return

    # ===== VIN detection =====
    # استثناء زريّ المساعدة والتقرير الجديد قبل فحص VIN
    if txt == "🆘 المساعدة والتواصل":
        lang = _get_user_report_lang(u) if u else None
        text_msg = _bridge.t("help.contact", lang)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(_bridge.t("help.whatsapp", lang) or "📞 واتساب", url="https://wa.me/962795378832"),
                InlineKeyboardButton(_bridge.t("help.email", lang) or "✉️ إيميل", url="mailto:info@dejavuplus.com")
            ],
            [
                InlineKeyboardButton(_bridge.t("help.website", lang) or "🌐 الموقع", url="https://www.dejavuplus.com")
            ],
            [
                InlineKeyboardButton(_bridge.t("action.home", lang) or "🏠 القائمة الرئيسية", callback_data="main_menu:show")
            ]
        ])
        await update.message.reply_text(text_msg, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if txt == "📄 تقرير جديد":
        await new_report_command(update, context)
        return
    if txt == "📢 إشعارات":
        await broadcast_menu_handler(update, context)
        return

    if not vin:
        try:
            await update.message.reply_text(_menu_hint_text(lang))
        except Exception:
            pass
        return

    if bridge_user_ctx and bridge_user_ctx.language == "ku":
        try:
            logger.debug("ku.trace.vin_branch", extra={"tg_id": tg_id, "vin": vin, "raw": txt})
        except Exception:
            pass
    if vin:
        # VIN flow is handled here (progress + background job). Prevent the global
        # smart fallback handler from also firing on the same update and opening
        # the main menu.
        if isinstance(chat_data, dict):
            chat_data["suppress_fallback"] = True
        try:
            if isinstance(getattr(context, "user_data", None), dict):
                context.user_data["suppress_fallback"] = True
        except Exception:
            pass
        vins = _tg_extract_all_vins(txt, primary=vin)
        if not vins:
            try:
                await update.message.reply_text(_menu_hint_text(lang))
            except Exception:
                pass
            return

        # Build a quick header snapshot once, then ACK each VIN immediately with its own progress message.
        report_lang = get_report_default_lang() or "en"
        header_snapshot: Dict[str, Any] = {
            "monthly_remaining": None,
            "monthly_limit": 0,
            "today_used": 0,
            "daily_limit": 0,
            "days_left": None,
        }
        try:
            async with _user_lock(tg_id):
                db0 = _load_db()
                u0 = _ensure_user(db0, tg_id, update.effective_user.username)
                report_lang = _get_user_report_lang(u0)
                header_snapshot = {
                    "monthly_remaining": _remaining_monthly_reports(u0),
                    "monthly_limit": _safe_int((u0.get("limits", {}) or {}).get("monthly")),
                    "today_used": _safe_int((u0.get("limits", {}) or {}).get("today_used")),
                    "daily_limit": _safe_int((u0.get("limits", {}) or {}).get("daily")),
                    "days_left": _days_left(u0.get("expiry_date")),
                }
        except Exception:
            pass

        base_key = None
        try:
            base_key = str(getattr(update.effective_message, "message_id", None) or "")
        except Exception:
            base_key = ""
        if not base_key:
            try:
                base_key = str(getattr(update, "update_id", None) or "")
            except Exception:
                base_key = ""
        if not base_key:
            base_key = str(int(time.time()))

        for i, v in enumerate(vins):
            header = _build_vin_progress_header(v, lang=report_lang, **header_snapshot)
            progress_payload = header + "\n" + _make_progress_bar(0)
            try:
                msg = await update.message.reply_text(progress_payload, parse_mode=ParseMode.HTML)
            except Exception:
                msg = await update.message.reply_text(re.sub(r"<[^>]+>", "", progress_payload))

            job = {
                "tg_id": str(tg_id),
                "chat_id": int(update.effective_chat.id),
                "username": (update.effective_user.username if update and update.effective_user else None),
                "first_name": (update.effective_user.first_name if update and update.effective_user else None),
                "vin": str(v),
                "raw_payload": raw_payload,
                "request_key": f"{base_key}:{i}",
                "progress_message_id": int(getattr(msg, "message_id", 0) or 0),
            }
            await _tg_submit_report_job(context, job)
        return

    # ===== Menu routing =====
    if txt in MAIN_MENU_TEXTS:
        lang = _get_user_report_lang(u) if u else None
        # If Telegram's UI language is English and user lang is unset/Arabic, align to English automatically
        try:
            ui_lang = (update.effective_user.language_code or "").lower()
        except Exception:
            ui_lang = ""
        if u and ui_lang.startswith("en") and lang != "en":
            _set_user_report_lang(u, "en")
            _save_db(db)
            lang = "en"
        header = _bridge.t("menu.header", lang)
        instructions = _bridge.t("menu.instructions", lang)
        text = f"{header}\n\n━━━━━━━━━━━━━━━━━━━━\n{instructions}"
        await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=build_reference_menu(tg_id))
        return
    
    if txt in ("👤 بياناتي", "💳 رصيدي", "🛂 طلب تفعيل", "👥 المستخدمون", "📊 إحصائيات", "📝 قائمة المنتظرين", "⚙️ إعدادات النظام", "🌐 لغة التقرير") + LANG_BUTTON_TEXTS:
        if txt == "👤 بياناتي": return await whoami_command(update, context)
        if txt == "💳 رصيدي": return await balance_command(update, context)
        if txt == "🛂 طلب تفعيل": return await request_activation_command(update, context)
        
        if txt == "🌐 لغة التقرير" or txt in LANG_BUTTON_TEXTS:
            # عرض قائمة اختيار اللغة
            db = _load_db()
            u = _ensure_user(db, tg_id, update.effective_user.username)
            current_lang = _get_user_report_lang(u)
            
            text = _bridge.t("language.change.prompt", _get_user_report_lang(u), current=_lang_label(current_lang))
            lang_rows = _language_choice_rows(current_lang, lambda code: f"lang:user_set:{code}")
            lang_rows.append([InlineKeyboardButton(_bridge.t("admin.users.main", current_lang), callback_data="main_menu:show")])
            kb = InlineKeyboardMarkup(lang_rows)
            await _panel_message(update, context, text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        if _is_admin_tg(tg_id) or _is_super_admin(tg_id):
            if txt == "👥 المستخدمون":
                # عرض قائمة المستخدمين مع القائمة المرجعية
                db = _load_db()
                admin_lang = _get_user_report_lang(_ensure_user(db, tg_id, update.effective_user.username))
                await _panel_message(update, context, _bridge.t("users.panel.header", admin_lang), parse_mode=ParseMode.HTML, reply_markup=_users_keyboard(db, 0, 8, admin_lang))
                return
            if txt == "📊 إحصائيات": return await admin_stats_command(update, context)
            if txt == "📝 قائمة المنتظرين": return await pending_list_command(update, context)
            if txt == "⚙️ إعدادات النظام":
                try:
                    # التحقق من أن المستخدم سوبر أدمن فقط
                    if not _is_super_admin(tg_id):
                        env_admins = _env_super_admins()
                        db_check = _load_db()
                        db_admins = _db_super_admins(db_check)
                        debug_info = (
                            f"{_bridge.t('admin.settings.super_only', lang)}\n\n"
                            f"ID: <code>{tg_id}</code>\n"
                            f"ENV super admins: {', '.join(map(str, env_admins)) if env_admins else '—'}\n"
                            f"DB super admins: {', '.join(db_admins) if db_admins else '—'}\n\n"
                            f"<i>Set TELEGRAM_SUPER_ADMINS in .env to grant access.</i>"
                        )
                        return await _panel_message(
                            update,
                            context,
                            debug_info,
                            parse_mode=ParseMode.HTML,
                            reply_markup=build_main_menu(tg_id)
                        )
                    return await open_settings_panel(update, context)
                except Exception as e:
                    logger.error(f"Error handling settings button: {e}", exc_info=True)
                    return await _panel_message(
                        update,
                        context,
                        _bridge.t("admin.settings.error", lang, error=str(e)),
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_main_menu(tg_id)
                    )
        return

    # غير معروف: لا تفتح القائمة تلقائياً، أعطِ تلميحاً فقط
    try:
        await update.message.reply_text(_menu_hint_text(lang))
    except Exception:
        pass
    return

# =================== Contact user (no username) ===================
async def contact_user_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.data.split(":")[2]
    try:
        db_lang = _load_db()
        admin_user = _ensure_user(db_lang, str(q.from_user.id), q.from_user.username if q.from_user else None)
        lang = _get_user_report_lang(admin_user)
    except Exception:
        lang = None
    return await q.edit_message_text(
        _bridge.t("contact.no_username", lang, tg=tg),
        parse_mode=ParseMode.HTML
    )
async def user_phone_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        db_lang = _load_db()
        admin_user = _ensure_user(db_lang, str(q.from_user.id), q.from_user.username if q.from_user else None)
        lang = _get_user_report_lang(admin_user)
    except Exception:
        lang = None
    try:
        _, action, sub = q.data.split(":")
    except Exception:
        action, sub = "open", ""
    if action == "phone":
        if sub == "open":
            context.user_data["await"] = {"op": "set_phone"}
            cc_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🇯🇴 +962", callback_data="user:phone_cc:+962"),
                 InlineKeyboardButton("🇸🇦 +966", callback_data="user:phone_cc:+966"),
                 InlineKeyboardButton("🇦🇪 +971", callback_data="user:phone_cc:+971")],
                [InlineKeyboardButton("🇮🇶 +964", callback_data="user:phone_cc:+964"),
                 InlineKeyboardButton("🇪🇬 +20", callback_data="user:phone_cc:+20"),
                 InlineKeyboardButton(_bridge.t("activation.cc.other", lang), callback_data="user:phone_cc:other")],
                [InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]
            ])
            return await q.edit_message_text(
                _bridge.t("whoami.phone.prompt", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=cc_kb
            )
    return


async def renewal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    caller_tg = str(q.from_user.id)
    if not _is_super_admin(caller_tg):
        await q.answer("❌ صلاحيات غير كافية", show_alert=True)
        return

    data = (q.data or "").split(":")
    if len(data) < 3:
        await q.answer("❌ بيانات غير صالحة", show_alert=True)
        return

    action = data[1]
    target_tg = data[2]
    db = _load_db()
    u = _ensure_user(db, target_tg, None)
    today = datetime.utcnow().date()
    today_str = today.strftime("%Y-%m-%d")

    if action == "auto":
        if len(data) < 6:
            await q.answer("❌ بيانات ناقصة", show_alert=True)
            return
        days = max(1, _safe_int(data[3], 30))
        daily_limit = max(1, _safe_int(data[4], 25))
        monthly_limit = max(1, _safe_int(data[5], 100))
        store = u.setdefault("last_auto_notifications", {})
        if store.get("admin_auto_renewed") == today_str:
            await q.answer("ℹ️ تم التجديد مسبقًا اليوم", show_alert=True)
            return

        exp_raw = u.get("expiry_date")
        if exp_raw:
            try:
                exp_d = datetime.strptime(exp_raw, "%Y-%m-%d").date()
            except Exception:
                exp_d = today
        else:
            exp_d = today
        base = max(today, exp_d)
        u["expiry_date"] = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        if not u.get("activation_date"):
            u["activation_date"] = today_str
        u["is_active"] = True
        if not u.get("plan"):
            u["plan"] = "custom"
        _set_user_limits(u, daily_limit=daily_limit, monthly_limit=monthly_limit)
        for key in ("expiry_overdue", "expiry_0", "expiry_day_1"):
            store.pop(key, None)
        store["admin_auto_renewed"] = today_str
        _audit(u, caller_tg, "auto_renewal", days=days, daily_limit=daily_limit, monthly_limit=monthly_limit)
        _save_db(db)

        await _notify_user(
            context,
            target_tg,
            (
                "🔁 <b>تم تجديد اشتراكك</b>\n\n"
                f"• المدة الجديدة: <b>{days}</b> يوم\n"
                f"• الحد اليومي: <b>{daily_limit}</b> تقرير\n"
                f"• الحد الشهري: <b>{monthly_limit}</b> تقرير\n"
                f"• ينتهي في: <code>{u['expiry_date']}</code>\n\n"
                "نتمنى لك استخداماً موفقاً!"
            ),
        )
        user_label = escape(_display_name(u))
        await _super_dashboard_event(
            context,
            f"تم تجديد اشتراك {_display_name(u)} تلقائياً لمدة {days} يوم بواسطة سوبر أدمن {caller_tg}",
        )

        await q.edit_message_text(
            (
                "✅ <b>تم تجديد الخطة</b>\n\n"
                f"• المستخدم: <b>{user_label}</b> ({_fmt_tg_with_phone(target_tg)})\n"
                f"• المدة: {days} يوم | {daily_limit}/{monthly_limit}\n"
                f"• ينتهي في: <code>{u['expiry_date']}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "dismiss":
        store = u.setdefault("last_auto_notifications", {})
        store["expiry_admin_day1"] = today_str
        _save_db(db)
        user_label = escape(_display_name(u))
        await q.edit_message_text(
            (
                "ℹ️ تم تجاهل تذكير التجديد لليوم.\n"
                f"المستخدم: <b>{user_label}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    await q.answer("❌ إجراء غير مدعوم", show_alert=True)


async def super_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    if not _is_super_admin(tg_id):
        await update.effective_chat.send_message("❌ هذا الأمر متاح للسوبر أدمن فقط.")
        return

    state = _super_dashboard_state(context)
    chat_id = update.effective_chat.id
    placeholder = "🛡️ يتم تجهيز لوحة السوبر المباشرة..."
    msg = None
    existing_id = state.get("listeners", {}).get(chat_id)
    if existing_id:
        try:
            msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_id,
                text=placeholder,
            )
        except Exception:
            state.get("listeners", {}).pop(chat_id, None)

    if not msg:
        msg = await update.effective_chat.send_message(placeholder)

    state.setdefault("listeners", {})[chat_id] = msg.message_id
    await _refresh_super_dashboards(context, state=state)

    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


async def super_dashboard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg_id = str(q.from_user.id)
    if not _is_super_admin(tg_id):
        await q.answer("❌ صلاحيات غير كافية", show_alert=True)
        return

    parts = (q.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "refresh"
    chat_id = q.message.chat_id if q.message else None

    if action == "refresh":
        if chat_id:
            state = _super_dashboard_state(context)
            state.setdefault("listeners", {})[chat_id] = q.message.message_id
        await _refresh_super_dashboards(context)
        await q.answer("تم التحديث")
        return

    if action == "close":
        state = _super_dashboard_state(context)
        if chat_id:
            state.get("listeners", {}).pop(chat_id, None)
        try:
            await q.edit_message_text("🛡️ تم إخفاء لوحة السوبر. أرسل /dashboard لإظهارها من جديد.")
        except Exception:
            pass
        await q.answer("تم الإخفاء")
        return

# =================== Main ===================

async def _post_shutdown_cleanup(app: Application) -> None:
    """Close shared aiohttp sessions to avoid shutdown warnings."""

    try:
        await _close_translation_session()
    except Exception:
        pass

    # Reports module also keeps a shared aiohttp session; close it on shutdown.
    try:
        await _close_reports_session()
    except Exception:
        pass

    # Close the shared Chromium/Playwright engine to avoid orphan processes on restart.
    try:
        from bot_core.services.pdf import close_pdf_engine

        await close_pdf_engine()
    except Exception:
        pass


_BG_TASKS: set[asyncio.Task[Any]] = set()


def _track_bg_task(task: asyncio.Task[Any], *, name: str) -> None:
    """Track background tasks so exceptions are never lost (prevents 'Future exception was never retrieved')."""

    _BG_TASKS.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _BG_TASKS.discard(t)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc:
            logger.warning("background task failed: %s", name, exc_info=exc)

    task.add_done_callback(_done)


async def _post_init_warmup(app: Application) -> None:
    try:
        bot_session = getattr(app.bot, "session", None)
        if bot_session and not bot_session.closed:
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("progress task failed", exc_info=True)
            await bot_session.close()
    except Exception:
        pass

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init_warmup)
        .post_shutdown(_post_shutdown_cleanup)
        .build()
    )

    # Commands - معطلة (البوت يعمل فقط بالأزرار)
    # فقط أمر start للبدء
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", super_dashboard_command))
    app.add_handler(CommandHandler("superdash", super_dashboard_command))
    # باقي الأوامر معطلة - البوت يعمل فقط بالأزرار
    # app.add_handler(CommandHandler("me", whoami_command))
    # app.add_handler(CommandHandler("balance", balance_command))
    # app.add_handler(CommandHandler("users", users_command))
    # app.add_handler(CommandHandler("stats", admin_stats_command))
    # app.add_handler(CommandHandler("pending", pending_list_command))
    # app.add_handler(CommandHandler("debug", debug_command))

    # Message button handlers (reply keyboard)
    app.add_handler(MessageHandler(filters.Regex(MAIN_MENU_BUTTON_REGEX), start_button_handler))
    app.add_handler(MessageHandler(filters.Regex("^🚀 ابدأ الآن$"), start_button_handler))  # توافق مع النسخ السابقة
    app.add_handler(MessageHandler(filters.Regex("^🆘 المساعدة والتواصل$"), help_command))
    app.add_handler(MessageHandler(filters.Regex("^📄 تقرير جديد$"), new_report_command))
    # Handler for "What can you do?" - uses centralized patterns from bridge
    _capabilities_regex = "(?i)(" + "|".join(re.escape(p) for p in _CAPABILITIES_PATTERNS) + ")"
    app.add_handler(MessageHandler(filters.Regex(_capabilities_regex), capabilities_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(main_menu_cb, pattern=r"^(main_menu|ref):"))
    app.add_handler(CallbackQueryHandler(users_status_cb, pattern=r"^users:(status|limit):"))
    app.add_handler(CallbackQueryHandler(usercard_cb, pattern=r"^(ucard|svc|limits|limitreq|lang|stats|notify|users):"))
    app.add_handler(CallbackQueryHandler(users_pager_cb, pattern=r"^users:page:"))
    app.add_handler(CallbackQueryHandler(pending_cb, pattern=r"^pending:"))
    app.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(contact_user_cb, pattern=r"^user:contact:"))
    app.add_handler(CallbackQueryHandler(user_phone_cb, pattern=r"^user:phone:"))
    app.add_handler(CallbackQueryHandler(broadcast_cb, pattern=r"^broadcast:"))
    app.add_handler(CallbackQueryHandler(help_back_cb, pattern=r"^help:back$"))
    app.add_handler(CallbackQueryHandler(help_faq_cb, pattern=r"^help:faq$"))
    app.add_handler(CallbackQueryHandler(help_capabilities_cb, pattern=r"^help:capabilities$"))
    app.add_handler(CallbackQueryHandler(report_back_cb, pattern=r"^report:back$"))
    app.add_handler(CallbackQueryHandler(vin_info_cb, pattern=r"^vin:info$"))
    app.add_handler(CallbackQueryHandler(vin_sample_cb, pattern=r"^vin:sample$"))
    app.add_handler(CallbackQueryHandler(activation_request_cb, pattern=r"^activation:request$"))
    app.add_handler(CallbackQueryHandler(activation_cc_cb, pattern=r"^activation:(cc|submit):"))
    app.add_handler(CallbackQueryHandler(report_prompt_cb, pattern=r"^report:prompt$"))
    app.add_handler(CallbackQueryHandler(super_dashboard_cb, pattern=r"^super_dash:"))
    app.add_handler(CallbackQueryHandler(renewal_cb, pattern=r"^renewal:"))

    # Text
    # Block downstream handlers once text_router consumes the update to avoid duplicate menus
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router, block=True))
    # Media with captions (ensure admin can send photo+caption for notifications)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, text_router, block=True))
    try:
        app.add_handler(MessageHandler(filters.Document.IMAGE & ~filters.COMMAND, text_router))
    except Exception:
        pass
    app.add_handler(MessageHandler(_filters.COMMAND, _commands_disabled), group=0)
    app.add_handler(MessageHandler(~_filters.COMMAND & _filters.TEXT, _smart_fallback), group=99)

    # Error handler
    app.add_error_handler(_on_error)

    if app.job_queue:
        app.job_queue.run_repeating(_auto_notifications_job, interval=1800, first=60)

    print("Bot running...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()
# =================== HELP & NEW REPORT COMMANDS ===================

async def start_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    u["onboarded"] = True
    _save_db(db)
    welcome = (
        "👋 أهلاً بك في نظام دِيجا-فو-بلَس!\n"
    )
    await _panel_message(update, context, welcome, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة المساعدة بتصميم بصري محسّن"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    lang = _get_user_report_lang(u)

    text_msg = _bridge.t("help.contact", lang)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_bridge.t("help.button.whatsapp", lang), url="https://wa.me/962795378832"),
            InlineKeyboardButton(_bridge.t("help.button.website", lang), url="https://www.dejavuplus.com")
        ],
        [
            InlineKeyboardButton(_bridge.t("help.button.capabilities", lang), callback_data="help:capabilities")
        ],
        [
            InlineKeyboardButton(_bridge.t("help.button.faq", lang), callback_data="help:faq"),
            InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")
        ]
    ])
    await _send_or_edit(update, context, text_msg, parse_mode=ParseMode.HTML, reply_markup=kb)

async def capabilities_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قدرات البوت عند كتابة السؤال مباشرة"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    lang = _get_user_report_lang(u)
    
    capabilities = _bridge.t("help.capabilities", lang)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("button.new_report", lang), callback_data="report:prompt")],
        [InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")]
    ])
    await _send_or_edit(update, context, capabilities, parse_mode=ParseMode.HTML, reply_markup=kb)

# ===== Button-only Handlers =====
async def new_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة طلب تقرير جديد بتصميم بصري محسّن"""
    tg_id = str(update.effective_user.id)
    db = _load_db()
    u = _ensure_user(db, tg_id, update.effective_user.username)
    lang = _get_user_report_lang(u)

    # سلوك ذكي: تنبيه إذا الحساب غير مفعّل أو لا يملك رصيد/حد كافٍ
    if not u.get("is_active"):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(_bridge.t("button.activation_now", lang), callback_data="activation:request")],
            [InlineKeyboardButton(_bridge.t("button.back_menu", lang), callback_data="main_menu:show")]
        ])
        text = _bridge.t("new_report.inactive", lang)
        return await _send_or_edit(update, context, text, parse_mode=ParseMode.HTML, reply_markup=kb)

    limits = u.get("limits", {}) or {}
    daily_limit = _safe_int(limits.get("daily"))
    today_used = _safe_int(limits.get("today_used"))
    monthly_limit = _safe_int(limits.get("monthly"))
    monthly_remaining = _remaining_monthly_reports(u)
    monthly_label = (
        "غير محدود"
        if monthly_remaining is None
        else f"{monthly_remaining}/{monthly_limit}"
    )
    msg = _bridge.t(
        "new_report.body",
        lang,
        today_used=today_used,
        daily_limit=daily_limit,
        monthly_label=(
            _bridge.t("usercard.unlimited", lang)
            if monthly_remaining is None
            else f"{monthly_remaining}/{monthly_limit}"
        ),
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_bridge.t("button.vin_info", lang), callback_data="vin:info"),
            InlineKeyboardButton(_bridge.t("button.vin_sample", lang), callback_data="vin:sample")
        ],
        [InlineKeyboardButton(_bridge.t("button.back_menu", lang), callback_data="main_menu:show")]
    ])
    await _send_or_edit(update, context, msg, parse_mode=ParseMode.HTML, reply_markup=kb)
async def help_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = str(q.from_user.id)
    try:
        lang = _get_user_report_lang(_ensure_user(_load_db(), tg_id, q.from_user.username))
        await q.edit_message_text(_bridge.t("help.return", lang), parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def help_faq_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username))
    faq = _bridge.t("help.faq", lang)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("button.new_report", lang), callback_data="report:prompt")],
        [InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")]
    ])
    try:
        await q.edit_message_text(faq, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=faq, parse_mode=ParseMode.HTML, reply_markup=kb)

async def help_capabilities_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قدرات البوت"""
    q = update.callback_query
    await q.answer()
    lang = _get_user_report_lang(_ensure_user(_load_db(), str(q.from_user.id), q.from_user.username))
    capabilities = _bridge.t("help.capabilities", lang)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(_bridge.t("button.new_report", lang), callback_data="report:prompt")],
        [InlineKeyboardButton(_bridge.t("action.back", lang), callback_data="main_menu:show")]
    ])
    try:
        await q.edit_message_text(capabilities, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=capabilities, parse_mode=ParseMode.HTML, reply_markup=kb)

async def report_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = str(q.from_user.id)
    try:
        lang = _get_user_report_lang(_ensure_user(_load_db(), tg_id, q.from_user.username))
        await q.edit_message_text(_bridge.t("help.return", lang), parse_mode=ParseMode.HTML)
    except Exception:
        pass
async def vin_info_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    info = (
        "رقم الشاصي (VIN) هو معرّف السيارة من 17 خانة موجود على:\n"
        "• لوحة قرب الزجاج الأمامي من جهة السائق\n"
        "• رخصة المركبة\n"
        "• باب السائق من الداخل"
    )
    admin_lang = _resolve_lang_for_tg(str(q.from_user.id))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("admin.users.back", admin_lang), callback_data="main_menu:show")]])
    try:
        await q.edit_message_text(info, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=info, parse_mode=ParseMode.HTML, reply_markup=kb)

async def vin_sample_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sample = "<code>1HGCM82633A123456</code>"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="main_menu:show")]])
    try:
        await q.edit_message_text(sample, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=sample, parse_mode=ParseMode.HTML, reply_markup=kb)

async def activation_request_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # استدعاء نفس منطق زر "🛂 طلب تفعيل" (إن وُجد) أو إرسال رسالة إرشادية
    try:
        await request_activation_command(update, context)
    except Exception:
        admin_lang = _resolve_lang_for_tg(str(q.from_user.id))
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=_bridge.t("admin.activation.hint", admin_lang),
            parse_mode=ParseMode.HTML,
        )


async def report_prompt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await new_report_command(update, context)

def _users_keyboard(db: Dict[str, Any], page: int = 0, per_page: int = 8, lang: Optional[str] = None) -> InlineKeyboardMarkup:
    lang_code = _normalize_report_lang_code(lang)
    users = list(db.get("users", {}).values())
    # Hide super-admin accounts from the users list UI.
    try:
        super_ids = set(map(str, _env_super_admins())) | set(map(str, _db_super_admins(db)))
    except Exception:
        super_ids = set()
    if super_ids:
        users = [u for u in users if str(u.get("tg_id") or "") not in super_ids]
    users.sort(
        key=lambda x: (
            not x.get("is_active", False),
            (x.get("custom_name") or x.get("tg_username") or str(x.get("tg_id") or "")),
        )
    )
    start = max(0, page) * per_page
    end = start + per_page
    chunk = users[start:end]

    rows: List[List[InlineKeyboardButton]] = []
    touched = False
    for u in chunk:
        tg_raw = u.get("tg_id")
        tg = str(tg_raw).strip() if tg_raw is not None else ""
        if not tg:
            continue

        if _auto_suspend_if_expired(u):
            touched = True

        phone = (u.get("phone") or "").strip()
        if phone:
            if re.fullmatch(r"\+?\d{7,15}", phone):
                phone_btn = InlineKeyboardButton("📞 " + phone, url=f"https://wa.me/{phone.lstrip('+')}")
            else:
                phone_btn = InlineKeyboardButton("📞 " + phone, callback_data=f"user:contact:{tg}")
        else:
            phone_btn = InlineKeyboardButton(_bridge.t("admin.users.phone.missing", lang_code), callback_data=f"user:contact:{tg}")

        expiry_raw = u.get("expiry_date") or "-"
        expiry_display = _fmt_date(expiry_raw) if expiry_raw not in (None, "-") else _bridge.t("admin.users.expiry.unset", lang_code)
        status_label, status_state = _user_status_meta(u)

        expiry_btn = InlineKeyboardButton(f"⏰ {expiry_display}", callback_data=f"ucard:activate_custom:{tg}")
        status_btn = InlineKeyboardButton(
            status_label,
            callback_data=f"users:status:{status_state}:{tg}:{page}"
        )
        delete_btn = InlineKeyboardButton(_bridge.t("admin.users.delete", lang_code), callback_data=f"ucard:delete:{tg}")

        rows.append([phone_btn, expiry_btn, status_btn, delete_btn])

    if not rows:
        rows.append([InlineKeyboardButton(_bridge.t("admin.users.none", lang_code), callback_data="users:none")])

    nav: List[InlineKeyboardButton] = []
    if start > 0:
        nav.append(InlineKeyboardButton(_bridge.t("admin.users.prev", lang_code), callback_data=f"users:page:{max(page-1,0)}"))
    if end < len(users):
        nav.append(InlineKeyboardButton(_bridge.t("admin.users.next", lang_code), callback_data=f"users:page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(_bridge.t("admin.users.main", lang_code), callback_data="main_menu:show")])
    if touched:
        _save_db(db)
    return InlineKeyboardMarkup(rows)


async def _refresh_users_overview(q, db: Dict[str, Any], page: int, lang: Optional[str] = None) -> None:
    lang_code = _normalize_report_lang_code(lang)
    await q.edit_message_text(
        _bridge.t("users.panel.header", lang_code),
        parse_mode=ParseMode.HTML,
        reply_markup=_users_keyboard(db, page, 8, lang_code)
    )

async def users_status_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    admin_tg = str(q.from_user.id)
    db = _load_db()
    u_admin = _ensure_user(db, admin_tg, q.from_user.username if q and q.from_user else None)
    lang = _get_user_report_lang(u_admin)
    if not (_is_admin_tg(admin_tg) or _is_super_admin(admin_tg)):
        return await q.edit_message_text(_unauthorized(lang), parse_mode=ParseMode.HTML)

    parts = q.data.split(":")
    if len(parts) < 3:
        return await q.answer(_bridge.t("common.invalid_button", lang), show_alert=True)

    mode = parts[1]
    sub_action = parts[2]
    target_tg = parts[3] if len(parts) > 3 else ""
    page = int(parts[4]) if len(parts) > 4 else 0

    if not target_tg:
        return await q.answer(_bridge.t("admin.user.unknown", lang), show_alert=True)

    db = _load_db()
    u = _ensure_user(db, target_tg, None)

    if mode == "status":
        if sub_action == "active":
            if not u.get("is_active"):
                return await q.answer(_bridge.t("admin.user.already_stopped", lang), show_alert=True)
            u["is_active"] = False
            _audit(u, admin_tg, "manual_suspend")
            _save_db(db)
            await _notify_user(context, target_tg, _bridge.t("admin.user.suspend.notify", lang))
            await _notify_supers(
                context,
                _bridge.t(
                    "admin.user.suspend.log",
                    lang,
                    admin=_fmt_tg_with_phone(admin_tg),
                    user=_fmt_tg_with_phone(target_tg),
                ),
            )
            await q.answer(_bridge.t("admin.user.suspend.toast", lang))
            return await _refresh_users_overview(q, db, page, lang)

        if sub_action == "stopped":
            reactivate_text = _bridge.t("admin.user.reactivate.prompt", lang, name=_display_name(u))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(_bridge.t("admin.user.reactivate.option.trial", lang), callback_data=f"pending:trial:{target_tg}")],
                [InlineKeyboardButton(_bridge.t("admin.user.reactivate.option.monthly", lang), callback_data=f"pending:monthly:{target_tg}")],
                [InlineKeyboardButton(_bridge.t("admin.user.reactivate.option.custom", lang), callback_data=f"ucard:activate_custom:{target_tg}")],
                [InlineKeyboardButton(_bridge.t("admin.user.reactivate.option.open_card", lang), callback_data=f"ucard:open:{target_tg}")]
            ])
            await context.bot.send_message(
                chat_id=int(admin_tg),
                text=reactivate_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
            return await q.answer(_bridge.t("admin.user.reactivate.sent", lang))

        if sub_action == "limit":
            limit_text = _bridge.t("admin.limit.prompt", lang, name=_display_name(u))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(_bridge.t("admin.limit.option.daily", lang), callback_data=f"users:limit:daily:{target_tg}:{page}")],
                [InlineKeyboardButton(_bridge.t("admin.limit.option.monthly", lang), callback_data=f"users:limit:monthly:{target_tg}:{page}")],
                [InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]
            ])
            await context.bot.send_message(
                chat_id=int(admin_tg),
                text=limit_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
            return await q.answer(_bridge.t("admin.limit.sent", lang))

        return await q.answer(_bridge.t("common.unknown_option", lang), show_alert=True)

    if mode == "limit":
        if sub_action not in {"daily", "monthly"}:
            return await q.answer(_bridge.t("common.unknown_option", lang), show_alert=True)
        op = "set_daily" if sub_action == "daily" else "set_monthly"
        prompt = _bridge.t("admin.limit.prompt.daily", lang) if sub_action == "daily" else _bridge.t("admin.limit.prompt.monthly", lang)
        context.user_data["await"] = {"op": op, "target": target_tg, "users_page": page}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(_bridge.t("action.cancel", lang), callback_data="main_menu:show")]])
        return await q.edit_message_text(prompt, parse_mode=ParseMode.HTML, reply_markup=kb)

    return await q.answer(_bridge.t("common.invalid_button", lang), show_alert=True)

# === Buttons-only mode: disable ALL slash-commands and show menu instead ===
async def _commands_disabled(update, context):
    try:
        tg_id = str(update.effective_user.id) if update and update.effective_user else ""
        db = _load_db()
        u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None) if tg_id else None
        lang = _get_user_report_lang(u) if u else None
        if update and update.message:
            text = _main_menu_prompt_text(lang)
            return await _panel_message(
                update,
                context,
                text,
                reply_markup=build_main_menu(tg_id, lang),
                parse_mode=ParseMode.HTML
            )
    except Exception:
        pass

# === Global smart fallback handler (text) ===
async def _smart_fallback(update, context):
    chat_data = context.chat_data if isinstance(getattr(context, "chat_data", None), dict) else {}
    user_data = context.user_data if isinstance(getattr(context, "user_data", None), dict) else {}
    if (isinstance(chat_data, dict) and chat_data.pop("suppress_fallback", False)) or (
        isinstance(user_data, dict) and user_data.pop("suppress_fallback", False)
    ):
        return

    txt = (update.message.text or "").strip() if update and update.message and update.message.text else ""
    tg_id = str(update.effective_user.id) if update and update.effective_user else ""

    # If the message looks like a phone number (e.g. WhatsApp +countrycode...),
    # do not treat it as a VIN-like candidate and do not override previous handlers.
    try:
        phone_candidate = (txt or "").strip().replace(" ", "").replace("-", "")
        if phone_candidate.startswith("00") and phone_candidate[2:].isdigit():
            phone_candidate = f"+{phone_candidate[2:]}"
        if phone_candidate.startswith("+") and phone_candidate[1:].isdigit() and 9 <= len(phone_candidate) <= 16:
            return
        if phone_candidate.isdigit() and 10 <= len(phone_candidate) <= 16:
            return
    except Exception:
        pass

    # Resolve language for friendly fallback response
    try:
        db = _load_db()
        u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None) if tg_id else None
        lang = _get_user_report_lang(u) if u else None
    except Exception:
        lang = None

    vin_try = _bridge._extract_vin_candidate(txt) or _norm_vin(txt)
    looks_like_vin = bool(re.search(r"[A-Za-z0-9]{10,}", (txt or "")))

    # If this message contains a valid VIN candidate, do not open the main menu.
    # (The VIN report flow is handled elsewhere.)
    if vin_try:
        return

    if txt in ALL_BUTTON_LABELS or not txt:
        return 
    if looks_like_vin and not vin_try:
        return await _panel_message(
            update,
            context,
            _bridge.t("common.invalid_vin", lang),
            reply_markup=build_main_menu(tg_id),
            parse_mode=ParseMode.HTML
        )

    # Default fallback: gently redirect to main menu
    return await _panel_message(
        update,
        context,
        _main_menu_prompt_text(lang),
        reply_markup=build_main_menu(tg_id, lang),
        parse_mode=ParseMode.HTML,
    )
# === Global error handler ===
async def _on_error(update, context):
    try:
        # log minimal info and show a friendly message (edit or send)
        tg_id = str(update.effective_user.id) if update and update.effective_user else ""
        db = _load_db()
        u = _ensure_user(db, tg_id, update.effective_user.username if update and update.effective_user else None) if tg_id else None
        lang = _get_user_report_lang(u) if u else None
        msg = _main_menu_prompt_text(lang)
        await _send_or_edit(update, context, msg, parse_mode=ParseMode.HTML, reply_markup=build_main_menu(tg_id, lang))
    except Exception:
        pass

async def _auto_notifications_job(context: ContextTypes.DEFAULT_TYPE):
    await check_and_send_auto_notifications(context)

if __name__ == "__main__":
    print("Starting Carfax bot...")
    main()
