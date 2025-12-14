# type: ignore
# pyright: reportGeneralTypeIssues=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# -*- coding: utf-8 -*-
"""Platform-agnostic bridge for shared bot flows."""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Tuple
from bot_core.auth import is_admin_tg as _is_admin_tg, is_super_admin as _is_super_admin
from bot_core.services.notifications import notify_supers
from bot_core.services.images import download_image_bytes
from bot_core.services.translation import translate_batch, _latin_ku_to_arabic as _ku_to_arabic  # type: ignore
from bot_core.services.reports import ReportResult, generate_vin_report
from bot_core.storage import (
    ensure_user,
    load_db,
    save_db,
    bump_usage,
    days_left,
    display_name,
    fmt_date,
    format_tg_with_phone,
    now_str,
    remaining_monthly_reports,
    reserve_credit,
    refund_credit,
    commit_credit,
)
from bot_core.utils.vin import normalize_vin, VIN_RE

# Telegram inline keyboard types are optional: if unavailable (e.g., headless import), actions still work without buttons.
try:  # pragma: no cover - optional dependency guard
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
except Exception:  # pragma: no cover - safe fallback when telegram is absent
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None


LOGGER = logging.getLogger(__name__)

VIN_COMMAND_PREFIXES = ("/vin", "/report", "/carfax", "vin:", "report:")
VIN_TOKEN_SPLIT_RE = re.compile(r"[\s,:;\n]+")
PHONE_INPUT_RE = re.compile(r"^[+\d][\d\s()-]{6,}$")
_VIN_CONTROL_RE = re.compile(r"[\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_VIN_DIGIT_TRANSLATE = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

def _sanitize_for_vin(text: str) -> str:
    cleaned = _VIN_CONTROL_RE.sub("", text or "")
    cleaned = cleaned.translate(_VIN_DIGIT_TRANSLATE)
    cleaned = re.sub(r"[\s:-]", "", cleaned)
    return cleaned.upper()


@dataclass(slots=True)
class UserContext:
    user_id: str
    phone: Optional[str] = None
    language: str = "ar"
    state: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_anonymous(self) -> bool:
        return not self.phone


@dataclass(slots=True)
class IncomingMessage:
    """Normalized inbound message regardless of platform."""

    platform: str
    user_id: str
    text: Optional[str] = None
    media_url: Optional[str] = None
    caption: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BridgeResponse:
    """Structured response builders returned by bridge handlers."""

    messages: List[str] = field(default_factory=list)
    media: List[Dict[str, Any]] = field(default_factory=list)
    documents: List[Dict[str, Any]] = field(default_factory=list)
    actions: Dict[str, Any] = field(default_factory=dict)

    def has_payload(self) -> bool:
        """Return True if any payload is available."""

        return bool(self.messages or self.media or self.documents or self.actions)

    def __bool__(self) -> bool:  # pragma: no cover - trivial passthrough
        return self.has_payload()

    def __iter__(self):  # pragma: no cover - needed for legacy callers
        return iter(self.messages)

    def __len__(self) -> int:  # pragma: no cover - needed for legacy callers
        return len(self.messages)


class LimitCheckResult(NamedTuple):
    """Return type for limit checks (allowed?, message, reason)."""

    allowed: bool
    message: Optional[str]
    reason: Optional[str]


MENU_REGISTRY: List[Dict[str, Any]] = [
    {
        "id": "activation",
        "label_key": "menu.activation.label",
        "description_key": "menu.activation.description",
        "row": 10,
        "col": 1,
        "delegate": "request_activation",
    },
    {
        "id": "profile",
        "label_key": "menu.profile.label",
        "description_key": "menu.profile.description",
        "row": 20,
        "col": 1,
        "delegate": "whoami",
    },
    {
        "id": "language",
        "label_key": "menu.language.label",
        "description_key": "menu.language.description",
        "row": 30,
        "col": 1,
        "delegate": "lang_panel",
    },
    {
        "id": "help",
        "label_key": "menu.help.label",
        "description_key": "menu.help.description",
        "row": 40,
        "col": 1,
        "delegate": "help",
    },
    # Admin / super entries remain available but appear after user items
    {
        "id": "users",
        "label_key": "menu.users.label",
        "description_key": "menu.users.description",
        "row": 100,
        "col": 1,
        "requires_admin": True,
        "delegate": "users",
    },
    {
        "id": "stats",
        "label_key": "menu.stats.label",
        "description_key": "menu.stats.description",
        "row": 110,
        "col": 1,
        "requires_admin": True,
        "delegate": "stats",
    },
    {
        "id": "pending",
        "label_key": "menu.pending.label",
        "description_key": "menu.pending.description",
        "row": 120,
        "col": 1,
        "requires_admin": True,
        "delegate": "pending",
    },
    {
        "id": "settings",
        "label_key": "menu.settings.label",
        "description_key": "menu.settings.description",
        "row": 130,
        "col": 1,
        "requires_super": True,
        "delegate": "settings",
    },
    {
        "id": "notifications",
        "label_key": "menu.notifications.label",
        "description_key": "menu.notifications.description",
        "row": 140,
        "col": 1,
        "requires_super": True,
        "delegate": "notifications",
    },
]

KURDISH_LANGS = {"ku", "ckb"}

LANGUAGE_CHOICES = (
    ("en", "English"),
    ("ar", "العربية"),
    ("ku", "کوردی بادینی"),
    ("ckb", "کوردی سۆرانی"),
)

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "activation.invalid": {
        "ar": "⚠️ رقم غير صالح. أعد الإرسال بصيغة <code>+رمز_الدولة</code> ثم الرقم (مثال: <code>+962795378832</code>){cc_hint}",
        "en": "⚠️ Invalid number. Please resend it as <code>+country_code</code> followed by the number (example: <code>+962795378832</code>).{cc_hint}",
        "ku": "⚠️ ژمارە دروست نییە. تکایە بە شێوەی <code>+کۆدی وڵات</code> دواتر ژمارە بنێرە (نمونە: <code>+962795378832</code>).{cc_hint}",
        "ckb": "⚠️ ژمارە نادروستە. تکایە بە شێوەی <code>+کۆدی وڵات</code> دوای ژمارە بنێرە (نمونە: <code>+962795378832</code>).{cc_hint}",
    },
    "activation.invalid_cc_hint": {
        "ar": "\nيمكنك أيضًا إرسال الرقم بدون + بعد إزالة الصفر الأول بعد اختيار <b>{cc}</b>.",
        "en": "\nYou can also send the number without + after removing the leading zero once <b>{cc}</b> is selected.",
        "ku": "\nدەتوانیت ژمارەکە بۆنێریت بەبێ + دوای سڕینەوەی صفر لە پێشدا کاتێک <b>{cc}</b> هەڵتبژێردرا.",
        "ckb": "\nدەتوانیت ژمارەکە بنێریت بەبێ + دوای سڕینەوەی صفرەکە لە پێشدا کاتێک <b>{cc}</b> هەڵتبژێردرا.",
    },
    "activation.already_active": {
        "ar": "✅ حسابك مفعل حالياً، لا حاجة لإضافتك لقائمة الانتظار.",
        "en": "✅ Your account is already active, no need to join the waitlist.",
        "ku": "✅ ئەکاونتەکەت چالاکە؛ پێویست بە داواکاری چالاککردن نییە.",
        "ckb": "✅ هەژمارەکەت چالاکە؛ پێویست بە داواکاری چالاککردن نییە.",
    },
    "activation.request_pending": {
        "ar": "📨 <b>طلبك قيد المراجعة</b>\n\nلقد استلمنا طلب تفعيلك وننتظر موافقة الإدارة.\nسيتم إشعارك فور اكتمال المراجعة.",
        "en": "📨 <b>Your request is under review</b>\n\nWe already received your activation request and are waiting for approval.\nYou'll be notified as soon as it is processed.",
        "ku": "📨 <b>داواکاریەکەت لە چاوپێکەوتندایە</b>\n\nداواکاریی چالاککردنت گەیەندراوە و چاوەڕێی پەسەندکردنە.\nپەیام دەگەیتەوە کاتێک تەواوبێت.",
        "ckb": "📨 <b>داواکارییەکەت لە چاوپێکەوتندایە</b>\n\nداواکاریی چالاککردنتمان وەرگرتووە و چاوەڕێی پەسەندکردنی یە.\nهەنگاوەکانت ئاگادار دەکەین کاتێک تەواوبێت.",
    },
    "activation.request_received": {
        "ar": "✅ تم استلام رقم هاتفك وإرسال طلب التفعيل.\nسنقوم بالمراجعة وإعلامك قريبًا.",
        "en": "✅ We received your phone number and submitted the activation request.\nWe'll review it and update you shortly.",
        "ku": "✅ ژمارەی مۆبایلەکەت وەرگیرا و داواکاریی چالاککردن نێردرا.\nزوو پەیامدەدرێت بە نوێکاریەکان.",
        "ckb": "✅ ژمارەی مۆبایلت وەرگرت و داواکاریی چالاککردن نێردرا.\nبە زووترین کات ئاگادارت دەکەین بە دۆخی داواکاریەکە.",
    },
    "activation.prompt.cc": {
        "ar": (
            "🛂 <b>طلب تفعيل</b>\n\n"
            "اختر <b>مفتاح الدولة</b> أولاً، ثم أرسل رقم هاتفك.\n\n"
            "• الصيغة المفضلة: <code>+962795xxxxxx</code>\n"
            "• أو اختر المفتاح ثم أرسل الرقم بدون + وبدون الصفر الأول\n"
            "• مثال بعد اختيار +962: <code>795378832</code>\n"
        ),
        "en": (
            "🛂 <b>Activation request</b>\n\n"
            "Pick a <b>country code</b> first, then send your phone number.\n\n"
            "• Preferred format: <code>+962795xxxxxx</code>\n"
            "• Or pick the code, then send without + and without the leading zero\n"
            "• Example after +962: <code>795378832</code>\n"
        ),
        "ku": (
            "🛂 <b>داوای چالاککردن</b>\n\n"
            "سەرەتا <b>کۆدی وڵات</b> هەڵبژێرە، پاشان ژمارەکەت بنێرە.\n\n"
            "• فۆرماتی باش: <code>+962795xxxxxx</code>\n"
            "• یان کۆد هەڵبژێرە و ژمارە بنێرە بەبێ + و بەبێ صفر لە دەستپێکدا\n"
            "• نمونە دوای +962: <code>795378832</code>\n"
        ),
        "ckb": (
            "🛂 <b>داوای چالاککردن</b>\n\n"
            "سەرەتا <b>کۆدی وڵات</b> هەڵبژێرە، دواتر ژمارەکەت بنێرە.\n\n"
            "• فۆرماتێکی باش: <code>+962795xxxxxx</code>\n"
            "• یان کۆدەکە هەڵبژێرە و ژمارە بنێرە بەبێ + و بەبێ صفر لە سەرەتا\n"
            "• نمونە دوای هەڵبژاردنی +962: <code>795378832</code>\n"
        ),
    },
    "activation.cc.enter_full": {
        "ar": "🌍 أرسل رقمك كاملاً بصيغة <code>+رمز_الدولة</code> ثم الرقم. مثال: <code>+962795378832</code>",
        "en": "🌍 Send your full number as <code>+country_code</code> followed by the digits. Example: <code>+962795378832</code>",
        "ku": "🌍 ژمارەکەت بە تەواوی بنێرە بە شێوەی <code>+کۆدی وڵات</code> دواتر ژمارە. نمونە: <code>+962795378832</code>",
        "ckb": "🌍 ژمارەکەت بە تەواوی بنێرە بە شێوەی <code>+کۆدی وڵات</code> دواتر ژمارە. نمونە: <code>+962795378832</code>",
    },
    "activation.cc.other": {
        "ar": "🌍 رمز آخر",
        "en": "🌍 Other code",
        "ku": "🌍 کۆدی تر",
        "ckb": "🌍 کۆدی تر",
    },
    "activation.cc.selected": {
        "ar": "📞 المفتاح المختار: <b>{cc}</b>\nأرسل الآن رقمك بدون + وبدون الصفر الأول. مثال: <code>795378832</code>",
        "en": "📞 Selected code: <b>{cc}</b>\nSend your number now without + and without the leading zero. Example: <code>795378832</code>",
        "ku": "📞 کۆدی هەڵبژێردراو: <b>{cc}</b>\nئێستا ژمارەکەت بنێرە بەبێ + و بەبێ صفر لە دەستپێکدا. نمونە: <code>795378832</code>",
        "ckb": "📞 کۆدی هەڵبژێردراو: <b>{cc}</b>\nئێستا ژمارە بنێرە بەبێ + و بەبێ صفر لە سەرەتا. نمونە: <code>795378832</code>",
    },
    "activation.error.retry": {
        "ar": "⚠️ حدث خطأ أثناء معالجة الطلب. حاول مرة أخرى باستخدام زر 🛂 طلب تفعيل.",
        "en": "⚠️ Something went wrong processing the request. Try again from the 🛂 Activation button.",
        "ku": "⚠️ هەڵەیەک ڕوویدا لە کاتی پڕۆسەکردن. دووبارە هەوڵبدە لە دوگمەی 🛂 چالاککردنەوە.",
        "ckb": "⚠️ هەڵەیەک ڕوویدا لە کاتی پڕۆسەکردن. دوبارە هەوڵبدە لە دوگمەی 🛂 چالاککردنەوە.",
    },
    "common.cancelled": {
        "ar": "✅ تم إلغاء العملية.",
        "en": "✅ Operation cancelled.",
        "ku": "✅ کردار هەڵوەشێنرایەوە.",
        "ckb": "✅ کردار هەڵوەشێنرایەوە.",
    },
    "common.unauthorized": {
        "ar": "⛔ غير مصرح.",
        "en": "⛔ Not authorized.",
        "ku": "⛔ ڕێگەپێدراو نییە.",
        "ckb": "⛔ ڕێگەپێدراو نییە.",
    },
    "common.invalid_data": {
        "ar": "❌ بيانات غير صحيحة.",
        "en": "❌ Invalid data.",
        "ku": "❌ داتا هەڵەیە.",
        "ckb": "❌ داتا هەڵەیە.",
    },
    "common.invalid_vin": {
        "ar": "⚠️ الرجاء التأكد من رقم الشاصي الصحيح (VIN من 17 خانة) ثم أعد المحاولة.",
        "en": "⚠️ Please provide a valid VIN (17 characters) and try again.",
        "ku": "⚠️ تکایە VIN دروست (17 پیت) بنێرە و دووبارە هەوڵبدە.",
        "ckb": "⚠️ تکایە VIN ـێکی دروست (١٧ پیت) بنێرە و جارێکی تر هەوڵبدە.",
    },
    "common.invalid_button": {
        "ar": "⚠️ زر غير صالح.",
        "en": "⚠️ Invalid button.",
        "ku": "⚠️ دوگمە نادروستە.",
        "ckb": "⚠️ دوگمە نادروستە.",
    },
    "common.unknown_option": {
        "ar": "⚠️ خيار غير معروف.",
        "en": "⚠️ Unknown option.",
        "ku": "⚠️ هەڵبژاردەی نەناسراو.",
        "ckb": "⚠️ هەڵبژاردەی نەناسراو.",
    },
    "admin.user.unknown": {
        "ar": "⚠️ مستخدم غير معروف.",
        "en": "⚠️ Unknown user.",
        "ku": "⚠️ بەکارهێنەر نەناسراوە.",
        "ckb": "⚠️ بەکارهێنەر نەناسراوە.",
    },
    "admin.user.already_stopped": {
        "ar": "ℹ️ المستخدم متوقف بالفعل.",
        "en": "ℹ️ User is already stopped.",
        "ku": "ℹ️ بەکارهێنەر پێشتر وەستێنراوە.",
        "ckb": "ℹ️ بەکارهێنەر پێشتر وەستێنراوە.",
    },
    "admin.user.suspend.notify": {
        "ar": "⛔ تم إيقاف حسابك من قبل الإدارة.",
        "en": "⛔ Your account has been suspended by admin.",
        "ku": "⛔ هەژمارەکەت لەلایەن بەڕێوەبەرەوە وەستێنرا.",
        "ckb": "⛔ هەژمارەکەت لەلایەن بەڕێوەبەرەوە وەستێنرا.",
    },
    "admin.user.suspend.toast": {
        "ar": "✅ تم توقيف المستخدم.",
        "en": "✅ User has been suspended.",
        "ku": "✅ بەکارهێنەر وەستێنرا.",
        "ckb": "✅ بەکارهێنەر وەستێنرا.",
    },
    "admin.user.suspend.log": {
        "ar": "⛔ (Admin:{admin}) أوقف {user}.",
        "en": "⛔ (Admin:{admin}) suspended {user}.",
        "ku": "⛔ (ئادمین:{admin}) {user} وەستێناند.",
        "ckb": "⛔ (ئادمین:{admin}) {user} وەستێناند.",
    },
    "admin.user.reactivate.prompt": {
        "ar": "⛔ <b>{name}</b> متوقف.\n\nاختر طريقة لإعادة التفعيل:",
        "en": "⛔ <b>{name}</b> is stopped.\n\nChoose how to reactivate:",
        "ku": "⛔ <b>{name}</b> وەستێنراوە.\n\nڕێگای چالاککردن هەڵبژێرە:",
        "ckb": "⛔ <b>{name}</b> وەستێنراوە.\n\nڕێگای چالاککردن هەڵبژێرە:",
    },
    "admin.user.reactivate.option.trial": {
        "ar": "🧪 تجربة افتراضية",
        "en": "🧪 Trial preset",
        "ku": "🧪 تاقیکردنەوەی بنەڕەتی",
        "ckb": "🧪 تاقیکردنەوەی بنەڕەتی",
    },
    "admin.user.reactivate.option.monthly": {
        "ar": "🟢 اشتراك شهري",
        "en": "🟢 Monthly plan",
        "ku": "🟢 پلانی مانگانە",
        "ckb": "🟢 پلانی مانگانە",
    },
    "admin.user.reactivate.option.custom": {
        "ar": "🧾 تفعيل مخصّص",
        "en": "🧾 Custom activation",
        "ku": "🧾 چالاککردنی تایبەتی",
        "ckb": "🧾 چالاککردنی تایبەتی",
    },
    "admin.user.reactivate.option.open_card": {
        "ar": "🔎 فتح البطاقة",
        "en": "🔎 Open user card",
        "ku": "🔎 کارتی بەکارهێنەر بکەرەوە",
        "ckb": "🔎 کارتی بەکارهێنەر بکەرەوە",
    },
    "admin.user.reactivate.sent": {
        "ar": "📨 تم إرسال خيارات التفعيل.",
        "en": "📨 Activation options sent.",
        "ku": "📨 هەڵبژاردەکانی چالاککردن نێردران.",
        "ckb": "📨 هەڵبژاردەکانی چالاککردن نێردران.",
    },
    "admin.limit.prompt": {
        "ar": "📈 <b>{name}</b> وصل الحد المسموح.\n\nاختر الحد الذي تريد تعديله:",
        "en": "📈 <b>{name}</b> reached the limit.\n\nChoose which limit to adjust:",
        "ku": "📈 <b>{name}</b> گەیشتووە سنوورەکە.\n\nکام سنوور هەڵبژێریت بگۆڕیت؟",
        "ckb": "📈 <b>{name}</b> گەیشتووە سنوورەکە.\n\nکام سنوور هەڵبژێریت بگۆڕیت؟",
    },
    "admin.limit.option.daily": {
        "ar": "📅 رفع الحد اليومي",
        "en": "📅 Increase daily limit",
        "ku": "📅 بەرزکردنەوەی سنووری ڕۆژانە",
        "ckb": "📅 بەرزکردنەوەی سنووری ڕۆژانە",
    },
    "admin.limit.option.monthly": {
        "ar": "📆 رفع الحد الشهري",
        "en": "📆 Increase monthly limit",
        "ku": "📆 بەرزکردنەوەی سنووری مانگانە",
        "ckb": "📆 بەرزکردنەوەی سنووری مانگانە",
    },
    "admin.limit.sent": {
        "ar": "📨 تم إرسال خيارات الحد.",
        "en": "📨 Limit options sent.",
        "ku": "📨 هەڵبژاردەکانی سنوور نێردران.",
        "ckb": "📨 هەڵبژاردەکانی سنوور نێردران.",
    },
    "admin.limit.prompt.daily": {
        "ar": "📅 أرسل الحد اليومي الجديد (رقم):",
        "en": "📅 Send the new daily limit (number):",
        "ku": "📅 سنووری نوێی ڕۆژانە بنێرە (ژمارە):",
        "ckb": "📅 سنووری نوێی ڕۆژانە بنێرە (ژمارە):",
    },
    "admin.limit.prompt.monthly": {
        "ar": "📆 أرسل الحد الشهري الجديد (رقم):",
        "en": "📆 Send the new monthly limit (number):",
        "ku": "📆 سنووری نوێی مانگانە بنێرە (ژمارە):",
        "ckb": "📆 سنووری نوێی مانگانە بنێرە (ژمارە):",
    },
    "admin.users.back": {
        "ar": "↩️ رجوع",
        "en": "↩️ Back",
        "ku": "↩️ گەڕانەوە",
        "ckb": "↩️ گەڕانەوە",
    },
    "admin.users.prev": {
        "ar": "« السابق",
        "en": "« Prev",
        "ku": "« پێشوو",
        "ckb": "« پێشوو",
    },
    "admin.users.next": {
        "ar": "التالي »",
        "en": "Next »",
        "ku": "دواتر »",
        "ckb": "دواتر »",
    },
    "admin.users.main": {
        "ar": "🏠 القائمة الرئيسية",
        "en": "🏠 Main menu",
        "ku": "🏠 لیستەی سەرەکی",
        "ckb": "🏠 لیستەی سەرەکی",
    },
    "admin.users.none": {
        "ar": "لا يوجد مستخدمون حالياً",
        "en": "No users right now.",
        "ku": "هیچ بەکارهێنەرێک نییە لە ئێستادا.",
        "ckb": "هیچ بەکارهێنەرێک نییە لە ئێستادا.",
    },
    "admin.users.page.empty": {
        "ar": "لا يوجد مستخدمون في هذه الصفحة.",
        "en": "No users on this page.",
        "ku": "لەمانەوە بەکارهێنەر نییە لەم لاپەرەدا.",
        "ckb": "لەمانەوە بەکارهێنەر نییە لەم پەڕەدا.",
    },
    "admin.users.phone.missing": {
        "ar": "📞 لا يوجد",
        "en": "📞 None",
        "ku": "📞 نییە",
        "ckb": "📞 نییە",
    },
    "admin.users.expiry.unset": {
        "ar": "غير محدد",
        "en": "Not set",
        "ku": "دیار نەکراوە",
        "ckb": "دیار نەکراوە",
    },
    "admin.users.delete": {
        "ar": "🗑️ حذف",
        "en": "🗑️ Delete",
        "ku": "🗑️ سڕینەوە",
        "ckb": "🗑️ سڕینەوە",
    },
    "admin.activation.hint": {
        "ar": "أرسل طلب التفعيل من زر 🛂 طلب تفعيل في القائمة.",
        "en": "Send the activation request from the 🛂 Activation Request button in the menu.",
        "ku": "داوای چالاککردن لە دوگمەی 🛂 داوای چالاککردن لە لیستە بکە.",
        "ckb": "داوای چالاککردن لە دوگمەی 🛂 داوای چالاککردن لە لیستە بکە.",
    },
    "admin.users.list.intro": {
        "ar": "👥 <b>قائمة المستخدمين</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<i>انقر على اسم المستخدم لفتح بطاقته</i>\n\n<b>💡 في البطاقة ستجد:</b>\n• ✉️ إشعار سريع\n• 💳 ضبط الرصيد\n• 📝 ملاحظة\n• وغيرها من الخيارات",
        "en": "👥 <b>Users list</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<i>Tap the username to open the card</i>\n\n<b>💡 In the card you will find:</b>\n• ✉️ Quick notify\n• 💳 Balance adjust\n• 📝 Note\n• Other actions",
        "ku": "👥 <b>لیستی بەکارهێنەرەکان</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<i>ناوی بەکارهێنەر بکەرەوە بۆ کردنی کارد</i>\n\n<b>💡 لە کاردەدا دەتوانیت:</b>\n• ✉️ ئاگادارکردنەوەی خێرا\n• 💳 ڕێکخستنی باڵانس\n• 📝 تێبینی\n• هەڵبژاردەی تر",
        "ckb": "👥 <b>لیستی بەکارهێنەران</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<i>ناوی بەکارهێنەر دابگرە بۆ کردنی کارد</i>\n\n<b>💡 لە کاردەدا دەتوانیت:</b>\n• ✉️ ئاگادارکردنی خێرا\n• 💳 ڕێکخستنی باڵانس\n• 📝 تێبینی\n• هەڵبژاردەی دیکە",
    },
    "admin.users.load_error": {
        "ar": "❌ حدث خطأ في تحميل قائمة المستخدمين.",
        "en": "❌ Failed to load user list.",
        "ku": "❌ هەڵە ڕووی دا لە بارکردنی لیستی بەکارهێنەران.",
        "ckb": "❌ هەڵە ڕووی دا لە بارکردنی لیستی بەکارهێنەران.",
    },
    "admin.stats.open_error": {
        "ar": "❌ تعذر فتح الإحصائيات.",
        "en": "❌ Could not open stats.",
        "ku": "❌ ناتوانرێت ئامار بکەرەوە.",
        "ckb": "❌ ناتوانرێت ئامار بکەرەوە.",
    },
    "admin.settings.super_only": {
        "ar": "❌ هذه الإعدادات متاحة فقط للسوبر أدمن.",
        "en": "❌ Settings are restricted to super admins only.",
        "ku": "❌ ئەم ڕێکخستنە تایبەتە بە سووپەر ئەدمینەکانە.",
        "ckb": "❌ ئەم ڕێکخستنە تەنها بۆ سووپەر ئەدمینەکانە.",
    },
    "admin.settings.error": {
        "ar": "❌ حدث خطأ: {error}\n\nاستخدم /debug للتحقق من صلاحياتك.",
        "en": "❌ Error: {error}\n\nUse /debug to check your permissions.",
        "ku": "❌ هەڵە: {error}\n\n/\u2026 بەکاربهێنە بۆ دڵنیابوون لە دەسەڵاتەکانت.",
        "ckb": "❌ هەڵە: {error}\n\n/\u2026 بەکاربهێنە بۆ دڵنیابوون لە دەسەڵاتەکانت.",
    },
    "photos.heading.hidden": {
        "ar": "📷 صور السيارة المخفية",
        "en": "📷 Hidden car photos",
        "ku": "📷 وێنەکانی شاراوەی ئۆتۆمبێل",
        "ckb": "📷 وێنەکانی شاراوەی ئۆتۆمبێل",
    },
    "photos.heading.auction": {
        "ar": "🚗 صور المزاد الحالي",
        "en": "🚗 Current auction photos",
        "ku": "🚗 وێنەکانی مزاودەی ئێستا",
        "ckb": "🚗 وێنەکانی مزاودەی ئێستا",
    },
    "photos.heading.accident": {
        "ar": "💥 صور حادث سابق",
        "en": "💥 Previous accident photos",
        "ku": "💥 وێنەکانی پەڕینی پێشوو",
        "ckb": "💥 وێنەکانی پەڕینی پێشوو",
    },
    "photos.not_enabled": {
        "ar": "⛔ {label} غير مفعلة لحسابك.",
        "en": "⛔ {label} is not enabled for your account.",
        "ku": "⛔ {label} بۆ هەژمارەکەت چالاک نییە.",
        "ckb": "⛔ {label} بۆ هەژمارەکەت چالاک نییە.",
    },
    "common.status.yes": {
        "ar": "✅ نعم",
        "en": "✅ Yes",
        "ku": "✅ بەڵێ",
        "ckb": "✅ بەڵێ",
    },
    "common.status.no": {
        "ar": "❌ لا",
        "en": "❌ No",
        "ku": "❌ نەخێر",
        "ckb": "❌ نەخێر",
    },
    "common.set": {
        "ar": "محدد",
        "en": "set",
        "ku": "دیاریکراو",
        "ckb": "دیاریکراو",
    },
    "common.unset": {
        "ar": "غير محدد",
        "en": "not set",
        "ku": "دیار نەکراوە",
        "ckb": "دیار نەکراوە",
    },
    "common.unavailable": {
        "ar": "غير متوفر",
        "en": "Unavailable",
        "ku": "بەردەست نییە",
        "ckb": "بەردەست نییە",
    },
    "admin.debug.title": {
        "ar": "🔍 <b>معلومات الصلاحيات والبيئة</b>",
        "en": "🔍 <b>Permissions and environment</b>",
        "ku": "🔍 <b>زانیاری دەسەڵات و ژینگە</b>",
        "ckb": "🔍 <b>زانیاری دەسەڵات و ژینگە</b>",
    },
    "admin.debug.user_id": {
        "ar": "معرفك: <code>{tg_id}</code>",
        "en": "Your ID: <code>{tg_id}</code>",
        "ku": "ناسنامە: <code>{tg_id}</code>",
        "ckb": "ناسنامەت: <code>{tg_id}</code>",
    },
    "admin.debug.username": {
        "ar": "اسم المستخدم: {username}",
        "en": "Username: {username}",
        "ku": "ناوی بەکارهێنەر: {username}",
        "ckb": "ناوی بەکارهێنەر: {username}",
    },
    "admin.debug.roles.header": {
        "ar": "<b>الصلاحيات:</b>",
        "en": "<b>Roles:</b>",
        "ku": "<b>دەسەڵاتەکان:</b>",
        "ckb": "<b>دەسەڵاتەکان:</b>",
    },
    "admin.debug.roles.super": {
        "ar": "• سوبر أدمن: {value}",
        "en": "• Super admin: {value}",
        "ku": "• سووپەر ئەدمین: {value}",
        "ckb": "• سووپەر ئەدمین: {value}",
    },
    "admin.debug.roles.admin": {
        "ar": "• أدمن: {value}",
        "en": "• Admin: {value}",
        "ku": "• ئەدمین: {value}",
        "ckb": "• ئەدمین: {value}",
    },
    "admin.debug.roles.ultimate": {
        "ar": "• سوبر أدمن مطلق (.env): {value}",
        "en": "• Ultimate super (.env): {value}",
        "ku": "• سووپەر ئەدمینی سەرەکی (.env): {value}",
        "ckb": "• سووپەر ئەدمینی سەرەکی (.env): {value}",
    },
    "admin.debug.env.header": {
        "ar": "📋 <b>معلومات متغيرات البيئة:</b>",
        "en": "📋 <b>Environment variables:</b>",
        "ku": "📋 <b>گۆڕاوەکانی ژینگە:</b>",
        "ckb": "📋 <b>گۆڕاوەکانی ژینگە:</b>",
    },
    "admin.debug.env.telegram_supers": {
        "ar": "• TELEGRAM_SUPER_ADMINS: <code>{env_supers}</code>",
        "en": "• TELEGRAM_SUPER_ADMINS: <code>{env_supers}</code>",
        "ku": "• TELEGRAM_SUPER_ADMINS: <code>{env_supers}</code>",
        "ckb": "• TELEGRAM_SUPER_ADMINS: <code>{env_supers}</code>",
    },
    "admin.debug.env.dotenv_loaded": {
        "ar": "• تم تحميل dotenv: {value}",
        "en": "• Dotenv loaded: {value}",
        "ku": "• dotenv بارکرا: {value}",
        "ckb": "• dotenv بارکرا: {value}",
    },
    "admin.debug.env.bot_token": {
        "ar": "• BOT_TOKEN: <code>{value}</code>",
        "en": "• BOT_TOKEN: <code>{value}</code>",
        "ku": "• BOT_TOKEN: <code>{value}</code>",
        "ckb": "• BOT_TOKEN: <code>{value}</code>",
    },
    "admin.debug.env.db_path": {
        "ar": "• DB_PATH: <code>{value}</code>",
        "en": "• DB_PATH: <code>{value}</code>",
        "ku": "• DB_PATH: <code>{value}</code>",
        "ckb": "• DB_PATH: <code>{value}</code>",
    },
    "admin.debug.env.supers_env": {
        "ar": "<b>السوبر أدمن من .env:</b> {env_admins}",
        "en": "<b>Super admins from .env:</b> {env_admins}",
        "ku": "<b>سووپەر ئەدمینەکانی .env:</b> {env_admins}",
        "ckb": "<b>سووپەر ئەدمینەکانی .env:</b> {env_admins}",
    },
    "admin.debug.env.supers_db": {
        "ar": "<b>السوبر أدمن من db.json:</b> {db_admins}",
        "en": "<b>Super admins from db.json:</b> {db_admins}",
        "ku": "<b>سووپەر ئەدمینەکانی db.json:</b> {db_admins}",
        "ckb": "<b>سووپەر ئەدمینەکانی db.json:</b> {db_admins}",
    },
    "admin.debug.tip": {
        "ar": "<i>💡 نصيحة: إذا قمت بتعديل ملف .env، استخدم /debug لإعادة تحميل المتغيرات</i>",
        "en": "<i>💡 Tip: after editing .env, run /debug to reload variables.</i>",
        "ku": "<i>💡 پێشنیار: دوای دەستکاری .env، /debug بەکاربهێنە بۆ دووبارە بارکردن.</i>",
        "ckb": "<i>💡 پێشنیار: دوای دەستکاری .env، /debug بەکاربهێنە بۆ دووبارە بارکردن.</i>",
    },
    "profile.add_phone": {
        "ar": "📞 إضافة هاتف",
        "en": "📞 Add phone",
        "ku": "📞 زۆرکردنی ژمارە",
        "ckb": "📞 زیادکردنی ژمارە",
    },
    "common.main_menu": {
        "ar": "🏠 القائمة الرئيسية",
        "en": "🏠 Main Menu",
        "ku": "🏠 لیستی سەرەکی",
        "ckb": "🏠 لیستی سەرەکی",
    },
    "limits.updated.daily.user": {
        "ar": "📈 <b>تم تحديث حدك اليومي</b>\n\nالحد الجديد: <b>{value}</b> تقرير كل يوم.\n👤 بواسطة الإدارة: <code>{admin}</code>",
        "en": "📈 <b>Your daily limit was updated</b>\n\nNew limit: <b>{value}</b> reports per day.\n👤 By admin: <code>{admin}</code>",
        "ku": "📈 <b>سنووری ڕۆژانت نوێ کرایەوە</b>\n\nسنووری نوێ: <b>{value}</b> ڕاپۆرت لە ڕۆژێکدا.\n👤 لەلایەن بەڕێوەبەرەوە: <code>{admin}</code>",
        "ckb": "📈 <b>سنووری ڕۆژانت نوێ کرایەوە</b>\n\nسنووری نوێ: <b>{value}</b> ڕاپۆرت لە ڕۆژێکدا.\n👤 لەلایەن بەڕێوەبەرەوە: <code>{admin}</code>",
    },
    "limits.updated.monthly.user": {
        "ar": "📊 <b>تم تحديث حدك الشهري</b>\n\nالحد الجديد: <b>{value}</b> تقرير في الشهر.\n👤 بواسطة الإدارة: <code>{admin}</code>",
        "en": "📊 <b>Your monthly limit was updated</b>\n\nNew limit: <b>{value}</b> reports per month.\n👤 By admin: <code>{admin}</code>",
        "ku": "📊 <b>سنووری مانگانەت نوێ کرایەوە</b>\n\nسنووری نوێ: <b>{value}</b> ڕاپۆرت لە مانگێکدا.\n👤 لەلایەن بەڕێوەبەرەوە: <code>{admin}</code>",
        "ckb": "📊 <b>سنووری مانگانەت نوێ کرایەوە</b>\n\nسنووری نوێ: <b>{value}</b> ڕاپۆرت لە مانگێکدا.\n👤 لەلایەن بەڕێوەبەرەوە: <code>{admin}</code>",
    },
    "pending.denied.user": {
        "ar": "⛔ تم رفض طلب التفعيل الخاص بك.",
        "en": "⛔ Your activation request was denied.",
        "ku": "⛔ داوای چالاککردنت ڕەتکرایەوە.",
    },
    "action.cancel": {
        "ar": "↩️ إلغاء",
        "en": "↩️ Cancel",
        "ku": "↩️ هەڵوەشاندن",
    },
    "action.back": {
        "ar": "↩️ رجوع",
        "en": "↩️ Back",
        "ku": "↩️ گەڕانەوە",
    },
    "button.activation_now": {
        "ar": "🛂 طلب تفعيل الآن",
        "en": "🛂 Request activation",
        "ku": "🛂 داوای چالاککردنەوە",
    },
    "button.back_menu": {
        "ar": "↩️ رجوع إلى القائمة",
        "en": "↩️ Back to menu",
        "ku": "↩️ گەڕانەوە بۆ لیستە",
    },
    "button.vin_info": {
        "ar": "ℹ️ ما هو VIN؟",
        "en": "ℹ️ What is VIN?",
        "ku": "ℹ️ VIN چییە؟",
    },
    "button.vin_sample": {
        "ar": "🈯️ مثال",
        "en": "🈯️ Sample",
        "ku": "🈯️ نموونە",
    },
    "button.new_report": {
        "ar": "📄 تقرير جديد",
        "en": "📄 New report",
        "ku": "📄 ڕاپۆرتی نوێ",
    },
    "help.contact": {
        "ar": (
            "🆘 <b>المساعدة والتواصل</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📞 طرق التواصل:</b>\n\n"
            "🌐 <b>الموقع الإلكتروني:</b>\n<a href='https://www.dejavuplus.com'>www.dejavuplus.com</a>\n\n"
            "✉️ <b>البريد الإلكتروني:</b>\n<a href='mailto:info@dejavuplus.com'>info@dejavuplus.com</a>\n\n"
            "🟢 <b>واتساب:</b>\n<a href='https://wa.me/962795378832'>+962 7 9537 8832</a>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>اختر طريقة التواصل المناسبة لك:</i>"
        ),
        "en": (
            "🆘 <b>Help & Contact</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📞 Contact options:</b>\n\n"
            "🌐 <b>Website:</b>\n<a href='https://www.dejavuplus.com'>www.dejavuplus.com</a>\n\n"
            "✉️ <b>Email:</b>\n<a href='mailto:info@dejavuplus.com'>info@dejavuplus.com</a>\n\n"
            "🟢 <b>WhatsApp:</b>\n<a href='https://wa.me/962795378832'>+962 7 9537 8832</a>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Pick your preferred channel.</i>"
        ),
        "ku": (
            "🆘 <b>یارمەتی و پەیوەندی</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📞 ڕێگاکانی پەیوەندی:</b>\n\n"
            "🌐 <b>وێبسايت:</b>\n<a href='https://www.dejavuplus.com'>www.dejavuplus.com</a>\n\n"
            "✉️ <b>ئیمەیل:</b>\n<a href='mailto:info@dejavuplus.com'>info@dejavuplus.com</a>\n\n"
            "🟢 <b>WhatsApp:</b>\n<a href='https://wa.me/962795378832'>+962 7 9537 8832</a>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>ڕێگای دڵخوازت هەڵبژێرە.</i>"
        ),
    },
    # Usercard / admin panels
    "usercard.header": {
        "ar": "🪪 <b>بطاقة المستخدم</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "en": "🪪 <b>User card</b>\n━━━━━━━━━━━━━━━━━━━━\n",
        "ku": "🪪 <b>کارتێکی بەکارهێنەر</b>\n━━━━━━━━━━━━━━━━━━━━\n",
    },
    "usercard.name_line": {
        "ar": "• الاسم: <b>{name}</b>\n",
        "en": "• Name: <b>{name}</b>\n",
        "ku": "• ناو: <b>{name}</b>\n",
    },
    "usercard.tg_line": {
        "ar": "• TG: <code>{tg}</code> @{username}\n",
        "en": "• TG: <code>{tg}</code> @{username}\n",
        "ku": "• TG: <code>{tg}</code> @{username}\n",
    },
    "usercard.contact.username": {
        "ar": "• 📬 المراسلة: <a href='https://t.me/{username}'>@{username}</a>\n",
        "en": "• 📬 Contact: <a href='https://t.me/{username}'>@{username}</a>\n",
        "ku": "• 📬 پەیوەندی: <a href='https://t.me/{username}'>@{username}</a>\n",
    },
    "usercard.contact.id": {
        "ar": "• 📬 المراسلة عبر ID: <code>{tg_id}</code>\n",
        "en": "• 📬 Contact via ID: <code>{tg_id}</code>\n",
        "ku": "• 📬 پەیوەندی بە ID: <code>{tg_id}</code>\n",
    },
    "usercard.phone": {
        "ar": "• 📞 الهاتف: <a href='https://wa.me/{wa}'>{phone}</a>\n",
        "en": "• 📞 Phone: <a href='https://wa.me/{wa}'>{phone}</a>\n",
        "ku": "• 📞 تەلەفۆن: <a href='https://wa.me/{wa}'>{phone}</a>\n",
    },
    "usercard.plan_services": {
        "ar": "• الخطة/الخدمات: <b>{plan}</b> — {services}\n",
        "en": "• Plan/Services: <b>{plan}</b> — {services}\n",
        "ku": "• پلانی/خزمەتگوزارییەکان: <b>{plan}</b> — {services}\n",
    },
    "usercard.report_lang": {
        "ar": "• 🌐 لغة التقرير: <b>{lang}</b>\n\n",
        "en": "• 🌐 Report language: <b>{lang}</b>\n\n",
        "ku": "• 🌐 زمانی ڕاپۆرت: <b>{lang}</b>\n\n",
    },
    "usercard.sections.stats": {
        "ar": "<b>📊 الإحصائيات:</b>\n",
        "en": "<b>📊 Stats:</b>\n",
        "ku": "<b>📊 ئامار:</b>\n",
    },
    "usercard.stats.line": {
        "ar": "الكل: <b>{total}</b> | آخر تقرير: <code>{last}</code>",
        "en": "Total: <b>{total}</b> | Last report: <code>{last}</code>",
        "ku": "کۆی گشتی: <b>{total}</b> | دوایین ڕاپۆرت: <code>{last}</code>",
    },
    "usercard.limits.line": {
        "ar": "اليوم {today_used}/{daily} | الشهر {month_used}/{monthly}",
        "en": "Today {today_used}/{daily} | Month {month_used}/{monthly}",
        "ku": "ئەمڕۆ {today_used}/{daily} | مانگ {month_used}/{monthly}",
    },
    "usercard.sections.subscription": {
        "ar": "<b>⏰ الاشتراك:</b>\n",
        "en": "<b>⏰ Subscription:</b>\n",
        "ku": "<b>⏰ بەشداریکردن:</b>\n",
    },
    "usercard.status.active": {
        "ar": "فعّال",
        "en": "Active",
        "ku": "چالاک",
    },
    "usercard.status.inactive": {
        "ar": "معطّل",
        "en": "Disabled",
        "ku": "ناچالاک",
    },
    "usercard.subscription.start": {
        "ar": "• تاريخ البدء: <code>{start}</code>\n",
        "en": "• Start date: <code>{start}</code>\n",
        "ku": "• بەرواری دەستپێک: <code>{start}</code>\n",
    },
    "usercard.subscription.end": {
        "ar": "• تاريخ الانتهاء: <code>{end}</code>{left}\n\n",
        "en": "• Expiry date: <code>{end}</code>{left}\n\n",
        "ku": "• بەرواری کۆتایی: <code>{end}</code>{left}\n\n",
    },
    "usercard.balance": {
        "ar": "• 💳 الرصيد المتبقي: <b>{balance}</b>\n",
        "en": "• 💳 Remaining balance: <b>{balance}</b>\n",
        "ku": "• 💳 باڵانسی ماوە: <b>{balance}</b>\n",
    },
    "usercard.note": {
        "ar": "• 📝 ملاحظة: {note}",
        "en": "• 📝 Note: {note}",
        "ku": "• 📝 تێبینی: {note}",
    },
    "usercard.left.days_remaining": {
        "ar": " (باقي <b>{days}</b> يوم)",
        "en": " (<b>{days}</b> day(s) left)",
        "ku": " (ماوەی <b>{days}</b> ڕۆژە)",
    },
    "usercard.left.today": {
        "ar": " <b>(منتهي اليوم!)</b>",
        "en": " <b>(Expires today!)</b>",
        "ku": " <b>(ئەمڕۆ کۆتایی دێت!)</b>",
    },
    "usercard.left.expired_days": {
        "ar": " <b>(منتهي منذ {days} يوم)</b>",
        "en": " <b>(Expired {days} day(s) ago)</b>",
        "ku": " <b>({days} ڕۆژ پێش ئێستا کۆتایی هاتووە)</b>",
    },
    "usercard.unlimited": {
        "ar": "غير محدود",
        "en": "Unlimited",
        "ku": "بێ سنوور",
    },
    "usercard.services.line": {
        "ar": "Carfax {carfax} | BadVin {badvin} | مزاد {auction} | حادث {accident}",
        "en": "Carfax {carfax} | BadVin {badvin} | Auction {auction} | Accident {accident}",
        "ku": "Carfax {carfax} | BadVin {badvin} | مزاد {auction} | ڕووداو {accident}",
    },
    "usercard.service.carfax": {
        "ar": "Carfax",
        "en": "Carfax",
        "ku": "Carfax",
    },
    "usercard.service.photos_badvin": {
        "ar": "صور السيارة المخفية",
        "en": "Hidden car photos",
        "ku": "وێنەکانی ئۆتۆمبیلی شاردراو",
    },
    "usercard.service.photos_auction": {
        "ar": "صور المزاد الحالي",
        "en": "Auction photos",
        "ku": "وێنەکانی مزادی ئێستا",
    },
    "usercard.service.photos_accident": {
        "ar": "صور حادث سابق",
        "en": "Accident photos",
        "ku": "وێنەکانی ڕووداوی پێشتر",
    },
    "usercard.buttons.contact": {
        "ar": "📬 مراسلة",
        "en": "📬 Contact",
        "ku": "📬 پەیوەندی",
    },
    "usercard.buttons.monthly": {
        "ar": "🟢 اشتراك شهري",
        "en": "🟢 Monthly plan",
        "ku": "🟢 ئەبۆنەی مانگانە",
    },
    "usercard.buttons.trial": {
        "ar": "🧪 تجربة مجانية",
        "en": "🧪 Free trial",
        "ku": "🧪 تاقیکردنەوەی بێ‌بەرامبەر",
    },
    "usercard.buttons.activate_custom": {
        "ar": "🧾 تفعيل مخصّص",
        "en": "🧾 Custom activation",
        "ku": "🧾 چالاککردنی تایبەت",
    },
    "usercard.buttons.quick_notify": {
        "ar": "✉️ إشعار سريع",
        "en": "✉️ Quick notify",
        "ku": "✉️ ئاگاداری خێرا",
    },
    "usercard.buttons.balance_edit": {
        "ar": "💳 ضبط الرصيد",
        "en": "💳 Adjust balance",
        "ku": "💳 ڕێکخستنی باڵانس",
    },
    "usercard.buttons.note": {
        "ar": "📝 ملاحظة",
        "en": "📝 Note",
        "ku": "📝 تێبینی",
    },
    "usercard.buttons.custom_name": {
        "ar": "🏷️ اسم مخصص",
        "en": "🏷️ Custom name",
        "ku": "🏷️ ناوی تایبەت",
    },
    "usercard.buttons.services": {
        "ar": "📦 الخدمات",
        "en": "📦 Services",
        "ku": "📦 خزمەتگوزارییەکان",
    },
    "usercard.buttons.limits": {
        "ar": "📈 الحدود",
        "en": "📈 Limits",
        "ku": "📈 سنوورەکان",
    },
    "usercard.buttons.report_lang": {
        "ar": "🌐 لغة التقرير",
        "en": "🌐 Report language",
        "ku": "🌐 زمانی ڕاپۆرت",
    },
    "usercard.buttons.audit": {
        "ar": "📊 السجل",
        "en": "📊 Log",
        "ku": "📊 تۆمار",
    },
    "usercard.buttons.notify_user": {
        "ar": "📬 تنبيه للمستخدم",
        "en": "📬 Notify user",
        "ku": "📬 ئاگاداری بۆ بەکارهێنەر",
    },
    "usercard.buttons.disable": {
        "ar": "⛔ تعطيل",
        "en": "⛔ Disable",
        "ku": "⛔ ناچالاککردن",
    },
    "usercard.buttons.delete": {
        "ar": "🗑️ حذف",
        "en": "🗑️ Delete",
        "ku": "🗑️ سڕینەوە",
    },
    "usercard.buttons.main_menu": {
        "ar": "🏠 القائمة الرئيسية",
        "en": "🏠 Main menu",
        "ku": "🏠 لیستی سەرەکی",
    },
    "usercard.buttons.back_menu": {
        "ar": "↩️ رجوع للقائمة",
        "en": "↩️ Back to menu",
        "ku": "↩️ گەڕانەوە بۆ لیستە",
    },
    "usercard.prompt.activate_custom": {
        "ar": "🧾 أرسل: <b>أيام,حد_يومي,عدد_التقارير[,تقارير_إضافية]</b> مثال <code>30,25,500</code>",
        "en": "🧾 Send: <b>days,daily_limit,monthly_limit[,extra_reports]</b> e.g. <code>30,25,500</code>",
        "ku": "🧾 بنێرە: <b>ڕۆژ، سنووری ڕۆژانە، سنووری مانگانە[,ڕاپۆرتی زیادە]</b> نموونە: <code>30,25,500</code>",
    },
    "usercard.prompt.renew_custom": {
        "ar": "♻️ أرسل عدد <b>الأيام</b> للتجديد. مثال <code>60</code>",
        "en": "♻️ Send the number of <b>days</b> to renew. Example <code>60</code>",
        "ku": "♻️ ژمارەی <b>ڕۆژەکان</b> بنێرە بۆ نوێکردنەوە. نموونە <code>60</code>",
    },
    "usercard.prompt.balance_edit": {
        "ar": "💳 أرسل قيمة الرصيد المتبقي (رقم فقط). مثال <code>1000</code>",
        "en": "💳 Send the remaining balance (numbers only). Example <code>1000</code>",
        "ku": "💳 باڵانسی ماوە بنێرە (تەنیا ژمارە). نموونە <code>1000</code>",
    },
    "usercard.prompt.custom_name": {
        "ar": "🏷️ أرسل الاسم المخصص:",
        "en": "🏷️ Send the custom name:",
        "ku": "🏷️ ناوی تایبەت بنێرە:",
    },
    "usercard.prompt.note": {
        "ar": "📝 أرسل الملاحظة (نص قصير):",
        "en": "📝 Send the note (short text):",
        "ku": "📝 تێبینی بنێرە (دەقێکی کورت):",
    },
    "usercard.notify.disabled": {
        "ar": "⛔ تم تعطيل حسابك.\nللتواصل مع الدعم: واتساب: {support}",
        "en": "⛔ Your account has been disabled.\nSupport on WhatsApp: {support}",
        "ku": "⛔ هەژمارت ناچالاک کرا.\nپشتیوانی لە واتساپ: {support}",
    },
    "usercard.result.disabled": {
        "ar": "⛔ تم التعطيل.",
        "en": "⛔ Disabled.",
        "ku": "⛔ ناچالاک کرا.",
    },
    "services.manage.title": {
        "ar": "📦 إدارة الخدمات:",
        "en": "📦 Manage services:",
        "ku": "📦 بەڕێوەبردنی خزمەتگوزارییەکان:",
    },
    "limits.manage.title": {
        "ar": "📈 إدارة الحدود:",
        "en": "📈 Manage limits:",
        "ku": "📈 بەڕێوەبردنی سنوورەکان:",
    },
    "services.status.enabled": {
        "ar": "✅ تم تفعيل",
        "en": "✅ Enabled",
        "ku": "✅ چالاک کرا",
    },
    "services.status.disabled": {
        "ar": "⛔ تم تعطيل",
        "en": "⛔ Disabled",
        "ku": "⛔ ناچالاک کرا",
    },
    "services.notify.user": {
        "ar": "{status} {service} لحسابك.",
        "en": "{status} {service} for your account.",
        "ku": "{status} {service} بۆ هەژمارت.",
    },
    "services.action.enable": {
        "ar": "فعّل",
        "en": "enabled",
        "ku": "چالاک کرد",
    },
    "services.action.disable": {
        "ar": "عطّل",
        "en": "disabled",
        "ku": "ناچالاک کرد",
    },
    "services.notify.super": {
        "ar": "🔧 (Admin:{admin}) {action} {service} للمستخدم {user}.",
        "en": "🔧 (Admin:{admin}) {action} {service} for user {user}.",
        "ku": "🔧 (Admin:{admin}) {action} {service} بۆ بەکارهێنەری {user}.",
    },
    "limits.buttons.set_daily": {
        "ar": "📅 ضبط حد يومي",
        "en": "📅 Set daily limit",
        "ku": "📅 سنووری ڕۆژانە دابنێ",
    },
    "limits.buttons.set_monthly": {
        "ar": "📆 ضبط حد شهري",
        "en": "📆 Set monthly limit",
        "ku": "📆 سنووری مانگانە دابنێ",
    },
    "limits.buttons.reset_today": {
        "ar": "🔄 تصفير عداد اليوم",
        "en": "🔄 Reset today counter",
        "ku": "🔄 ژمێریارەکەی ئەمڕۆ لە sifr بدە",
    },
    "limits.prompt.set_daily": {
        "ar": "📈 أدخل الحد اليومي الجديد (رقم):",
        "en": "📈 Enter the new daily limit (number):",
        "ku": "📈 سنووری ڕۆژانەی نوێ بنووسە (ژمارە):",
    },
    "limits.prompt.set_monthly": {
        "ar": "📈 أدخل الحد الشهري الجديد (رقم):",
        "en": "📈 Enter the new monthly limit (number):",
        "ku": "📈 سنووری مانگانەی نوێ بنووسە (ژمارە):",
    },
    "limits.reset.user_notify": {
        "ar": "🔄 <b>تم تصفير استخدامك اليومي</b>\n\nيمكنك متابعة طلب التقارير دون انتظار للغد.\n👤 بواسطة الإدارة: <code>{admin}</code>",
        "en": "🔄 <b>Your daily usage was reset</b>\n\nYou can keep requesting reports without waiting for tomorrow.\n👤 By admin: <code>{admin}</code>",
        "ku": "🔄 <b>بەکارهێنانی ڕۆژانەت سفر کرا</b>\n\nدەتوانی بەردەوام بیت لە داوای ڕاپۆرت بەبێ چاوەڕێی سبەی.\n👤 لەلایەن بەڕێوەبەر: <code>{admin}</code>",
    },
    "limits.reset.done": {
        "ar": "✅ تم تصفير عداد اليوم.",
        "en": "✅ Daily counter reset.",
        "ku": "✅ ژمێریاری ئەمڕۆ سافر کرا.",
    },
    "limits.super.daily_hit": {
        "ar": "📈 <b>{user}</b> وصل إلى الحد اليومي <b>{used}/{limit}</b>.",
        "en": "📈 <b>{user}</b> hit the daily limit <b>{used}/{limit}</b>.",
        "ku": "📈 <b>{user}</b> گەیشت بە سنووری ڕۆژانە <b>{used}/{limit}</b>.",
    },
    "limits.hit.daily.user": {
        "ar": "📈 <b>وصلت إلى الحد اليومي</b>\n\nالاستخدام الحالي: <b>{used}/{limit}</b> تقرير.\nسيُعاد ضبط العداد عند منتصف الليل أو يمكنك طلب رفع الحد من الإدارة.",
        "en": "📈 <b>You reached the daily limit</b>\n\nCurrent usage: <b>{used}/{limit}</b> reports.\nResets at midnight or ask admins to raise it.",
        "ku": "📈 <b>گەیشتی بە سنووری ڕۆژانە</b>\n\nبەکارهێنانی ئێستا: <b>{used}/{limit}</b> ڕاپۆرت.\nلە نیوەشەودا دەگەڕێتە صفر یان داوای زیادکردن بکە لە بەڕێوەبەران.",
    },
    "limits.hit.monthly.user": {
        "ar": "📊 <b>وصلت إلى الحد الشهري</b>\n\nالاستخدام الحالي: <b>{used}/{limit}</b> تقرير.\nسيُعاد ضبط العداد في بداية الشهر القادم أو راسل الإدارة لرفع الحد.",
        "en": "📊 <b>You reached the monthly limit</b>\n\nCurrent usage: <b>{used}/{limit}</b> reports.\nResets at the start of next month or contact admins to raise it.",
        "ku": "📊 <b>گەیشتی بە سنووری مانگانە</b>\n\nبەکارهێنانی ئێستا: <b>{used}/{limit}</b> ڕاپۆرت.\nلە دەستپێکی مانگی داهاتوودا دەگەڕێتە صفر یان پەیوەندی بکە بۆ زیادکردن.",
    },
    "limits.updated.daily": {
        "ar": "✅ تم ضبط الحد اليومي.",
        "en": "✅ Daily limit updated.",
        "ku": "✅ سنووری ڕۆژانە نوێکرایەوە.",
    },
    "limits.updated.monthly": {
        "ar": "✅ تم ضبط الحد الشهري.",
        "en": "✅ Monthly limit updated.",
        "ku": "✅ سنووری مانگانە نوێکرایەوە.",
    },
    # Broadcast / notifications
    "broadcast.panel.intro": {
        "ar": "📢 <b>نظام الإشعارات الجماعية</b>\n\n📊 <b>عدد المستخدمين في النظام:</b> {total}\n\nاختر نوع الإشعار الذي تريد إرساله:",
        "en": "📢 <b>Broadcast center</b>\n\n📊 <b>Total users:</b> {total}\n\nPick the notification type:",
        "ku": "📢 <b>ناوەندی ئاگادارکردنەوەی گشتی</b>\n\n📊 <b>ژمارەی بەکارهێنەرەکان:</b> {total}\n\nجۆری ئاگادارکردنەوە هەڵبژێرە:",
    },
    "broadcast.send_all.prompt": {
        "ar": "📢 <b>إرسال إشعار للجميع</b>\n\nأرسل نص الرسالة التي تريد إرسالها لجميع المستخدمين:",
        "en": "📢 <b>Send to all users</b>\n\nSend the message text to broadcast to everyone:",
        "ku": "📢 <b>ناردن بۆ هەموو بەکارهێنەرانی</b>\n\nدەقی نامەکە بنێرە بۆ ناردنی گشتی:",
    },
    "broadcast.select.title": {
        "ar": "👥 <b>اختر المستخدمين للإشعار</b>\n\n📊 <b>عدد المستخدمين:</b> {total}\n✅ <b>المختارون:</b> {selected}\n\nاضغط على المستخدمين لتحديدهم أو إلغاء تحديدهم:",
        "en": "👥 <b>Select users to notify</b>\n\n📊 <b>Total users:</b> {total}\n✅ <b>Selected:</b> {selected}\n\nTap users to toggle selection:",
        "ku": "👥 <b>بەکارهێنەران هەڵبژێرە بۆ ئاگادارکردن</b>\n\n📊 <b>کۆی بەکارهێنەر:</b> {total}\n✅ <b>هەڵبژێردراو:</b> {selected}\n\nکلیک بکە بۆ دیاریکردن یان هەڵوەشاندن:",
    },
    "broadcast.select.all_selected": {
        "ar": "✅ تم تحديد جميع المستخدمين",
        "en": "✅ All users selected",
        "ku": "✅ هەموو بەکارهێنەران هەڵبژێردران",
    },
    "broadcast.select.cleared": {
        "ar": "تم إلغاء تحديد جميع المستخدمين",
        "en": "Selection cleared",
        "ku": "هەڵبژاردن سڕایەوە",
    },
    "broadcast.error.toggle": {
        "ar": "❌ خطأ في التبديل",
        "en": "❌ Toggle failed",
        "ku": "❌ هەڵە لە گۆڕان",
    },
    "broadcast.error.page": {
        "ar": "❌ خطأ في التنقل",
        "en": "❌ Pagination error",
        "ku": "❌ هەڵە لە گوزارشتن",
    },
    "broadcast.error.none_selected": {
        "ar": "❌ لم يتم اختيار أي مستخدم.",
        "en": "❌ No users selected.",
        "ku": "❌ هیچ بەکارهێنەرێک هەڵبژێردراو نییە.",
    },
    "broadcast.error.empty_message": {
        "ar": "❌ يرجى إدخال نص الرسالة.",
        "en": "❌ Please enter the message text.",
        "ku": "❌ تکایە دەقی نامەک بنووسە.",
    },
    "broadcast.error.no_users": {
        "ar": "❌ لا يوجد مستخدمون في النظام.",
        "en": "❌ No users in the system.",
        "ku": "❌ هیچ بەکارهێنەرێک نییە لە سیستەمدا.",
    },
    "broadcast.error.type": {
        "ar": "❌ نوع إشعار غير صحيح.",
        "en": "❌ Invalid notification type.",
        "ku": "❌ جۆری ئاگادارکردنەوە نادروستە.",
    },
    "broadcast.send_selected.prompt": {
        "ar": "📢 <b>إرسال إشعار لـ {count} مستخدم</b>\n\nأرسل نص الرسالة التي تريد إرسالها:",
        "en": "📢 <b>Send a notification to {count} user(s)</b>\n\nSend the message text:",
        "ku": "📢 <b>ئاگادارکردنەوە بنێرە بۆ {count} بەکارهێنەر</b>\n\nدەقی نامەکە بنێرە:",
    },
    "broadcast.status.all": {
        "ar": "🔄 جاري إرسال الإشعار لجميع المستخدمين ({count} مستخدم)...",
        "en": "🔄 Sending notification to all users ({count})...",
        "ku": "🔄 ناردنی ئاگادارکردنەوە بۆ هەموو بەکارهێنەران ({count})...",
    },
    "broadcast.status.selected": {
        "ar": "🔄 جاري إرسال الإشعار لـ {count} مستخدم مختار...",
        "en": "🔄 Sending notification to {count} selected user(s)...",
        "ku": "🔄 ناردنی ئاگادارکردنەوە بۆ {count} بەکارهێنەری هەڵبژێردراو...",
    },
    "broadcast.message.header": {
        "ar": "📢 <b>إشعار من الإدارة</b>\n\n{body}",
        "en": "📢 <b>Admin notification</b>\n\n{body}",
        "ku": "📢 <b>ئاگادارکردنەوەی بەڕێوەبەرایەتی</b>\n\n{body}",
    },
    "broadcast.result.summary": {
        "ar": "✅ <b>اكتمل إرسال الإشعارات</b>\n\n📊 <b>الإحصائيات:</b>\n• ✅ نجح: {success}\n• ❌ فشل: {failed}\n• 📝 المجموع: {total}\n",
        "en": "✅ <b>Broadcast finished</b>\n\n📊 <b>Stats:</b>\n• ✅ Sent: {success}\n• ❌ Failed: {failed}\n• 📝 Total: {total}\n",
        "ku": "✅ <b>ناردنی گشتی تەواو بوو</b>\n\n📊 <b>ئامار:</b>\n• ✅ سەرکەوتو: {success}\n• ❌ شکستی هێنا: {failed}\n• 📝 کۆ: {total}\n",
    },
    "broadcast.result.failed_list": {
        "ar": "❌ <b>المستخدمون الذين فشل الإرسال لهم:</b>\n{users}",
        "en": "❌ <b>Failed for these users:</b>\n{users}",
        "ku": "❌ <b>ئەم بەکارهێنەرانە نەگەیشتن:</b>\n{users}",
    },
    "broadcast.result.failed_count": {
        "ar": "❌ فشل الإرسال لـ {count} مستخدم",
        "en": "❌ Failed to send to {count} user(s)",
        "ku": "❌ نەتوانرا بنێردرێت بۆ {count} بەکارهێنەر",
    },
    # Reports / VIN processing
    "report.error.generic": {
        "ar": "⚠️ تعذّر معالجة التقرير.",
        "en": "⚠️ Could not process the report.",
        "ku": "⚠️ نەتوانرا ڕاپۆرت کاربکات.",
    },
    "report.error.fetch": {
        "ar": "⚠️ فشل جلب التقرير.",
        "en": "⚠️ Report fetch failed.",
        "ku": "⚠️ هێنانی ڕاپۆرت شکستی هێنا.",
    },
    "report.error.fetch_detailed": {
        "ar": "⚠️ فشل جلب تقرير VIN: {error}",
        "en": "⚠️ Failed to fetch VIN report: {error}",
        "ku": "⚠️ ڕاپۆرتی VIN هێنەنەوەی شکستی هێنا: {error}",
    },
    "report.error.pdf": {
        "ar": "⚠️ تعذّر إنشاء ملف PDF.",
        "en": "⚠️ Failed to generate PDF.",
        "ku": "⚠️ نەتوانرا PDF دروست بکرێت.",
    },
    "report.error.pdf_render": {
        "ar": "⚠️ تعذّر تحويل التقرير إلى PDF.",
        "en": "⚠️ Could not render the report to PDF.",
        "ku": "⚠️ نەتوانرا ڕاپۆرت بۆ PDF بگۆڕدرێت.",
    },
    "report.refund.note": {
        "ar": "\n\n🔁 تم إعادة الرصيد تلقائياً.",
        "en": "\n\n🔁 Credit was refunded automatically.",
        "ku": "\n\n🔁 باڵانس خۆکارانە گەڕایەوە.",
    },
    "report.success.note": {
        "ar": "\n\n✅ تم التسليم بنجاح.",
        "en": "\n\n✅ Delivered successfully.",
        "ku": "\n\n✅ بە سەرکەوتوویی گەیاندرا.",
    },
    "report.success.pdf_note": {
        "ar": "\n\n✅ تم التسليم (PDF).",
        "en": "\n\n✅ Delivered (PDF).",
        "ku": "\n\n✅ نێردرا (PDF).",
    },
    "report.success.pdf_direct": {
        "ar": "✅ تم استلام ملف PDF مباشر.",
        "en": "✅ Received a direct PDF file.",
        "ku": "✅ پەڕگەی PDF ڕاستەوخۆ وەرگیرایەوە.",
    },
    "report.success.pdf_created": {
        "ar": "✅ تم إنشاء ملف PDF للتقرير.",
        "en": "✅ Generated a PDF for the report.",
        "ku": "✅ PDF بۆ ڕاپۆرت دروست کرا.",
    },
    "report.invalid_vin": {
        "ar": "❌ رقم VIN غير صالح.",
        "en": "❌ Invalid VIN number.",
        "ku": "❌ ژمارەی VIN دروست نییە.",
    },
    "report.dashboard.success": {
        "ar": "تقرير VIN {vin} سُلّم للمستخدم {user} — الرصيد المتبقي: {remaining}",
        "en": "VIN report {vin} delivered to {user} — remaining credit: {remaining}",
        "ku": "ڕاپۆرتی VIN {vin} نێردرا بۆ {user} — باڵانسی ماوە: {remaining}",
    },
    "report.dashboard.failure": {
        "ar": "فشل جلب VIN {vin} للمستخدم {user}: {error}",
        "en": "VIN {vin} failed for user {user}: {error}",
        "ku": "VIN {vin} بۆ بەکارهێنەر {user} شکستی هێنا: {error}",
    },
    "report.dashboard.pdf_failure": {
        "ar": "فشل تحويل VIN {vin} إلى PDF للمستخدم {user} (تم رد الرصيد).",
        "en": "Failed to convert VIN {vin} to PDF for user {user} (credit refunded).",
        "ku": "هەڵە لە گۆڕینی VIN {vin} بۆ PDF بۆ {user} (باڵانس گەڕێندرایەوە).",
    },
    "report.summary.unlimited": {
        "ar": "💳 الرصيد: <b>غير محدود</b>",
        "en": "💳 Credit: <b>Unlimited</b>",
        "ku": "💳 باڵانس: <b>بێ سنوور</b>",
    },
    "report.summary.credit": {
        "ar": "💳 الرصيد المتبقي: <b>{remaining}</b>/<b>{limit}</b>",
        "en": "💳 Remaining credit: <b>{remaining}</b>/<b>{limit}</b>",
        "ku": "💳 باڵانسی ماوە: <b>{remaining}</b>/<b>{limit}</b>",
    },
    "report.summary.expires_in": {
        "ar": " — الاشتراك ينتهي بعد <b>{days}</b> يوم",
        "en": " — Subscription ends in <b>{days}</b> day(s)",
        "ku": " — بەشداریکردن دەکۆتایەوە لە <b>{days}</b> ڕۆژدا",
    },
    "report.summary.sent": {
        "ar": "✅ تم إرسال {label} لـ VIN <code>{vin}</code>{expires}\n{credit}",
        "en": "✅ Sent {label} for VIN <code>{vin}</code>{expires}\n{credit}",
        "ku": "✅ {label} نێردرا بۆ VIN <code>{vin}</code>{expires}\n{credit}",
    },
    "report.photos.toast": {
        "ar": "✅ تم إرسال الصور وظهرت أسفل الرسائل.",
        "en": "✅ Photos sent and shown below.",
        "ku": "✅ وێنەکان نێردران و خوارەوە پیشان دراون.",
    },
    "report.photos.error": {
        "ar": "⚠️ تعذّر تحميل الصور حالياً.",
        "en": "⚠️ Unable to load photos right now.",
        "ku": "⚠️ نەتوانرا ئێستا وێنەکان داونلۆد بکرێن.",
        "ckb": "⚠️ نەتوانرا ئێستا وێنەکان داگرتە بکرێن.",
    },
    "report.photos.collecting": {
        "ar": "⏳ <b>{label}</b>\nيتم الآن جمع الصور لـ VIN <code>{vin}</code>...",
        "en": "⏳ <b>{label}</b>\nCollecting photos for VIN <code>{vin}</code>...",
        "ku": "⏳ <b>{label}</b>\nخەزنکردنی وێنەکان بۆ VIN <code>{vin}</code>...",
        "ckb": "⏳ <b>{label}</b>\nکۆکردنەوەی وێنەکان بۆ VIN <code>{vin}</code>...",
    },
    "photos.label.hidden": {
        "ar": "صور السيارة المخفية",
        "en": "Hidden car photos",
        "ku": "وێنەکانی نەهێنراوی ئۆتۆمبێل",
        "ckb": "وێنەکانی نەهێنراوی ئۆتۆمبێل",
    },
    "photos.label.auction": {
        "ar": "صور المزاد الحالي",
        "en": "Current auction photos",
        "ku": "وێنەکانی مزاودەی ئێستا",
        "ckb": "وێنەکانی مزایدەی ئێستا",
    },
    "photos.label.accident": {
        "ar": "صور حادث سابق",
        "en": "Accident photos",
        "ku": "وێنەکانی ڕووداو",
        "ckb": "وێنەکانی ڕووداو",
    },
    "report.photos.empty.hidden": {
        "ar": "⚠️ لا توجد صور السيارة المخفية متاحة حالياً.",
        "en": "⚠️ No hidden car photos are available right now.",
        "ku": "⚠️ هیچ وێنەی نەشاردراوی ئۆتۆمبێل نییە لە ئێستا.",
        "ckb": "⚠️ هیچ وێنەی نەهێنراوی ئۆتۆمبێل نییە لە ئێستا.",
    },
    "report.photos.empty.auction": {
        "ar": "⚠️ لا توجد صور مزاد حالياً.",
        "en": "⚠️ No auction photos are available right now.",
        "ku": "⚠️ هیچ وێنەی مزاودە نییە لە ئێستا.",
        "ckb": "⚠️ هیچ وێنەی مزایدە نییە لە ئێستا.",
    },
    "report.photos.empty.accident": {
        "ar": "⚠️ لا توجد صور حادث متاحة لهذا رقم الشاصي.",
        "en": "⚠️ No accident photos are available for this VIN.",
        "ku": "⚠️ هیچ وێنەی ڕووداو بۆ ئەم VIN ـە نییە.",
        "ckb": "⚠️ هیچ وێنەی ڕووداو بۆ ئەم VIN ـە نییە.",
    },
    "report.photos.accident.error": {
        "ar": "⚠️ حدث خطأ أثناء جلب صور الحادث.",
        "en": "⚠️ Error while fetching accident photos.",
        "ku": "⚠️ هەڵە ڕوویدا لە هێنانى وێنەکانى ڕووداو.",
        "ckb": "⚠️ هەڵە ڕوویدا لە هێنانی وێنەکانی ڕووداو.",
    },
    "language.change.prompt": {
        "ar": "🌐 <b>تغيير لغة التقرير</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<b>اللغة الحالية:</b> {current}\n\nاختر اللغة الجديدة:",
        "en": "🌐 <b>Change report language</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<b>Current language:</b> {current}\n\nPick a new language:",
        "ku": "🌐 <b>گۆڕینی زمانی ڕاپۆرت</b>\n\n━━━━━━━━━━━━━━━━━━━━\n<b>زمانی ئێستا:</b> {current}\n\nزمانی نوێ هەڵبژێرە:",
    },
    # Admin settings / super admins
    "settings.buttons.secrets_policy": {
        "ar": "🔒 سياسة الأسرار (.env)",
        "en": "🔒 Secrets policy (.env)",
        "ku": "🔒 سیاسەتی نهێنی (.env)",
    },
    "settings.buttons.activation_presets": {
        "ar": "🧾 قوالب التفعيل",
        "en": "🧾 Activation presets",
        "ku": "🧾 قالەبی چالاککردن",
    },
    "settings.buttons.add_super_admin": {
        "ar": "👑 إضافة سوبر أدمن",
        "en": "👑 Add super admin",
        "ku": "👑 زیاکردنی سوپەر ئادمین",
    },
    "settings.buttons.manage_supers": {
        "ar": "🗂️ إدارة السوبر أدمن",
        "en": "🗂️ Manage super admins",
        "ku": "🗂️ بەڕێوەبردنی سوپەر ئادمینەکان",
    },
    "settings.buttons.reload_env": {
        "ar": "🔄 إعادة تحميل .env",
        "en": "🔄 Reload .env",
        "ku": "🔄 دووبارە .env بکەرەوە",
    },
    "settings.buttons.main_menu": {
        "ar": "🏠 القائمة الرئيسية",
        "en": "🏠 Main menu",
        "ku": "🏠 لیستی سەرەکی",
    },
    "settings.buttons.edit_trial": {
        "ar": "✏️ تعديل القالب التجريبي",
        "en": "✏️ Edit trial preset",
        "ku": "✏️ دەستکاری قالەبی تاقیکردنەوە",
    },
    "settings.buttons.edit_monthly": {
        "ar": "✏️ تعديل قالب الاشتراك",
        "en": "✏️ Edit subscription preset",
        "ku": "✏️ دەستکاری قالەبی بەشداریکردن",
    },
    "settings.buttons.reset_presets": {
        "ar": "♻️ إعادة القيم الافتراضية",
        "en": "♻️ Reset defaults",
        "ku": "♻️ ڕێکخستنی بنەڕەتی",
    },
    "settings.buttons.back_settings": {
        "ar": "↩️ رجوع للإعدادات",
        "en": "↩️ Back to settings",
        "ku": "↩️ گەڕانەوە بۆ ڕێکخستنەکان",
    },
    "settings.secrets_policy.text": {
        "ar": (
            "🔒 <b>سياسة إدارة الأسرار</b>\n\n"
            "• يتم حفظ التوكنات وكلمات المرور داخل ملف <code>.env</code> فقط.\n"
            "• المتغيرات المدعومة: <code>API_TOKEN</code>, <code>BADVIN_EMAIL</code>, <code>BADVIN_PASSWORD</code>.\n"
            "• بعد التعديل، استخدم زر <b>🔄 إعادة تحميل .env</b> لتطبيق التغييرات دون إعادة التشغيل."
        ),
        "en": (
            "🔒 <b>Secrets management</b>\n\n"
            "• Tokens and passwords must live in <code>.env</code>.\n"
            "• Supported vars: <code>API_TOKEN</code>, <code>BADVIN_EMAIL</code>, <code>BADVIN_PASSWORD</code>.\n"
            "• After editing, press <b>🔄 Reload .env</b> to apply without restart."
        ),
        "ku": (
            "🔒 <b>سیاسەتی نهێنی</b>\n\n"
            "• تۆکەن و تێپەڕەوشەکان تەنها لە <code>.env</code> دەنوسرێن.\n"
            "• گۆڕاوە پشتیوانی کراوەکان: <code>API_TOKEN</code>, <code>BADVIN_EMAIL</code>, <code>BADVIN_PASSWORD</code>.\n"
            "• دوای گۆڕان، دوگمەی <b>🔄 دووبارە .env</b> بەکاربەرە بێ گەڕاندنەوە."
        ),
    },
    "settings.env.locked": {
        "ar": "🔒 <b>تم قفل تعديل هذا الإعداد من داخل البوت</b>\n\nقم بتحديث المتغير <code>{env_var}</code> داخل ملف <code>.env</code> ثم استخدم زر \"🔄 إعادة تحميل .env\" لتطبيق التغييرات.",
        "en": "🔒 <b>This setting is locked in-bot</b>\n\nUpdate <code>{env_var}</code> in <code>.env</code> then press \"🔄 Reload .env\" to apply.",
        "ku": "🔒 <b>ئەم ڕێکخستنە لە ناو بۆت داخراوە</b>\n\nگۆڕاوەی <code>{env_var}</code> لە <code>.env</code> نوێ بکەوە پاشان \"🔄 دووبارە .env\" داگرە.",
    },
    "settings.menu.summary": {
        "ar": (
            "⚙️ <b>إعدادات النظام</b>\n\n"
            "<b>📋 الإعدادات الحالية:</b>\n"
            "🪪 API Token (.env): <code>{api_token}</code>\n"
            "📧 Badvin Email (.env): <code>{badvin_email}</code>\n"
            "🔐 Badvin Password (.env): <code>{badvin_password}</code>\n\n"
            "<b>👑 السوبر أدمن:</b>\n"
            "• من .env: <b>{env_count}</b>\n"
            "• من db.json: <b>{db_count}</b>\n"
            "• الإجمالي: <b>{total}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "اختر الإعداد الذي تريد تعديله:"
        ),
        "en": (
            "⚙️ <b>System settings</b>\n\n"
            "<b>📋 Current values:</b>\n"
            "🪪 API Token (.env): <code>{api_token}</code>\n"
            "📧 Badvin Email (.env): <code>{badvin_email}</code>\n"
            "🔐 Badvin Password (.env): <code>{badvin_password}</code>\n\n"
            "<b>👑 Super admins:</b>\n"
            "• From .env: <b>{env_count}</b>\n"
            "• From db.json: <b>{db_count}</b>\n"
            "• Total: <b>{total}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Pick a setting to edit:"
        ),
        "ku": (
            "⚙️ <b>ڕێکخستنی سیستەم</b>\n\n"
            "<b>📋 نرخەکانی ئێستا:</b>\n"
            "🪪 API Token (.env): <code>{api_token}</code>\n"
            "📧 Badvin Email (.env): <code>{badvin_email}</code>\n"
            "🔐 Badvin Password (.env): <code>{badvin_password}</code>\n\n"
            "<b>👑 سوپەر ئادمینەکان:</b>\n"
            "• لە .env: <b>{env_count}</b>\n"
            "• لە db.json: <b>{db_count}</b>\n"
            "• کۆی گشتی: <b>{total}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ڕێکخستنێک هەڵبژێرە بۆ دەستکاری."
        ),
    },
    "settings.unauthorized.debug": {
        "ar": (
            "❌ <b>غير مصرح لك بالوصول إلى هذه الإعدادات.</b>\n\n"
            "معرفك: <code>{tg_id}</code>\n"
            "السوبر أدمن من .env: {env_admins}\n"
            "السوبر أدمن من db.json: {db_admins}\n\n"
            "<i>لإضافة نفسك كسوبر أدمن، أضف معرفك في TELEGRAM_SUPER_ADMINS</i>"
        ),
        "en": (
            "❌ <b>You are not allowed to access settings.</b>\n\n"
            "Your ID: <code>{tg_id}</code>\n"
            "Super admins from .env: {env_admins}\n"
            "Super admins from db.json: {db_admins}\n\n"
            "<i>Add your ID to TELEGRAM_SUPER_ADMINS to grant access.</i>"
        ),
        "ku": (
            "❌ <b>دەسترسی بە ئەم ڕێکخستنە ناتوانیت.</b>\n\n"
            "ناسنامەکەت: <code>{tg_id}</code>\n"
            "سوپەر ئادمینەکان لە .env: {env_admins}\n"
            "سوپەر ئادمینەکان لە db.json: {db_admins}\n\n"
            "<i>بۆ زیادکردنی خۆت، ناسنامەکەت لە TELEGRAM_SUPER_ADMINS دابنێ</i>"
        ),
    },
    "settings.activation_presets.body": {
        "ar": (
            "🧾 <b>قوالب التفعيل السريعة</b>\n\n"
            "يمكنك تحديد القيم الافتراضية التي ستظهر عند وصول طلبات التفعيل الجديدة.\n\n"
            "🧪 <b>التجربة:</b> <b>{trial_days}</b> يوم — حد <b>{trial_daily}/{trial_monthly}</b>\n"
            "🟢 <b>الاشتراك:</b> <b>{monthly_days}</b> يوم — حد <b>{monthly_daily}/{monthly_monthly}</b>\n\n"
            "✏️ اختر قالباً لتعديل الأيام والحدود اليومية/الشهرية أو أعد الضبط للقيم الافتراضية."
        ),
        "en": (
            "🧾 <b>Quick activation presets</b>\n\n"
            "Define default values used when new activation requests arrive.\n\n"
            "🧪 <b>Trial:</b> <b>{trial_days}</b> days — limit <b>{trial_daily}/{trial_monthly}</b>\n"
            "🟢 <b>Subscription:</b> <b>{monthly_days}</b> days — limit <b>{monthly_daily}/{monthly_monthly}</b>\n\n"
            "✏️ Pick a preset to edit days and daily/monthly limits or reset to defaults."
        ),
        "ku": (
            "🧾 <b>قالەبی خێرای چالاککردن</b>\n\n"
            "نرخە بنەڕەتییەکان دیاری بکە بۆ داواکاریە نوێکان.\n\n"
            "🧪 <b>تاقیکردنەوە:</b> <b>{trial_days}</b> ڕۆژ — سنوور <b>{trial_daily}/{trial_monthly}</b>\n"
            "🟢 <b>بەشداریکردن:</b> <b>{monthly_days}</b> ڕۆژ — سنوور <b>{monthly_daily}/{monthly_monthly}</b>\n\n"
            "✏️ قالەبێک هەڵبژێرە بۆ دەستکاری ڕۆژ و سنوورەکانی ڕۆژانە/مانگانە یان ڕیسێتکردن."
        ),
    },
    "settings.activation_edit.title.trial": {
        "ar": "القالب التجريبي",
        "en": "Trial preset",
        "ku": "قالەبی تاقیکردنەوە",
    },
    "settings.activation_edit.title.monthly": {
        "ar": "قالب الاشتراك",
        "en": "Subscription preset",
        "ku": "قالەبی بەشداریکردن",
    },
    "settings.activation_edit.prompt": {
        "ar": (
            "✏️ <b>{title}</b>\n\n"
            "القيم الحالية: <b>{days}</b> يوم — حد <b>{daily}/{monthly}</b>\n\n"
            "📥 أرسل القيم بصيغة: <code>أيام,حد_يومي,عدد_التقارير</code>\n"
            "مثال: <code>30,25,500</code>"
        ),
        "en": (
            "✏️ <b>{title}</b>\n\n"
            "Current values: <b>{days}</b> days — limit <b>{daily}/{monthly}</b>\n\n"
            "📥 Send as: <code>days,daily_limit,monthly_limit</code>\n"
            "Example: <code>30,25,500</code>"
        ),
        "ku": (
            "✏️ <b>{title}</b>\n\n"
            "نرخە ئێستایەکان: <b>{days}</b> ڕۆژ — سنوور <b>{daily}/{monthly}</b>\n\n"
            "📥 بنێرە بە شێوەی: <code>ڕۆژ،سنووری ڕۆژانە،سنووری مانگانە</code>\n"
            "نمونە: <code>30,25,500</code>"
        ),
    },
    "settings.activation_edit.format_hint": {
        "ar": "⚠️ الصيغة: <code>أيام,حد_يومي,عدد_التقارير</code> مثال: <code>30,25,500</code>",
        "en": "⚠️ Format: <code>days,daily_limit,monthly_limit</code> Example: <code>30,25,500</code>",
        "ku": "⚠️ شێواز: <code>ڕۆژ،سنووری ڕۆژانە،سنووری مانگانە</code> نمونە: <code>30,25,500</code>",
    },
    "settings.activation_edit.unknown": {
        "ar": "⚠️ قالب غير معروف.",
        "en": "⚠️ Unknown preset.",
        "ku": "⚠️ قالەبی نادیار.",
    },
    "settings.activation_edit.invalid_numbers": {
        "ar": "⚠️ الرجاء إدخال أرقام صحيحة مفصولة بفواصل.",
        "en": "⚠️ Please enter valid numbers separated by commas.",
        "ku": "⚠️ تکایە ژمارەی دروست بە کۆما دابەش بکە.",
    },
    "settings.activation_edit.updated": {
        "ar": "✅ تم تحديث قالب {title}: <b>{days}</b> يوم — حد <b>{daily}/{monthly}</b>",
        "en": "✅ Updated {title}: <b>{days}</b> days — limit <b>{daily}/{monthly}</b>",
        "ku": "✅ {title} نوێکرایەوە: <b>{days}</b> ڕۆژ — سنوور <b>{daily}/{monthly}</b>",
    },
    "settings.activation_reset.done": {
        "ar": "♻️ تمت إعادة ضبط القوالب إلى القيم الافتراضية.",
        "en": "♻️ Presets reset to defaults.",
        "ku": "♻️ قالەبەکان بۆ بنەڕەت گەڕێندرایەوە.",
    },
    "settings.supers.add.prompt": {
        "ar": (
            "👑 <b>إضافة سوبر أدمن</b>\n\n"
            "📝 أرسل الآن Telegram ID للسوبر أدمن الجديد.\n"
            "• يجب أن يكون رقماً فقط (مثال: <code>123456789</code>)\n"
            "• يمكنك الحصول على ID من @userinfobot\n\n"
            "💡 <i>يمكنك إلغاء العملية بالضغط على زر الإلغاء أو كتابة \"إلغاء\"</i>"
        ),
        "en": (
            "👑 <b>Add a super admin</b>\n\n"
            "📝 Send the Telegram ID for the new super admin.\n"
            "• Digits only (example: <code>123456789</code>)\n"
            "• You can fetch the ID from @userinfobot\n\n"
            "💡 <i>Cancel via the cancel button or by typing \"cancel\"</i>"
        ),
        "ku": (
            "👑 <b>زیادکردنی سوپەر ئادمین</b>\n\n"
            "📝 ناسنامەی تلێگرام بنێرە بۆ سوپەر ئادمینی نوێ.\n"
            "• تەنها ژمارە (نمونە: <code>123456789</code>)\n"
            "• دەتوانیت ID لە @userinfobot وەرگری\n\n"
            "💡 <i>هەڵوەشاندنەوە لەڕێی دوگمەی هەڵوەشاندن یان نووسینی \"cancel\".</i>"
        ),
    },
    "settings.supers.manage.empty": {
        "ar": "❌ لا يوجد سوبر أدمن مضاف في db.json بعد.",
        "en": "❌ No super admins in db.json yet.",
        "ku": "❌ هێشتا هیچ سوپەر ئادمینێک لە db.json نییە.",
    },
    "settings.supers.manage.header": {
        "ar": "🗂️ <b>إدارة السوبر أدمن</b>\n\n📊 <b>إجمالي السوبر أدمن:</b> {count}\n\n<b>قائمة السوبر أدمن:</b>\n",
        "en": "🗂️ <b>Super admin management</b>\n\n📊 <b>Total super admins:</b> {count}\n\n<b>List:</b>\n",
        "ku": "🗂️ <b>بەڕێوەبردنی سوپەر ئادمین</b>\n\n📊 <b>کۆی سوپەر ئادمین:</b> {count}\n\n<b>لیست:</b>\n",
    },
    "settings.supers.manage.footer": {
        "ar": "\n💡 <i>يمكن حذف السوبر أدمن من db.json فقط (ليس من .env)</i>",
        "en": "\n💡 <i>Only super admins from db.json can be removed (not from .env).</i>",
        "ku": "\n💡 <i>تەنها سوپەر ئادمینەکانی db.json دەکرێن بسڕێنەوە (نە لە .env).</i>",
    },
    "settings.supers.status.env_suffix": {
        "ar": " (من .env)",
        "en": " (from .env)",
        "ku": " (لە .env)",
    },
    "settings.supers.status.env_label": {
        "ar": "🔒 من .env (محظور)",
        "en": "🔒 From .env (locked)",
        "ku": "🔒 لە .env (داخراو)",
    },
    "settings.supers.status.db_label": {
        "ar": "👤 من db.json",
        "en": "👤 From db.json",
        "ku": "👤 لە db.json",
    },
    "settings.supers.button.blocked_suffix": {
        "ar": " (محظور)",
        "en": " (locked)",
        "ku": " (داخراو)",
    },
    "settings.supers.delete.missing_target": {
        "ar": "❌ لم يتم تمرير رقم المستخدم المطلوب حذفه.",
        "en": "❌ Missing target user ID to delete.",
        "ku": "❌ ناسنامەی بەکارهێنەری مەبەست بوونی نییە بۆ سڕینەوە.",
    },
    "settings.supers.delete.only_ultimate": {
        "ar": "❌ فقط السوبر الأدمن المطلق من .env يمكنه الحذف.",
        "en": "❌ Only the ultimate super admin from .env can delete others.",
        "ku": "❌ تەنها سوپەر ئادمینی سەرەکی لە .env دەتوانێت بسڕێتەوە.",
    },
    "settings.supers.delete.not_env_deletable": {
        "ar": "❌ لا يمكن حذف السوبر الأدمن المطلق من .env.",
        "en": "❌ The ultimate super admin from .env cannot be deleted.",
        "ku": "❌ سوپەر ئادمینی سەرەکی لە .env ناتوانرێت بسڕدرێتەوە.",
    },
    "settings.supers.delete.success": {
        "ar": "✅ <b>تم الحذف بنجاح</b>\n\n🗑️ تم حذف السوبر أدمن: <code>{target}</code>\n\n📊 <b>السوبر أدمن المتبقي:</b> {remaining}",
        "en": "✅ <b>Deleted successfully</b>\n\n🗑️ Removed super admin: <code>{target}</code>\n\n📊 <b>Remaining super admins:</b> {remaining}",
        "ku": "✅ <b>بە سەرکەوتووی سڕایەوە</b>\n\n🗑️ سوپەر ئادمین سڕایەوە: <code>{target}</code>\n\n📊 <b>سوپەر ئادمینە ماوەکان:</b> {remaining}",
    },
    "settings.supers.delete.not_found": {
        "ar": "⚠️ هذا المستخدم غير موجود.",
        "en": "⚠️ This user does not exist.",
        "ku": "⚠️ ئەم بەکارهێنەرە بوونی نییە.",
    },
    "settings.supers.delete.notify": {
        "ar": "👑🗑️ تم حذف سوبر أدمن: {target} بواسطة {by}",
        "en": "👑🗑️ Super admin removed: {target} by {by}",
        "ku": "👑🗑️ سوپەر ئادمین سڕایەوە: {target} لەلایەن {by}",
    },
    "settings.supers.add.notify": {
        "ar": "👑➕ تم إضافة سوبر أدمن جديد: {target} بواسطة {by}",
        "en": "👑➕ New super admin added: {target} by {by}",
        "ku": "👑➕ سوپەر ئادمینی نوێ زیاد کرا: {target} لەلایەن {by}",
    },
    "settings.reload.success": {
        "ar": (
            "✅ <b>تم إعادة تحميل .env بنجاح</b>\n\n"
            "🔄 تم تحديث جميع متغيرات البيئة من ملف .env\n\n"
            "💡 <i>تم تحديث:\n"
            "• BOT_TOKEN\n"
            "• TELEGRAM_SUPER_ADMINS\n"
            "• BADVIN_EMAIL\n"
            "• BADVIN_PASSWORD</i>"
        ),
        "en": (
            "✅ <b>.env reloaded successfully</b>\n\n"
            "🔄 Environment variables refreshed from .env\n\n"
            "💡 <i>Updated:\n"
            "• BOT_TOKEN\n"
            "• TELEGRAM_SUPER_ADMINS\n"
            "• BADVIN_EMAIL\n"
            "• BADVIN_PASSWORD</i>"
        ),
        "ku": (
            "✅ <b>.env بە سەرکەوتووی نوێکرایەوە</b>\n\n"
            "🔄 گۆڕاوەکانی ژینگە لە .env نوێکرانەوە\n\n"
            "💡 <i>نوێکرانەوە:\n"
            "• BOT_TOKEN\n"
            "• TELEGRAM_SUPER_ADMINS\n"
            "• BADVIN_EMAIL\n"
            "• BADVIN_PASSWORD</i>"
        ),
    },
    "settings.reload.error": {
        "ar": "❌ <b>خطأ في إعادة تحميل .env</b>\n\n{error}",
        "en": "❌ <b>Error reloading .env</b>\n\n{error}",
        "ku": "❌ <b>هەڵە لە دووبارە بارکردنی .env</b>\n\n{error}",
    },
    "settings.unknown_action": {
        "ar": "⚠️ إجراء غير معروف.",
        "en": "⚠️ Unknown action.",
        "ku": "⚠️ کرداری نەزانراو.",
    },
    "settings.error.no_user_id": {
        "ar": "❌ خطأ: لم يتم العثور على معرف المستخدم.",
        "en": "❌ Error: user ID not found.",
        "ku": "❌ هەڵە: ناسنامەی بەکارهێنەر نەدۆزرایەوە.",
    },
    "settings.add_super_admin.id_digits_only": {
        "ar": "❌ الرجاء إرسال Telegram ID أرقام فقط.",
        "en": "❌ Please send a numeric Telegram ID only.",
        "ku": "❌ تکایە تەنها ژمارەی Telegram ID بنێرە.",
    },
    "settings.add_super_admin.exists_db": {
        "ar": "ℹ️ هذا المستخدم موجود مسبقًا ضمن سوبر أدمن (db.json).",
        "en": "ℹ️ This user is already a super admin in db.json.",
        "ku": "ℹ️ ئەم بەکارهێنەرە پێشتر سوپەر ئادمینە لە db.json.",
    },
    "settings.add_super_admin.added_db": {
        "ar": "✅ تمت إضافة {tg_id} إلى قائمة السوبر أدمن (db.json).",
        "en": "✅ Added {tg_id} to the super admin list (db.json).",
        "ku": "✅ {tg_id} زیاد کرا بۆ لیستی سوپەر ئادمین (db.json).",
    },
    "settings.await.add_super_admin.id_digits_example": {
        "ar": "❌ يجب أن يكون Telegram ID رقماً فقط. مثال: <code>123456789</code>",
        "en": "❌ Telegram ID must be numeric only. Example: <code>123456789</code>",
        "ku": "❌ Telegram ID دەبێت تەنها ژمارە بێت. نمونە: <code>123456789</code>",
    },
    "settings.await.add_super_admin.verify_failed": {
        "ar": (
            "❌ <b>فشل التحقق من المستخدم</b>\n\n"
            "⚠️ لا يمكن الوصول إلى المستخدم بالـ ID: <code>{tg_id}</code>\n\n"
            "<b>الأسباب المحتملة:</b>\n"
            "• المستخدم غير موجود في Telegram\n"
            "• المستخدم حذف حسابه\n"
            "• المستخدم لم يبدأ محادثة مع البوت من قبل\n"
            "• ID غير صحيح\n\n"
            "💡 <i>تأكد من أن المستخدم بدأ محادثة مع البوت أولاً</i>"
        ),
        "en": (
            "❌ <b>Failed to verify user</b>\n\n"
            "⚠️ Cannot reach user with ID: <code>{tg_id}</code>\n\n"
            "<b>Possible reasons:</b>\n"
            "• User does not exist on Telegram\n"
            "• User deleted the account\n"
            "• User never started the bot\n"
            "• Invalid ID\n\n"
            "💡 <i>Ask the user to start the bot first</i>"
        ),
        "ku": (
            "❌ <b>سەلماندنی بەکارهێنەر شکستی هێنا</b>\n\n"
            "⚠️ ناتوانرێت بگەیێندرێت بە ID: <code>{tg_id}</code>\n\n"
            "<b>هۆکارە پێدراوەکان:</b>\n"
            "• بەکارهێنەر لە تلێگرام نییە\n"
            "• هەژمارەکەی سڕاوەتەوە\n"
            "• هەرگیز بۆتەکەی دەستپێنەکردووە\n"
            "• ID نادروستە\n\n"
            "💡 <i>داوای بکە بۆتەکە دەستپێبکات لە یەکەم جار</i>"
        ),
    },
    "settings.await.add_super_admin.already_super": {
        "ar": "⚠️ هذا المستخدم (<code>{tg_id}</code>) سوبر أدمن بالفعل.\n\n👤 الاسم: <b>{name}</b>\n📱 Username: {username}",
        "en": "⚠️ This user (<code>{tg_id}</code>) is already a super admin.\n\n👤 Name: <b>{name}</b>\n📱 Username: {username}",
        "ku": "⚠️ ئەم بەکارهێنەرە (<code>{tg_id}</code>) پێشتر سوپەر ئادمینە.\n\n👤 ناو: <b>{name}</b>\n📱 Username: {username}",
    },
    "settings.await.add_super_admin.env_exists": {
        "ar": "⚠️ هذا المستخدم (<code>{tg_id}</code>) سوبر أدمن من .env ولا يمكن إضافته مرة أخرى.\n\n👤 الاسم: <b>{name}</b>\n📱 Username: {username}",
        "en": "⚠️ This user (<code>{tg_id}</code>) is already a super admin from .env and cannot be re-added.\n\n👤 Name: <b>{name}</b>\n📱 Username: {username}",
        "ku": "⚠️ ئەم بەکارهێنەرە (<code>{tg_id}</code>) لە .env سوپەر ئادمینە و ناتوانرێت دووبارە زیاد بکرێت.\n\n👤 ناو: <b>{name}</b>\n📱 Username: {username}",
    },
    "settings.await.add_super_admin.added_detail": {
        "ar": (
            "✅ <b>تم إضافة سوبر أدمن بنجاح</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👑 <b>Telegram ID:</b> <code>{tg_id}</code>\n"
            "👤 <b>الاسم:</b> {name}\n"
            "📱 <b>Username:</b> {username}\n\n"
            "💡 <i>تم إضافته إلى db.json</i>"
        ),
        "en": (
            "✅ <b>Super admin added</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👑 <b>Telegram ID:</b> <code>{tg_id}</code>\n"
            "👤 <b>Name:</b> {name}\n"
            "📱 <b>Username:</b> {username}\n\n"
            "💡 <i>Added to db.json</i>"
        ),
        "ku": (
            "✅ <b>سوپەر ئادمین زیاد کرا</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👑 <b>Telegram ID:</b> <code>{tg_id}</code>\n"
            "👤 <b>ناو:</b> {name}\n"
            "📱 <b>Username:</b> {username}\n\n"
            "💡 <i>زیادکرا بۆ db.json</i>"
        ),
    },
    "settings.await.add_super_admin.already_db_detail": {
        "ar": "⚠️ هذا المستخدم (<code>{tg_id}</code>) موجود بالفعل في db.json.\n\n👤 الاسم: <b>{name}</b>\n📱 Username: {username}",
        "en": "⚠️ This user (<code>{tg_id}</code>) already exists in db.json.\n\n👤 Name: <b>{name}</b>\n📱 Username: {username}",
        "ku": "⚠️ ئەم بەکارهێنەرە (<code>{tg_id}</code>) پێشتر لە db.json هەیە.\n\n👤 ناو: <b>{name}</b>\n📱 Username: {username}",
    },
    "settings.await.add_super_admin.error": {
        "ar": "❌ <b>حدث خطأ</b>\n\n⚠️ {error}\n\n💡 <i>تأكد من أن ID صحيح وأن المستخدم موجود في Telegram</i>",
        "en": "❌ <b>An error occurred</b>\n\n⚠️ {error}\n\n💡 <i>Make sure the ID is correct and the user exists on Telegram</i>",
        "ku": "❌ <b>هەڵەیەک ڕوویدا</b>\n\n⚠️ {error}\n\n💡 <i>دڵنیا ببە ID دروستە و بەکارهێنەر لە تلێگرام هەیە</i>",
    },
    "common.user_not_found": {
        "ar": "⚠️ هذا المستخدم غير موجود.",
        "en": "⚠️ This user does not exist.",
        "ku": "⚠️ ئەم بەکارهێنەرە بوونی نییە.",
    },
    "common.enter_valid_number": {
        "ar": "⚠️ أدخل رقمًا صحيحًا.",
        "en": "⚠️ Enter a valid number.",
        "ku": "⚠️ ژمارەی دروست بنووسە.",
    },
    "common.error.super_only": {
        "ar": "❌ هذه العملية مخصصة للسوبر أدمن فقط.",
        "en": "❌ This action is for super admins only.",
        "ku": "❌ ئەم کردارە تەنها بۆ سوپەر ئادمینەکانە.",
    },
    "whoami.phone.saved": {
        "ar": "✅ تم حفظ رقم الهاتف: <code>{phone}</code>",
        "en": "✅ Phone number saved: <code>{phone}</code>",
        "ku": "✅ ژمارەی تەلەفۆن پاشەکەوت کرا: <code>{phone}</code>",
    },
    "whoami.phone.prompt": {
        "ar": (
            "📞 <b>إضافة رقم هاتف</b>\n\n"
            "اختر مفتاح الدولة ثم أرسل رقمك بدون + وبدون الصفر الأول، أو أرسل الرقم كاملاً بصيغة +E.164.\n"
            "مثال: <code>+962795378832</code> أو بعد اختيار +962 أرسل <code>795378832</code>"
        ),
        "en": (
            "📞 <b>Add phone number</b>\n\n"
            "Pick a country code, then send your number without + and without the leading zero, or send it in full +E.164 format.\n"
            "Example: <code>+962795378832</code> or after choosing +962 send <code>795378832</code>"
        ),
        "ku": (
            "📞 <b>زیادکردنی ژمارەی تەلەفۆن</b>\n\n"
            "کۆدی وڵات هەڵبژێرە پاشان ژمارەکەت بنێرە بەبێ + و بەبێ صفر لە دەستپێک، یان بە شێوەی تەواوی +E.164 بنێرە.\n"
            "نمونە: <code>+962795378832</code> یان دوای هەڵبژاردنی +962 بنێرە <code>795378832</code>"
        ),
    },
    "contact.no_username": {
        "ar": "📨 لا يملك هذا المستخدم اسم مستخدم. استخدم ID:\n<code>{tg}</code>",
        "en": "📨 This user has no username. Use the ID:\n<code>{tg}</code>",
        "ku": "📨 ئەم بەکارهێنەرە ناوی بەکارهێنەر نییە. ID بەکاربهێنە:\n<code>{tg}</code>",
    },
    "admin.activation.custom.done": {
        "ar": "✅ تم التفعيل المخصّص.",
        "en": "✅ Custom activation completed.",
        "ku": "✅ چالاککردنی تایبەت تەواو بوو.",
    },
    "admin.renew.custom.done": {
        "ar": "✅ تم التجديد المخصّص.",
        "en": "✅ Custom renewal completed.",
        "ku": "✅ نوێکردنەوەی تایبەت تەواو بوو.",
    },
    "admin.activation.custom.format_hint": {
        "ar": "⚠️ الصيغة: <code>أيام,حد_يومي,عدد_التقارير[,تقارير_إضافية]</code> مثال: <code>30,25,500</code>",
        "en": "⚠️ Format: <code>days,daily_limit,monthly_limit[,extra_reports]</code> Example: <code>30,25,500</code>",
        "ku": "⚠️ شێواز: <code>ڕۆژ،سنووری ڕۆژانە،سنووری مانگانە[,ڕاپۆرتی زیادە]</code> نمونە: <code>30,25,500</code>",
    },
    "admin.renew.invalid_days": {
        "ar": "⚠️ أرسل عدد الأيام فقط. مثال: <code>60</code>",
        "en": "⚠️ Send number of days only. Example: <code>60</code>",
        "ku": "⚠️ تەنها ژمارەی ڕۆژ بنێرە. نمونە: <code>60</code>",
    },
    "admin.balance.invalid_number": {
        "ar": "⚠️ أدخل رقمًا صحيحًا فقط. مثال: <code>1000</code>",
        "en": "⚠️ Enter a valid number only. Example: <code>1000</code>",
        "ku": "⚠️ تەنها ژمارەی دروست بنووسە. نمونە: <code>1000</code>",
    },
    "admin.balance.updated": {
        "ar": "✅ تم ضبط الرصيد المتبقي: <b>{old}</b> → <b>{new}</b> ({delta})",
        "en": "✅ Remaining balance set: <b>{old}</b> → <b>{new}</b> ({delta})",
        "ku": "✅ باڵانسی ماوە ڕێکخرا: <b>{old}</b> → <b>{new}</b> ({delta})",
    },
    "admin.name_set": {
        "ar": "✅ تم تعيين الاسم.",
        "en": "✅ Name set.",
        "ku": "✅ ناو دانرایەوە.",
    },
    "admin.note_deleted": {
        "ar": "✅ تم حذف الملاحظة.",
        "en": "✅ Note deleted.",
        "ku": "✅ تێبینی سڕایەوە.",
    },
    "admin.note_saved": {
        "ar": "✅ تم حفظ الملاحظة.",
        "en": "✅ Note saved.",
        "ku": "✅ تێبینی پاشەکەوت کرا.",
    },
    "admin.notify.sent": {
        "ar": "✅ تم إرسال التنبيه.",
        "en": "✅ Notification sent.",
        "ku": "✅ ئاگاداری نێردرا.",
    },
    "admin.notify_bulk.empty": {
        "ar": "⚠️ يرجى إرسال نص الإشعار أو صورة مع تعليق.",
        "en": "⚠️ Please send notification text or a photo with caption.",
        "ku": "⚠️ تکایە دەقی ئاگاداری یان وێنەیەک بە نووسین بنێرە.",
    },
    "admin.notify_bulk.result": {
        "ar": "✅ <b>تم إرسال الإشعار</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• تم الإرسال: <b>{sent}</b>\n• فشل: <b>{failed}</b>\n• الإجمالي: <b>{total}</b>",
        "en": "✅ <b>Notification sent</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• Sent: <b>{sent}</b>\n• Failed: <b>{failed}</b>\n• Total: <b>{total}</b>",
        "ku": "✅ <b>ئاگاداری نێردرا</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• نێردرا: <b>{sent}</b>\n• شکستی هێنا: <b>{failed}</b>\n• کۆ: <b>{total}</b>",
    },
    "help.faq": {
        "ar": (
            "📚 <b>أسئلة شائعة</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ كيف أطلب تقرير؟</b>\n"
            "اضغط على زر 📄 تقرير جديد ثم أرسل رقم الشاصي (VIN).\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ ما هو VIN؟</b>\n"
            "رقم الشاصي هو معرّف السيارة المكوّن من <b>17 خانة</b> بدون مسافات.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ ما تكلفة التقرير؟</b>\n"
            "يُخصم <b>1</b> تقرير من حدك الشهري مع كل طلب.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ ما هي الحدود؟</b>\n"
            "• الحد اليومي: <b>200</b> تقرير\n"
            "• الحد الشهري: <b>500</b> تقرير\n\n"
            "💡 <i>هذه هي الحدود الافتراضية، يمكن تعديلها من قبل الإدارة</i>"
        ),
        "en": (
            "📚 <b>FAQ</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ How do I request a report?</b>\n"
            "Tap 📄 New Report then send the VIN.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ What is a VIN?</b>\n"
            "It's the 17-character vehicle identifier with no spaces.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ How much does it cost?</b>\n"
            "Each report deducts <b>1</b> from your monthly quota.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ What are the limits?</b>\n"
            "• Daily limit: <b>200</b> reports\n"
            "• Monthly limit: <b>500</b> reports\n\n"
            "💡 <i>These are defaults; admins can adjust.</i>"
        ),
        "ku": (
            "📚 <b>پرسیارە باوەکان</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ چۆن ڕاپۆرت داوابکەم؟</b>\n"
            "دوگمەی 📄 ڕاپۆرتی نوێ بگرە و VIN بنێرە.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ VIN چییە؟</b>\n"
            "ژمارەی شاصییەکی 17 پیتە بەبێ بۆشایی.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ تێچووەکە چەندە؟</b>\n"
            "هەموو ڕاپۆرتێک <b>1</b> لە خولی مانگانەت کەم دەکاتەوە.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>❓ سنوورەکان چییە؟</b>\n"
            "• سنووری ڕۆژانە: <b>200</b> ڕاپۆرت\n"
            "• سنووری مانگانە: <b>500</b> ڕاپۆرت\n\n"
            "💡 <i>ئەمە سنوورە سەرەتایین؛ بەڕێوبەران دەکرێت بگۆڕن.</i>"
        ),
    },
    "help.returned": {
        "ar": "✅ تم الرجوع إلى القائمة.",
        "en": "✅ Returned to menu.",
        "ku": "✅ گەڕایەوە بۆ لیستە.",
    },
    "new_report.inactive": {
        "ar": (
            "📄 <b>طلب تقرير جديد</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ <b>حسابك غير مُفعّل</b>\n\n"
            "💡 <i>يجب تفعيل حسابك أولاً قبل طلب التقارير.</i>\n\n"
            "استخدم زر 🛂 طلب تفعيل لتفعيل حسابك."
        ),
        "en": (
            "📄 <b>New report request</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ <b>Your account is inactive</b>\n\n"
            "💡 <i>Please activate your account before requesting reports.</i>\n\n"
            "Use 🛂 Request activation to proceed."
        ),
        "ku": (
            "📄 <b>داوای ڕاپۆرتی نوێ</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ <b>هەژمارەکەت ناچالاکە</b>\n\n"
            "💡 <i>پێویستە هەژمارەکەت چالاک بکەیت پێش داوای ڕاپۆرت.</i>\n\n"
            "دوگمەی 🛂 داوای چالاککردنەوە بەکاربەرە."
        ),
    },
    "new_report.body": {
        "ar": (
            "📄 <b>طلب تقرير جديد</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📝 خطوات الطلب:</b>\n\n"
            "1️⃣ أرسل رقم الشاصي (VIN)\n"
            "2️⃣ يجب أن يكون <b>17 خانة</b> بالضبط\n"
            "3️⃣ بدون مسافات أو شرطات\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📌 مثال:</b>\n"
            "<code>1HGCM82633A123456</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 حدودك الحالية:</b>\n"
            "• اليوم: <b>{today_used}</b>/<b>{daily_limit}</b>\n"
            "• الشهر: <b>{monthly_label}</b>\n\n"
            "💡 <i>تلميحات:\n"
            "• إذا وصلك الرابط من موقع، انسخ VIN فقط\n"
            "• تأكد من عدم وجود مسافات أو أخطاء</i>"
        ),
        "en": (
            "📄 <b>Request a new report</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📝 Steps:</b>\n\n"
            "1️⃣ Send the VIN\n"
            "2️⃣ It must be exactly <b>17 characters</b>\n"
            "3️⃣ No spaces or dashes\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📌 Example:</b>\n"
            "<code>1HGCM82633A123456</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 Your limits:</b>\n"
            "• Today: <b>{today_used}</b>/<b>{daily_limit}</b>\n"
            "• Monthly: <b>{monthly_label}</b>\n\n"
            "💡 <i>Tips:\n"
            "• If you got a link, copy only the VIN\n"
            "• Ensure there are no spaces or typos</i>"
        ),
        "ku": (
            "📄 <b>داوای ڕاپۆرتی نوێ</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📝 هەنگاوەکان:</b>\n\n"
            "1️⃣ VIN بنێرە\n"
            "2️⃣ دەبێت <b>17 پیت</b> بیت بە تەواوی\n"
            "3️⃣ بەبێ بۆشایی یان داشەکان\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📌 نموونە:</b>\n"
            "<code>1HGCM82633A123456</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📊 سنوورەکانت:</b>\n"
            "• ئەمڕۆ: <b>{today_used}</b>/<b>{daily_limit}</b>\n"
            "• مانگانە: <b>{monthly_label}</b>\n\n"
            "💡 <i>تێبینیەکان:\n"
            "• ئەگەر بەستەرەکەت هاتووە، تەنها VIN بکۆپیەوە\n"
            "• دڵنیا ببە لە نەبوونی بۆشایی یان هەڵە</i>"
        ),
    },
    "vin.invalid": {
        "ar": "⚠️ الرجاء التأكد من رقم الشاصي الصحيح (VIN من 17 خانة) ثم أعد المحاولة.",
        "en": "⚠️ Please verify the VIN (must be 17 characters) and try again.",
        "ku": "⚠️ تکایە دڵنیابە لە دروستی VIN (دەبێت 17 پیت بێت) و دووبارە هەوڵبدە.",
    },
    "help.return": {
        "ar": "✅ تم الرجوع إلى القائمة.",
        "en": "✅ Returned to the menu.",
        "ku": "✅ گەڕایەوە بۆ لیستە.",
    },
    "vin.info": {
        "ar": "رقم الشاصي (VIN) هو معرّف السيارة من 17 خانة موجود على:\n• لوحة قرب الزجاج الأمامي من جهة السائق\n• رخصة المركبة\n• باب السائق من الداخل",
        "en": "The VIN is the 17-character vehicle ID located on:\n• The dash near the driver-side windshield\n• The vehicle registration card\n• Inside the driver door",
        "ku": "VIN ناسنامەی ئۆتۆمبێلە لە 17 پیت پێکدێت و دێت لە:\n• تابلۆی نزیک شیشەی شۆفێر\n• کارتێکی تۆماری تێبینی ئۆتۆمبێل\n• ناوەوەی دەرگای شۆفێر",
    },
    "help.return.minor": {
        "ar": "✅ تم الرجوع.",
        "en": "✅ Done.",
        "ku": "✅ تەواوبوو.",
    },
    "help.button.whatsapp": {
        "ar": "📞 واتساب",
        "en": "📞 WhatsApp",
        "ku": "📞 واتساپ",
    },
    "help.button.website": {
        "ar": "🌐 الموقع",
        "en": "🌐 Website",
        "ku": "🌐 وێبسايت",
    },
    "help.button.faq": {
        "ar": "📚 أسئلة شائعة",
        "en": "📚 FAQ",
        "ku": "📚 پرسیارە باوەکان",
    },
    "help.button.capabilities": {
        "ar": "🤖 ماذا يمكنني أن أفعل؟",
        "en": "🤖 What can I do?",
        "ku": "🤖 چی دەکرێت بکەم؟",
    },
    "help.capabilities": {
        "ar": (
            "🤖 <b>ماذا يمكنني أن أفعل؟</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📋 الميزات الرئيسية:</b>\n\n"
            "📄 <b>تقارير Carfax:</b>\n"
            "• احصل على تقارير مفصلة لأي سيارة بإرسال رقم الشاصي (VIN)\n"
            "• التقارير متوفرة بصيغة PDF عالية الجودة\n"
            "• دعم اللغات: العربية، الإنجليزية، الكردية (بادينية وسورانية)\n\n"
            "📷 <b>صور السيارات:</b>\n"
            "• صور السيارة المخفية من BadVin\n"
            "• صور المزاد الحالي من Apicar\n"
            "• صور الحوادث السابقة\n\n"
            "💳 <b>إدارة الاشتراك:</b>\n"
            "• متابعة رصيدك الشهري واليومي\n"
            "• طلب تفعيل الحساب أو رفع الحدود\n"
            "• إشعارات تلقائية عند اقتراب انتهاء الاشتراك\n\n"
            "🌐 <b>تعدد اللغات:</b>\n"
            "• تبديل فوري بين اللغات المدعومة\n"
            "• واجهة كاملة بلغتك المفضلة\n\n"
            "📱 <b>الدعم والمساعدة:</b>\n"
            "• تواصل مع فريق الدعم عبر واتساب أو البريد الإلكتروني\n"
            "• أسئلة شائعة لحل المشاكل السريعة\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>استخدم الأزرار أدناه للوصول لجميع الميزات!</i>"
        ),
        "en": (
            "🤖 <b>What can I do?</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📋 Main Features:</b>\n\n"
            "📄 <b>Carfax Reports:</b>\n"
            "• Get detailed reports for any vehicle by sending the VIN\n"
            "• High-quality PDF reports\n"
            "• Language support: Arabic, English, Kurdish (Badini & Sorani)\n\n"
            "📷 <b>Vehicle Images:</b>\n"
            "• Hidden car photos from BadVin\n"
            "• Current auction photos from Apicar\n"
            "• Previous accident photos\n\n"
            "💳 <b>Subscription Management:</b>\n"
            "• Track your monthly and daily balance\n"
            "• Request account activation or limit increases\n"
            "• Automatic notifications before subscription expiry\n\n"
            "🌐 <b>Multi-language:</b>\n"
            "• Instant switching between supported languages\n"
            "• Complete interface in your preferred language\n\n"
            "📱 <b>Support & Help:</b>\n"
            "• Contact support team via WhatsApp or Email\n"
            "• FAQ for quick solutions\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Use the buttons below to access all features!</i>"
        ),
        "ku": (
            "🤖 <b>چی دەکرێت بکەم؟</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📋 تایبەتمەندییە سەرەکییەکان:</b>\n\n"
            "📄 <b>ڕاپۆرتەکانی Carfax:</b>\n"
            "• ڕاپۆرتی ورد بۆ هەر ئۆتۆمبێلێک بە ناردنی VIN\n"
            "• ڕاپۆرتی PDF بە کوالێتی بەرز\n"
            "• پشتگیری زمان: عەرەبی، ئینگلیزی، کوردی (بادینی و سۆرانی)\n\n"
            "📷 <b>وێنەکانی ئۆتۆمبێل:</b>\n"
            "• وێنە شاراوەکان لە BadVin\n"
            "• وێنەکانی مزایدەی ئێستا لە Apicar\n"
            "• وێنەکانی ڕووداوی پێشوو\n\n"
            "💳 <b>بەڕێوەبردنی بەشداری:</b>\n"
            "• شوێنکەوتنی باڵانسی مانگانە و ڕۆژانە\n"
            "• داواکردنی چالاککردنی هەژمار یان زیادکردنی سنوور\n"
            "• ئاگادارکردنەوەی ئۆتۆماتیکی پێش کۆتایی بەشداری\n\n"
            "🌐 <b>فرە-زمان:</b>\n"
            "• گۆڕینی خێرا لە نێوان زمانە پشتگیریکراوەکان\n"
            "• ڕووکاری تەواو بە زمانی دڵخوازت\n\n"
            "📱 <b>پشتگیری و یارمەتی:</b>\n"
            "• پەیوەندی بە تیمی پشتگیری لە ڕێگەی WhatsApp یان Email\n"
            "• پرسیارە باوەکان بۆ چارەسەری خێرا\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>دوگمەکانی خوارەوە بەکاربەرە بۆ دەستگەیشتن بە هەموو تایبەتمەندییەکان!</i>"
        ),
        "ckb": (
            "🤖 <b>چی دەکرێت بکەم؟</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>📋 تایبەتمەندییە سەرەکییەکان:</b>\n\n"
            "📄 <b>ڕاپۆرتەکانی Carfax:</b>\n"
            "• ڕاپۆرتی ورد بۆ هەر ئۆتۆمبێلێک بە ناردنی VIN\n"
            "• ڕاپۆرتی PDF بە کوالێتی بەرز\n"
            "• پشتگیری زمان: عەرەبی، ئینگلیزی، کوردی (بادینی و سۆرانی)\n\n"
            "📷 <b>وێنەکانی ئۆتۆمبێل:</b>\n"
            "• وێنە شاراوەکان لە BadVin\n"
            "• وێنەکانی مزایدەی ئێستا لە Apicar\n"
            "• وێنەکانی ڕووداوی پێشوو\n\n"
            "💳 <b>بەڕێوەبردنی بەشداری:</b>\n"
            "• شوێنکەوتنی باڵانسی مانگانە و ڕۆژانە\n"
            "• داواکردنی چالاککردنی هەژمار یان زیادکردنی سنوور\n"
            "• ئاگادارکردنەوەی ئۆتۆماتیکی پێش کۆتایی بەشداری\n\n"
            "🌐 <b>فرە-زمان:</b>\n"
            "• گۆڕینی خێرا لە نێوان زمانە پشتگیریکراوەکان\n"
            "• ڕووکاری تەواو بە زمانی دڵخوازت\n\n"
            "📱 <b>پشتگیری و یارمەتی:</b>\n"
            "• پەیوەندی بە تیمی پشتگیری لە ڕێگەی WhatsApp یان Email\n"
            "• پرسیارە باوەکان بۆ چارەسەری خێرا\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>دوگمەکانی خوارەوە بەکاربەرە بۆ دەستگەیشتن بە هەموو تایبەتمەندییەکان!</i>"
        ),
    },
    "photos.badvin.label": {
        "ar": "صور السيارة المخفية",
        "en": "Hidden car photos",
        "ku": "وێنەی شۆفێرە شاراوەکان",
    },
    "photos.auction.label": {
        "ar": "صور المزاد الحالي",
        "en": "Current auction photos",
        "ku": "وێنەکانی مزادی ئێستا",
    },
    "photos.accident.label": {
        "ar": "صور حادث سابق",
        "en": "Accident photos",
        "ku": "وێنەکانی ڕووداو",
    },
    "photos.status.loading": {
        "ar": "⏳ <b>{label}</b>\nيتم الآن جمع الصور لـ VIN <code>{vin}</code>...",
        "en": "⏳ <b>{label}</b>\nFetching photos for VIN <code>{vin}</code>...",
        "ku": "⏳ <b>{label}</b>\nوێنەکان بۆ VIN <code>{vin}</code> دەهێنرێن...",
    },
    "photos.empty": {
        "ar": "⚠️ لا توجد صور متاحة حالياً.",
        "en": "⚠️ No photos are available right now.",
        "ku": "⚠️ هیچ وێنەیەک بوونی نییە لە ئێستادا.",
    },
    "photos.error": {
        "ar": "⚠️ حدث خطأ أثناء جلب الصور.",
        "en": "⚠️ An error occurred while fetching photos.",
        "ku": "⚠️ هەڵە ڕوویدا لە کاتی هێنانی وێنەکاندا.",
    },
    "photos.accident.empty": {
        "ar": "⚠️ لا توجد صور حادث متاحة لهذا رقم الشاصي.",
        "en": "⚠️ No accident images available for this VIN.",
        "ku": "⚠️ وێنەی ڕووداو بوونی نییە بۆ ئەم VIN ـە.",
    },
    "photos.accident.error": {
        "ar": "⚠️ حدث خطأ أثناء جلب صور الحادث.",
        "en": "⚠️ Error while fetching accident images.",
        "ku": "⚠️ هەڵە ڕوویدا لە هێنانی وێنەکانی ڕووداودا.",
    },
    "photos.not_enabled": {
        "ar": "⛔ {label} غير مفعلة لحسابك.",
        "en": "⛔ {label} is not enabled for your account.",
        "ku": "⛔ {label} بۆ هەژمارەکەت چالاک نەکراوە.",
    },
    "photos.summary": {
        "ar": "✅ تم إرسال {label} لـ VIN <code>{vin}</code>{days_txt}\n{credit_line}",
        "en": "✅ Sent {label} for VIN <code>{vin}</code>{days_txt}\n{credit_line}",
        "ku": "✅ {label} نێردرا بۆ VIN <code>{vin}</code>{days_txt}\n{credit_line}",
    },
    "photos.sent.notice": {
        "ar": "✅ تم إرسال الصور وظهرت أسفل الرسائل.",
        "en": "✅ Photos sent and displayed below.",
        "ku": "✅ وێنەکان نێردران و لە خوارەوە دیارە.",
    },
    "photos.credit.unlimited": {
        "ar": "💳 الرصيد: <b>غير محدود</b>",
        "en": "💳 Credit: <b>Unlimited</b>",
        "ku": "💳 کریدت: <b>بێ سنوور</b>",
    },
    "photos.credit.remaining": {
        "ar": "💳 الرصيد المتبقي: <b>{remaining}</b>/<b>{limit}</b>",
        "en": "💳 Remaining credit: <b>{remaining}</b>/<b>{limit}</b>",
        "ku": "💳 کریدتی ماوە: <b>{remaining}</b>/<b>{limit}</b>",
    },
    "photos.summary.days_left": {
        "ar": " — الاشتراك ينتهي بعد <b>{days}</b> يوم",
        "en": " — subscription ends in <b>{days}</b> days",
        "ku": " — بەروارەکە کۆتایی دەهات لە <b>{days}</b> ڕۆژدا",
    },
    "activation.prompt": {
        "ar": "🧾 طلب تفعيل\n\nأرسل رقم هاتفك بصيغة +رمز_الدولة ثم الرقم (مثال: +962795378832).\nسنقوم بمراجعة الطلب وإعلامك في أقرب وقت.",
        "en": "🧾 Activation request\n\nSend your phone number as +country_code followed by the number (example: +962795378832).\nWe will review and get back to you soon.",
        "ku": "🧾 داوای چالاککردن\n\nژمارەی مۆبایل بنێرە بە شێوەی +کۆدی وڵات و ژمارەکە (نمونە: +962795378832).\nداواکاریەکە پشکنراوە و زوو وەڵام دەدرێت.",
        "ckb": "🧾 داوای چالاککردن\n\nژمارەی مۆبایل بنێرە بە شێوەی +کۆدی وڵات و ژمارەکە (نمونە: +962795378832).\nداواکاریەکە دەبینرێت و بە نزیکترین کات وەڵام دەدرێت.",
    },
    "activation.preset.label": {
        "ar": "{title} | {days}يوم • {daily}/{monthly}",
        "en": "{title} | {days}d • {daily}/{monthly}",
        "ku": "{title} | {days} ڕۆژ • {daily}/{monthly}",
        "ckb": "{title} | {days} ڕۆژ • {daily}/{monthly}",
    },
    "menu.header": {
        "ar": "🏠 القائمة الرئيسية",
        "en": "🏠 Main Menu",
        "ku": "🏠 لیستی سەرەکی",
        "ckb": "🏠 لیستی سەرەکی",
    },
    "menu.telegram.prompt": {
        "ar": "اختر أحد الأزرار أدناه للمتابعة.",
        "en": "Pick one of the buttons below to continue.",
        "ku": "یەکێک لە دوگمەکان خوارەوە هەڵبژێرە بۆ بەردەوامبوون.",
        "ckb": "یەکێک لە دوگمەکان خوارەوە هەڵبژێرە بۆ بەردەوامبوون.",
    },
    "menu.instructions": {
        "ar": "أرسل رقم الخيار للمتابعة:",
        "en": "Send the option number to continue:",
        "ku": "ژمارەی هەڵبژاردەکە بنێرە بۆ بەردەوامبوون:",
        "ckb": "ژمارەی هەڵبژاردەکە بنێرە بۆ بەردەوامبوون:",
    },
    "menu.empty": {
        "ar": "🏠 القائمة الرئيسية\n\nلا تتوفر خيارات في الوقت الحالي.",
        "en": "🏠 Main menu\n\nNo options are available at the moment.",
        "ku": "🏠 لیستی سەرەکی\n\nلە ئێستادا هیچ هەڵبژاردەیەک نییە.",
        "ckb": "🏠 لیستی سەرەکی\n\nلە ئێستادا هیچ هەڵبژاردەیەک نییە.",
    },
    "menu.selection_required": {
        "ar": "⚠️ يرجى اختيار خيار من القائمة بإرسال رقمه أو اسمه.",
        "en": "⚠️ Please pick an option by sending its number or name.",
        "ku": "⚠️ تکایە هەڵبژاردەیەک هەڵبژێرە بە ناردنی ژمارە یان ناوی.",
        "ckb": "⚠️ تکایە هەڵبژاردەیەک هەڵبژێرە بە ناردنی ژمارە یان ناوی.",
    },
    "menu.selection_unknown": {
        "ar": "⚠️ لم يتم التعرف على الاختيار، أعد المحاولة.",
        "en": "⚠️ We couldn't understand that choice; please try again.",
        "ku": "⚠️ ئەو هەڵبژاردەیە نەزانیرا؛ تکایە دووبارە هەوڵبدە.",
        "ckb": "⚠️ ئەو هەڵبژاردەیە نەزانیرا؛ تکایە جارێکی تر هەوڵبدە.",
    },
    "menu.unavailable": {
        "ar": "⚠️ هذا الخيار قيد التطوير حاليًا.",
        "en": "⚠️ This option is under development.",
        "ku": "⚠️ ئەم هەڵبژاردەیە لە ژێر گەشەپێداندایە.",
        "ckb": "⚠️ ئەم هەڵبژاردەیە لە ژێر گەشەپێداندایە.",
    },
    "menu.admin_redirect": {
        "ar": "🔒 خيار {label} متاح من خلال لوحة Telegram الإدارية فقط.",
        "en": "🔒 The {label} option is available from the Telegram admin panel only.",
        "ku": "🔒 هەڵبژاردەی {label} تەنها لە پانێڵی بەڕێوەبەریی Telegram دەردەکەوێت.",
        "ckb": "🔒 هەڵبژاردەی {label} تەنها لە پانێڵی بەڕێوەبەرایەتیی Telegram بەردەستە.",
    },
    "media.not_found": {
        "ar": "⚠️ لم يتم العثور على المرفق الذي أرسلته.",
        "en": "⚠️ We couldn't find the attachment you sent.",
        "ku": "⚠️ پاشکەوتەکەی ناردووت نەدۆزرایەوە.",
        "ckb": "⚠️ پاشکەوتەکەی ناردووت نەدۆزرایەوە.",
    },
    "media.ack.default": {
        "ar": "📸 تم استلام المرفق بنجاح. سنخبرك في حال احتجنا إلى تفاصيل إضافية.",
        "en": "📸 Attachment received successfully. We'll let you know if more details are needed.",
        "ku": "📸 پاشکەوتەکە بەسەرکەوتووی گەیەندرا. ئەگەر وردەکاریی زیاتر پێویست بوو ئاگادارت دەکەین.",
        "ckb": "📸 پاشکەوتەکە بەسەرکەوتووی گەیەندرا. ئەگەر وردەکاری زیاتر پێویست بوو ئاگادارت دەکەین.",
    },
    "media.ack.vin": {
        "ar": "📸 تم استلام المرفق بنجاح. سنقوم بربط الصورة بطلب تقريرك الحالي وإعلامك عند الانتهاء.",
        "en": "📸 Attachment received. We'll link it to your current VIN request and update you once it's complete.",
        "ku": "📸 پاشکەوتەکە گەیەندرا. پەیوەندیی دەدەین بە داوای VIN ـی ئێستا و ئاگادارت دەکەین کاتێک تەواوبێت.",
        "ckb": "📸 پاشکەوتەکە گەیەندرا. دەیبەستین بە داوای VIN ـی ئێستا و ئاگادارت دەکەین کاتێک تەواوبێت.",
    },
    "keyboard.enabled": {
        "ar": "✅ تم تفعيل الزر بجانب المرفقات.",
        "en": "✅ The keyboard button next to attachments is now active.",
        "ku": "✅ دوگمەی تەختەکلیل لە نزیک هەڵگرتنەکان چالاک کرا.",
        "ckb": "✅ دوگمەی تەختەکلیل لە لاگەڵ پاشکەوتەکان چالاک کرا.",
    },
    "photos.options.accident": {
        "ar": "💥 صور حادث سابق",
        "en": "💥 Previous accident photos",
        "ku": "💥 وێنەکانی ڕووداوی پێشوو",
        "ckb": "💥 وێنەکانی ڕووداوی پێشووتر",
    },
    "photos.options.hidden": {
        "ar": "📷 صور السيارة المخفية",
        "en": "📷 Hidden vehicle photos",
        "ku": "📷 وێنەکانی ئۆتۆمبێلی شاردراو",
        "ckb": "📷 وێنەکانی ئۆتۆمبێلی شاردراو",
    },
    "media.ack.support": {
        "ar": "📸 تم استلام المرفق بنجاح. فريق الدعم سيراجعها ويتواصل معك قريبًا.",
        "en": "📸 Attachment received. Our support team will review it and get back to you soon.",
        "ku": "📸 پاشکەوتەکە گەیەندرا. تیمی پشتگیری پشکنین دەکات و زوو پەیوەندیت پێوە دەکات.",
        "ckb": "📸 پاشکەوتەکە گەیەندرا. تیمی پاڵپشت دەبینێتەوە و زوو پەیوەندیت دەکات.",
    },
    "limit.block.daily": {
        "ar": "📈 وصلت إلى الحد اليومي المسموح به.\nالاستخدام الحالي: {today_used}/{daily_limit}.",
        "en": "📈 You've reached the daily usage limit.\nCurrent usage: {today_used}/{daily_limit}.",
        "ku": "📈 گەیشتیتە سنووری ڕۆژانەی ڕێگەپێدراو.\nبەکارهێنان: {today_used}/{daily_limit}.",
        "ckb": "📈 گەیشتیت بە سنووری ڕۆژانەی ڕێگەپێدراو.\nبەکارهێنانی ئێستا: {today_used}/{daily_limit}.",
    },
    "limit.block.monthly": {
        "ar": "📊 وصلت إلى الحد الشهري المسموح به.\nالاستخدام الحالي: {month_used}/{monthly_limit}.",
        "en": "📊 You've reached the monthly usage limit.\nCurrent usage: {month_used}/{monthly_limit}.",
        "ku": "📊 گەیشتیتە سنووری مانگانەی ڕێگەپێدراو.\nبەکارهێنان: {month_used}/{monthly_limit}.",
        "ckb": "📊 گەیشتیت بە سنووری مانگانەی ڕێگەپێدراو.\nبەکارهێنانی ئێستا: {month_used}/{monthly_limit}.",
    },
    "limit.block.both": {
        "ar": "📈 وصلت إلى الحد اليومي و الشهري معًا.\nاليومي: {today_used}/{daily_limit}\nالشهري: {month_used}/{monthly_limit}.",
        "en": "📈 You've exhausted both your daily and monthly limits.\nDaily: {today_used}/{daily_limit}\nMonthly: {month_used}/{monthly_limit}.",
        "ku": "📈 هەردوو سنووری ڕۆژانە و مانگانەت تەواو کرد.\nڕۆژانە: {today_used}/{daily_limit}\nمانگانە: {month_used}/{monthly_limit}.",
        "ckb": "📈 هەردوو سنووری ڕۆژانە و مانگانەت تەواو کرد.\nڕۆژانە: {today_used}/{daily_limit}\nمانگانە: {month_used}/{monthly_limit}.",
    },
    "limit.block.notice": {
        "ar": "تم إرسال طلب رفع الحد للإدارة وسيتم التواصل معك فور المراجعة.",
        "en": "We've notified the admins about raising your limit and will update you after they review it.",
        "ku": "داوای بەرزکردنەوە نێردرا بۆ بەڕێوبەران و دوای پشکنین ئاگادارت دەکەین.",
        "ckb": "داوای بەرزکردنەوە نێردرا بۆ بەڕێوبەران و دوای پشکنین ئاگادارت دەکەین.",
    },
    "limit.reason.daily": {
        "ar": "اليومي",
        "en": "daily",
        "ku": "ڕۆژانە",
        "ckb": "ڕۆژانە",
    },
    "limit.reason.monthly": {
        "ar": "الشهري",
        "en": "monthly",
        "ku": "مانگانە",
        "ckb": "مانگانە",
    },
    "limit.reason.both": {
        "ar": "اليومي والشهري",
        "en": "daily and monthly",
        "ku": "ڕۆژانە و مانگانە",
        "ckb": "ڕۆژانە و مانگانە",
    },
    "limit.request.user": {
        "ar": "✅ تم إرسال طلب رفع الحد للإدارة.\nالنوع: الحد {label}.\nسنقوم بمراجعة الطلب وإبلاغك فور حدوث أي تحديث.",
        "en": "✅ Your limit increase request was sent to the admins.\nType: {label} limit.\nWe'll review it and let you know once it changes.",
        "ku": "✅ داوای بەرزکردنەوەی سنوورەکەت نێردرا بۆ بەڕێوبەران.\nجۆر: سنووری {label}.\nپاش پشکنین ئاگاداریت دەکەینەوە کاتێک نوێکاری هەبێت.",
        "ckb": "✅ داواکاری بەرزکردنەوەی سنوور نێردرا بۆ بەڕێوبەران.\nجۆر: سنووری {label}.\nپاش پشکنین ئاگادارت دەکەین ئەگەر گۆڕانکاری هەبوو.",
    },
    "limit.request.admin": {
        "ar": "📈 <b>طلب رفع حد</b>\n• المستخدم: <b>{user_name}</b> ({contact})\n• اليومي: {today_used}/{daily_limit}\n• الشهري: {month_used}/{monthly_limit}\n• النوع: <b>{reason}</b>",
        "en": "📈 <b>Limit increase request</b>\n• User: <b>{user_name}</b> ({contact})\n• Daily: {today_used}/{daily_limit}\n• Monthly: {month_used}/{monthly_limit}\n• Type: <b>{reason}</b>",
        "ku": "📈 <b>داوای بەرزکردنەوەی سنوور</b>\n• بەکارهێنەر: <b>{user_name}</b> ({contact})\n• ڕۆژانە: {today_used}/{daily_limit}\n• مانگانە: {month_used}/{monthly_limit}\n• جۆر: <b>{reason}</b>",
        "ckb": "📈 <b>داوای بەرزکردنەوەی سنوور</b>\n• بەکارهێنەر: <b>{user_name}</b> ({contact})\n• ڕۆژانە: {today_used}/{daily_limit}\n• مانگانە: {month_used}/{monthly_limit}\n• جۆر: <b>{reason}</b>",
    },
    "vin.error": {
        "ar": "⚠️ حدث خطأ أثناء معالجة تقرير VIN. حاول لاحقاً.",
        "en": "⚠️ Something went wrong while processing the VIN report. Please try again later.",
        "ku": "⚠️ هەڵەیەک ڕوویدا لە کاتی چاککردنی ڕاپۆرتی VIN. تکایە دواتر هەوڵبدە.",
        "ckb": "⚠️ هەڵەیەک ڕوویدا لە کاتی پڕۆسەی ڕاپۆرتی VIN. تکایە دواتر هەوڵبدە.",
    },
    "menu.profile.label": {"ar": "👤 بياناتي", "en": "👤 My Info", "ku": "👤 زانیاریی من", "ckb": "👤 زانیاریی من"},
    "menu.profile.description": {
        "ar": "عرض بيانات حسابك ورسائلك السابقة.",
        "en": "View your account details and recent history.",
        "ku": "وردەکاری هەژمارەکەت و مێژووە دواهەمەکانی ببینە.",
        "ckb": "وردەکاری هەژمارەکەت و مێژووە دواهەمەکانی ببینە.",
    },
    "menu.activation.label": {"ar": "🛂 طلب تفعيل", "en": "🛂 Activation Request", "ku": "🛂 داوای چالاککردن", "ckb": "🛂 داوای چالاککردن"},
    "menu.activation.description": {
        "ar": "أرسل رقم هاتفك لوضعك في قائمة التفعيل.",
        "en": "Send your phone number to join the activation queue.",
        "ku": "ژمارەی مۆبایل بنێرە بۆ خستنە لیستی چالاککردن.",
        "ckb": "ژمارەی مۆبایل بنێرە بۆ خستنە لیستی چالاککردن.",
    },
    "menu.balance.label": {"ar": "💳 رصيدي", "en": "💳 My Balance", "ku": "💳 باڵانسم", "ckb": "💳 باڵانسم"},
    "menu.balance.description": {
        "ar": "اِطلع على الرصيد وحدود التقارير.",
        "en": "Check your remaining credits and limits.",
        "ku": "باڵانس و سنوورەکانت بپشکنە.",
        "ckb": "باڵانس و سنوورەکانت بپشکنە.",
    },
    "menu.report.label": {"ar": "📄 تقرير جديد", "en": "📄 New Report", "ku": "📄 ڕاپۆرتی نوێ", "ckb": "📄 ڕاپۆرتی نوێ"},
    "menu.report.description": {
        "ar": "إرشادات إرسال VIN للحصول على تقرير جديد.",
        "en": "Get instructions for submitting a VIN report.",
        "ku": "ڕێنمایی ناردنی VIN بۆ وەرگرتنی ڕاپۆرتی نوێ.",
        "ckb": "ڕێنمایی ناردنی VIN بۆ بەدەستهێنانی ڕاپۆرتی نوێ.",
    },
    "menu.help.label": {"ar": "🆘 المساعدة والتواصل", "en": "🆘 Help & Contact", "ku": "🆘 یارمەتیدان و پەیوەندی", "ckb": "🆘 یارمەتی و پەیوەندی"},
    "menu.help.description": {
        "ar": "طرق التواصل مع الدعم.",
        "en": "How to reach support.",
        "ku": "ڕێگەی پەیوەندی بە پشتگیری.",
        "ckb": "چۆن پەیوەندیمان پێوە بکەیت.",
    },
    "menu.language.label": {"ar": "🌐 لغة التقرير", "en": "🌐 Report Language", "ku": "🌐 زمانێکی ڕاپۆرت", "ckb": "🌐 زمانی ڕاپۆرت"},
    "menu.language.description": {
        "ar": "اختر لغة التقرير الافتراضية.",
        "en": "Pick your default report language.",
        "ku": "زمانی دەستنیشانکردنی ڕاپۆرت دیاری بکە.",
        "ckb": "زمانی ڕاپۆرتی بنەڕەتی دیاری بکە.",
    },
    "main_menu.hint": {
        "ar": "أرسل أي رسالة للعودة إلى القائمة الرئيسية.",
        "en": "Send any message to return to the main menu.",
        "ku": "هەر پەیامێک بنێرە بۆ گەڕانەوە بۆ لیستی سەرەکی.",
        "ckb": "هەر پەیامێک بنێرە بۆ گەڕانەوە بۆ لیستی سەرەکی.",
    },
    "menu.users.label": {"ar": "👥 المستخدمون", "en": "👥 Users", "ckb": "👥 بەکارهێنەران"},
    "menu.users.description": {
        "ar": "لوحة إدارة المستخدمين (Telegram).",
        "en": "Telegram-only user management panel.",
        "ckb": "پانێڵی بەڕێوەبردنی بەکارهێنەرانی Telegram.",
    },
    "menu.stats.label": {"ar": "📊 إحصائيات", "en": "📊 Stats", "ckb": "📊 ئامار"},
    "menu.stats.description": {
        "ar": "عرض الإحصائيات الحية (Telegram).",
        "en": "Live stats (Telegram only).",
        "ckb": "ئاماری ڕاستەوخۆ (تەنها Telegram).",
    },
    "menu.pending.label": {"ar": "📝 قائمة المنتظرين", "en": "📝 Waiting List", "ckb": "📝 لیستی چاوەڕوان"},
    "menu.pending.description": {
        "ar": "طلبات التفعيل المعلقة (Telegram).",
        "en": "Pending activation requests (Telegram).",
        "ckb": "داواکاریی چالاککردنی چاوەڕوان (Telegram).",
    },
    "menu.settings.label": {"ar": "⚙️ إعدادات النظام", "en": "⚙️ System Settings", "ckb": "⚙️ ڕێکخستنەکانی سیستەم"},
    "menu.settings.description": {
        "ar": "خيارات السوبر أدمن فقط.",
        "en": "Super admin options only.",
        "ckb": "تەنها بۆ سوپەر ئەدمینەکان.",
    },
    "menu.notifications.label": {"ar": "📢 إشعارات", "en": "📢 Notifications", "ckb": "📢 ئاگادارکردنەوەکان"},
    "menu.notifications.description": {
        "ar": "أرسل إشعارات جماعية (Telegram).",
        "en": "Broadcast notifications (Telegram).",
        "ckb": "ناردنی ئاگادارکردنەوەی گشتی (Telegram).",
    },
    "users.panel.header": {
        "ar": (
            "👥 <b>قائمة المستخدمين</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "كل صف يعرض: الهاتف • تاريخ الانتهاء • حالة المستخدم • حذف.\n"
            "اضغط على الزر المناسب لتنفيذ الإجراء المطلوب."
        ),
        "en": (
            "👥 <b>User list</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Each row shows: phone • expiry • user status • delete.\n"
            "Tap the right button to perform the action."
        ),
        "ku": (
            "👥 <b>لیستی بەکارهێنەرەکان</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "هەموو ریزێک: مۆبایل • بەسەرچوون • دۆخی بەکارهێنەر • سڕینەوە.\n"
            "دوگمەی گونجاو بەکاربەرە بۆ ئەو کردارە."
        ),
        "ckb": (
            "👥 <b>لیستی بەکارهێنەران</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "هەر ریزێک: ژمارەی مۆبایل • بەسەرچوون • دۆخی بەکارهێنەر • سڕینەوە.\n"
            "دوگمەی گونجاو بەکاربەرە بۆ ئەو کردارە."
        ),
    },
    "text.fallback.instructions": {
        "ar": "👋 أهلاً بك! أرسل /start لعرض القائمة أو أرسل رقم VIN مكوّن من 17 خانة للحصول على تقرير جديد.",
        "en": "👋 Hi! Send /start to open the menu or send a 17-character VIN to request a report.",
        "ku": "👋 سڵاو! /start بنێرە بۆ کردنەوەی لیست یان ژمارەی VIN ـی ١٧ پیت بنێرە بۆ داوای ڕاپۆرت.",
    },
    "help.body": {
        "ar": "🆘 المساعدة والتواصل\n\n🌐 الموقع: {site}\n✉️ البريد: {email}\n📱 واتساب الدعم: {support}",
        "en": "🆘 Help & Contact\n\n🌐 Website: {site}\n✉️ Email: {email}\n📱 WhatsApp Support: {support}",
        "ku": "🆘 یارمەتیدان و پەیوەندی\n\n🌐 وێبسایت: {site}\n✉️ ئیمەیل: {email}\n📱 واتساپ پشتگیری: {support}",
    },
    "start.keyboard.hint": {
        "ar": "استخدم أيقونة لوحة المفاتيح بجوار زر المرفقات",
        "en": "Use the keyboard button next to attachments",
        "ku": "دوگمەی تەختەکلیلەکە بەکاربەرە لەگەڵ هەڵگرتنەکان",
    },
    "start.greeting": {
        "ar": "👋 <b>أهلاً بك {name}!</b>",
        "en": "👋 <b>Welcome {name}!</b>",
        "ku": "👋 <b>بەخێربێیت {name}!</b>",
    },
    "start.status.header": {
        "ar": "━━━━━━━━━━━━━━━━━━━━\n<b>📊 حالتك الحالية:</b>",
        "en": "━━━━━━━━━━━━━━━━━━━━\n<b>📊 Your current status:</b>",
        "ku": "━━━━━━━━━━━━━━━━━━━━\n<b>📊 دۆخی ئێستات:</b>",
    },
    "start.status.line": {
        "ar": "• الحالة: {status}",
        "en": "• Status: {status}",
        "ku": "• دۆخ: {status}",
    },
    "start.balance.line": {
        "ar": "• 💳 التقارير هذا الشهر: <b>{credit}</b>",
        "en": "• 💳 Reports this month: <b>{credit}</b>",
        "ku": "• 💳 ڕاپۆرتەکانی ئەم مانگە: <b>{credit}</b>",
    },
    "start.days_left": {
        "ar": "• ⏰ باقي: <b>{days}</b> يوم",
        "en": "• ⏰ Days left: <b>{days}</b>",
        "ku": "• ⏰ کات: <b>{days}</b> ڕۆژ ماوە",
    },
    "start.ends_today": {
        "ar": "• ⚠️ الاشتراك ينتهي اليوم",
        "en": "• ⚠️ Subscription ends today",
        "ku": "• ⚠️ ئەمڕۆ بەسەر دەچێت",
    },
    "start.options.header": {
        "ar": "\n━━━━━━━━━━━━━━━━━━━━\n<b>🚀 خياراتك المتاحة:</b>",
        "en": "\n━━━━━━━━━━━━━━━━━━━━\n<b>🚀 Your available options:</b>",
        "ku": "\n━━━━━━━━━━━━━━━━━━━━\n<b>🚀 هەڵبژاردەکانت:</b>",
    },
    "start.options.list": {
        "ar": "• {report}\n• {profile}\n• {balance}\n• {activation}\n• {help}\n• {language}",
        "en": "• {report}\n• {profile}\n• {balance}\n• {activation}\n• {help}\n• {language}",
        "ku": "• {report}\n• {profile}\n• {balance}\n• {activation}\n• {help}\n• {language}",
    },
    "start.admin.header": {
        "ar": "\n━━━━━━━━━━━━━━━━━━━━\n<b>👑 أدوات الإدارة:</b>",
        "en": "\n━━━━━━━━━━━━━━━━━━━━\n<b>👑 Admin tools:</b>",
        "ku": "\n━━━━━━━━━━━━━━━━━━━━\n<b>👑 ئامرازەکانی بەڕێوبەر:</b>",
    },
    "start.admin.list": {
        "ar": "• {users}\n• {stats}\n• {pending}\n{settings}",
        "en": "• {users}\n• {stats}\n• {pending}\n{settings}",
        "ku": "• {users}\n• {stats}\n• {pending}\n{settings}",
    },
    "start.admin.settings": {
        "ar": "• {settings}",
        "en": "• {settings}",
        "ku": "• {settings}",
    },
    "start.footer.telegram": {
        "ar": "💡 <i>استخدم الأزرار أدناه للتنقل</i>",
        "en": "💡 <i>Use the buttons below to navigate</i>",
        "ku": "💡 <i>دوگمەکان خوارەوە بەکارهێنە بۆ گەشتکردن</i>",
    },
    "start.footer.other": {
        "ar": "💡 <i>أرسل الخيار المطلوب مثل كلمة 'تقرير' أو 'رصيدي' للمتابعة</i>",
        "en": "💡 <i>Send an option like 'report' or 'balance' to continue</i>",
        "ku": "💡 <i>هەڵبژاردەیەک وەکو 'ڕاپۆرت' یان 'باڵانس' بنێرە بۆ بەردەوامبوون</i>",
    },
    "progress.vin.monthly.unlimited": {
        "ar": "💳 الرصيد: <b>غير محدود</b>",
        "en": "💳 Credit: <b>Unlimited</b>",
        "ku": "💳 کریدت: <b>بێ سنوور</b>",
    },
    "progress.vin.monthly.remaining": {
        "ar": "💳 الرصيد المتبقي: <b>{remaining}</b>/<b>{limit}</b>",
        "en": "💳 Remaining credit: <b>{remaining}</b>/<b>{limit}</b>",
        "ku": "💳 کریدتی ماوە: <b>{remaining}</b>/<b>{limit}</b>",
    },
    "progress.vin.daily.unlimited": {
        "ar": "📈 الاستخدام اليومي: <b>{used}</b>/<b>غير محدود</b>",
        "en": "📈 Daily usage: <b>{used}</b>/<b>Unlimited</b>",
        "ku": "📈 بەکارهێنانی ڕۆژانە: <b>{used}</b>/<b>بێ سنوور</b>",
    },
    "progress.vin.daily.remaining": {
        "ar": "📈 الاستخدام اليومي: <b>{used}</b>/<b>{limit}</b>",
        "en": "📈 Daily usage: <b>{used}</b>/<b>{limit}</b>",
        "ku": "📈 بەکارهێنانی ڕۆژانە: <b>{used}</b>/<b>{limit}</b>",
    },
    "progress.vin.days_left": {
        "ar": " — الانتهاء بعد <b>{days}</b> يوم",
        "en": " — expires in <b>{days}</b> days",
        "ku": " — دەکۆتێت لە <b>{days}</b> ڕۆژدا",
    },
    "progress.vin.days_left.today": {
        "ar": " — ينتهي اليوم",
        "en": " — expires today",
        "ku": " — ئەمڕۆ دەکۆتێت",
    },
    "progress.vin.days_left.expired": {
        "ar": " — الاشتراك منتهٍ",
        "en": " — subscription expired",
        "ku": " — بەسەرچووە",
    },
    "progress.vin.title": {
        "ar": "⏳ جاري جلب تقرير VIN:\n<code>{vin}</code>",
        "en": "⏳ Fetching VIN report:\n<code>{vin}</code>",
        "ku": "⏳ ڕاپۆرتی VIN دەهێنرێت:\n<code>{vin}</code>",
    },
    "progress.vin.body": {
        "ar": "{monthly_line}{days_line}\n{daily_line}",
        "en": "{monthly_line}{days_line}\n{daily_line}",
        "ku": "{monthly_line}{days_line}\n{daily_line}",
    },
    "account.header": {
        "ar": "👤 معلومات حسابك",
        "en": "👤 Your Account",
        "ku": "👤 زانیاری هەژمارەکەت",
    },
    "account.section.basic": {
        "ar": "🆔 المعلومات الأساسية:",
        "en": "🆔 Basic Info:",
        "ku": "🆔 زانیاری بنەڕەتی:",
    },
    "account.section.status": {
        "ar": "📊 حالة الحساب:",
        "en": "📊 Account Status:",
        "ku": "📊 دۆخی هەژمار:",
    },
    "account.section.services": {
        "ar": "🔧 الخدمات المتاحة:",
        "en": "🔧 Available Services:",
        "ku": "🔧 خزمەتگوزاریە بەردەستەکان:",
    },
    "account.section.limits": {
        "ar": "📈 الحدود والاستخدام:",
        "en": "📈 Limits & Usage:",
        "ku": "📈 سنوور و بەکارهێنان:",
    },
    "account.field.name": {
        "ar": "• الاسم: {value}",
        "en": "• Name: {value}",
        "ku": "• ناو: {value}",
    },
    "account.field.id": {
        "ar": "• المعرّف: {value}",
        "en": "• ID: {value}",
        "ku": "• ناسنامە: {value}",
    },
    "account.field.username": {
        "ar": "• Username: {value}",
        "en": "• Username: {value}",
        "ku": "• یوزەرنێم: {value}",
    },
    "account.field.status": {
        "ar": "• الحالة: {value}",
        "en": "• Status: {value}",
        "ku": "• دۆخ: {value}",
    },
    "account.field.phone": {
        "ar": "• الهاتف: {value}",
        "en": "• Phone: {value}",
        "ku": "• مۆبایل: {value}",
    },
    "account.status.active": {
        "ar": "✅ مفعّل",
        "en": "✅ Active",
        "ku": "✅ چالاک",
    },
    "account.status.inactive": {
        "ar": "⛔ غير مفعّل",
        "en": "⛔ Inactive",
        "ku": "⛔ ناچالاک",
    },
    "account.status.expired": {
        "ar": "⚠️ الاشتراك منتهي",
        "en": "⚠️ Subscription expired",
        "ku": "⚠️ بەسەرچووە",
    },
    "account.field.monthly_remaining": {
        "ar": "• التقارير المتبقية هذا الشهر: 💳 {value}",
        "en": "• Reports left this month: 💳 {value}",
        "ku": "• ڕاپۆرتی ماوەی ئەم مانگە: 💳 {value}",
    },
    "account.field.activation_date": {
        "ar": "• تاريخ التفعيل: {value}",
        "en": "• Activation date: {value}",
        "ku": "• بەرواری چالاککردن: {value}",
    },
    "account.field.expiry_date": {
        "ar": "• تاريخ الانتهاء: {value}",
        "en": "• Expiry date: {value}",
        "ku": "• بەرواری بەسەرچوون: {value}",
    },
    "account.field.service.carfax": {
        "ar": "• Carfax: {value}",
        "en": "• Carfax: {value}",
        "ku": "• Carfax: {value}",
    },
    "account.field.service.photos": {
        "ar": "• Photos: {value}",
        "en": "• Photos: {value}",
        "ku": "• وێنەکان: {value}",
    },
    "account.field.daily": {
        "ar": "• اليوم: {value}",
        "en": "• Today: {value}",
        "ku": "• ئه‌مڕۆ: {value}",
    },
    "account.field.monthly_limit": {
        "ar": "• الشهر: {value}",
        "en": "• Month: {value}",
        "ku": "• مانگ: {value}",
    },
    "main_menu.hint": {
        "ar": "للعودة إلى القائمة الرئيسية أرسل أي رسالة.",
        "en": "Send any message to return to the main menu.",
        "ku": "هەر پەیامێک بنێرە بۆ گەڕانەوە بۆ لیستی سەرەکی.",
    },
    "language.prompt": {
        "ar": "🌐 تغيير لغة التقارير\nاللغة الحالية: {current}\nاختر اللغة الجديدة بإرسال الرقم:\n1️⃣ العربية\n2️⃣ English\n3️⃣ كردي باديني\n4️⃣ كردي سوراني",
        "en": "🌐 Change report language\nCurrent language: {current}\nPick a new language by sending its number:\n1️⃣ Arabic\n2️⃣ English\n3️⃣ Kurdish Badini\n4️⃣ Kurdish Sorani",
        "ku": "🌐 گۆڕینی زمانی ڕاپۆرت\nزمانی ئێستا: {current}\nزمانێکی نوێ بە ژمارەیەکەی بنێرە:\n1️⃣ عەرەبی\n2️⃣ ئینگلیزی\n3️⃣ کوردی بادینی\n4️⃣ کوردی سۆرانی",
        "ckb": "🌐 گۆڕینی زمانی ڕاپۆرت\nزمانی ئێستا: {current}\nزمانێکی نوێ بە ژمارەیەکەی بنێرە:\n1️⃣ عەرەبی\n2️⃣ ئینگلیزی\n3️⃣ کوردی بادینی\n4️⃣ کوردی سۆرانی",
    },
    "language.panel": {
        "ar": "🌐 تغيير لغة التقرير\n\nاللغة الحالية: {label}\nاختر اللغة الجديدة من الأزرار.",
        "en": "🌐 Change report language\n\nCurrent language: {label}\nPick a new language from the buttons.",
        "ku": "🌐 زمانێکی ڕاپۆرت بگۆڕە\n\nزمانی ئێستا: {label}\nلە دوگمەکانەوە زمان هەڵبژێرە.",
        "ckb": "🌐 گۆڕینی زمانی ڕاپۆرت\n\nزمانی ئێستا: {label}\nلە دوگمەکانەوە زمان هەڵبژێرە.",
    },
    "language.choice_invalid": {
        "ar": "⚠️ الرجاء اختيار 1 أو 2 أو 3 أو 4 لاختيار اللغة.",
        "en": "⚠️ Please choose 1, 2, 3 or 4 to pick a language.",
        "ku": "⚠️ تکایە ١ یان ٢ یان ٣ یان ٤ هەڵبژێرە بۆ دیاریکردنی زمان.",
        "ckb": "⚠️ تکایە ١ یان ٢ یان ٣ یان ٤ هەڵبژێرە بۆ دیاریکردنی زمان.",
    },
    "language.changed": {
        "ar": "✅ تم تغيير لغة النظام والتقارير إلى {label}. أرسل أي رسالة لعرض القائمة الرئيسية.",
        "en": "✅ System and report language changed to {label}. Send any message to open the main menu.",
        "ku": "✅ زمانی سیستەم و ڕاپۆرت گۆڕدرا بۆ {label}. هەر پەیامێک بنێرە بۆ کردنەوەی لیستی سەرەکی.",
        "ckb": "✅ زمانی سیستەم و ڕاپۆرت گۆڕدرا بۆ {label}. هەر پەیامێک بنێرە بۆ کردنەوەی لیستی سەرەکی.",
    },
    "balance.title": {
        "ar": "💳 الرصيد",
        "en": "💳 Balance",
        "ku": "💳 باڵانس",
    },
    "balance.daily": {
        "ar": "اليوم: {today}/{daily}",
        "en": "Today: {today}/{daily}",
        "ku": "ئەمڕۆ: {today}/{daily}",
    },
    "balance.monthly": {
        "ar": "الشهر: {remaining}/{monthly}",
        "en": "Month: {remaining}/{monthly}",
        "ku": "مانگ: {remaining}/{monthly}",
    },
    "balance.remaining": {
        "ar": "التقارير المتبقية: {remaining}",
        "en": "Reports left: {remaining}",
        "ku": "ڕاپۆرتی ماوە: {remaining}",
    },
    "balance.unlimited": {
        "ar": "التقارير المتبقية: غير محدود",
        "en": "Reports left: Unlimited",
        "ku": "ڕاپۆرتی ماوە: بێ سنوور",
    },
    "pending.list.title": {
        "ar": "📝 قائمة طلبات التفعيل:",
        "en": "📝 Activation requests list:",
        "ku": "📝 لیستی داوای چالاککردن:",
    },
    "notifications.panel": {
        "ar": "📢 <b>نظام الإشعارات</b>\n\n━━━━━━━━━━━━━━━━━━━━\nاختر نوع الإشعار:",
        "en": "📢 <b>Notifications system</b>\n\n━━━━━━━━━━━━━━━━━━━━\nChoose the notification type:",
        "ku": "📢 <b>سیستەمی ئاگادارکردنەوە</b>\n\n━━━━━━━━━━━━━━━━━━━━\nجۆری ئاگادارکردنەوە هەڵبژێرە:",
    },
    "notifications.buttons.all": {
        "ar": "📢 للجميع",
        "en": "📢 To all",
        "ku": "📢 بۆ هەموو",
    },
    "notifications.buttons.active": {
        "ar": "✅ للمفعّلين",
        "en": "✅ Active users",
        "ku": "✅ چالاکەکان",
    },
    "notifications.buttons.inactive": {
        "ar": "⛔ للمعطّلين",
        "en": "⛔ Inactive users",
        "ku": "⛔ ناچالاکەکان",
    },
    "notifications.buttons.select": {
        "ar": "👥 اختيار مستخدمين",
        "en": "👥 Select users",
        "ku": "👥 هەڵبژاردنی بەکارهێنەران",
    },
    "balance.expiring_in": {
        "ar": "باقي على انتهاء الاشتراك: {days} يوم",
        "en": "Days until expiry: {days}",
        "ku": "ڕۆژ ماوە بۆ بەسەرچوون: {days}",
    },
    "balance.expires_today": {
        "ar": "⚠️ الاشتراك ينتهي اليوم",
        "en": "⚠️ Subscription ends today",
        "ku": "⚠️ ئەمڕۆ بەسەر دەچێت",
    },
    "balance.expired": {
        "ar": "⛔ الاشتراك منتهي",
        "en": "⛔ Subscription expired",
        "ku": "⛔ بەسەرچووە",
    },
    "balance.deduction": {
        "ar": "كل تقرير VIN يخصم 1 من رصيدك الشهري.",
        "en": "Each VIN report deducts 1 from your monthly balance.",
        "ku": "هەر ڕاپۆرتێک ١ لە باڵانسی مانگانە دەکەمێنێت.",
    },
    "report.limit_line": {
        "ar": "التقارير المتبقية هذا الشهر: {value}",
        "en": "Reports left this month: {value}",
        "ku": "ڕاپۆرتی ماوەی ئەم مانگە: {value}",
    },
    "report.limit_unlimited": {
        "ar": "الرصيد: غير محدود",
        "en": "Balance: Unlimited",
        "ku": "باڵانس: بێ سنوور",
    },
    "report.instructions": {
        "ar": "📄 طلب تقرير جديد\n\n1) أرسل رقم الشاصي (VIN) المكون من 17 خانة.\n2) اكتب الحروف الإنجليزية فقط بدون مسافات أو شرطات.\n3) مثال: 1HGCM82633A123456\n\n{limit_line}\n💡 لكل تقرير يتم خصم 1 من رصيدك الشهري.",
        "en": "📄 Request a new report\n\n1) Send the 17-character VIN.\n2) Use English letters only, no spaces or dashes.\n3) Example: 1HGCM82633A123456\n\n{limit_line}\n💡 Each report deducts 1 from your monthly balance.",
        "ku": "📄 داوای ڕاپۆرتی نوێ بکە\n\n1) ژمارەی VIN ـی ١٧ پیت بنێرە.\n2) تەنها پیتە ئینگلیزییەکان بەکاربەرە، بێ بۆشایی یان هێڵە.\n3) نمونە: 1HGCM82633A123456\n\n{limit_line}\n💡 هەر ڕاپۆرتێک ١ لە باڵانسی مانگانە دەکەمێنێت.",
    },
    "account.inactive.expired": {
        "ar": "⛔ تم تعليق اشتراكك. انتهت صلاحيته بتاريخ {expiry}. تواصل مع الإدارة لتجديده ثم أعد المحاولة.",
        "en": "⛔ Your subscription is suspended. It expired on {expiry}. Please contact support to renew and try again.",
        "ku": "⛔ بەشداریکەت ناچالاک کراوە. لە {expiry} بەسەرچووە. تکایە پەیوەندی بە یارمەتیدان بکە بۆ نوێکردنەوە.",
    },
    "account.inactive": {
        "ar": "⛔ حسابك غير مفعّل حالياً. راسل الإدارة لإتمام التفعيل ثم أعد المحاولة.",
        "en": "⛔ Your account is inactive. Contact support to activate it, then try again.",
        "ku": "⛔ هەژمارەکەت ناچالاکە. پەیوەندی بە پشتگیری بکە بۆ چالاککردن، دووبارە هەوڵبدە.",
    },
    "service.carfax.disabled": {
        "ar": "🚫 خدمة Carfax غير مفعّلة لحسابك. تواصل مع الإدارة لتفعيلها.",
        "en": "🚫 Carfax service is disabled for your account. Please contact support to enable it.",
        "ku": "🚫 خزمەتگوزاری Carfax بۆ هەژمارەکەت ناچالاکە. تکایە پەیوەندی بکە بۆ چالاککردن.",
    },
    # WhatsApp flows
    "wa.broadcast.prompt": {
        "ar": "📢 *نظام الإشعارات*\n\nلمن تريد إرسال الإشعار؟",
        "en": "📢 *Notifications*\n\nWho should receive the notification?",
        "ku": "📢 *سیستەمی ئاگادارکردنەوە*\n\nبۆ کێ دەتەوێت ئاگاداریکردنەوە بنێریت؟",
    },
    "wa.broadcast.button.all": {
        "ar": "📢 للجميع",
        "en": "📢 To everyone",
        "ku": "📢 بۆ هەمووان",
    },
    "wa.broadcast.button.user": {
        "ar": "👤 لمستخدم محدد",
        "en": "👤 Specific user",
        "ku": "👤 بۆ بەکارهێنەرێکی دیاریکراو",
    },
    "wa.broadcast.button.cancel": {
        "ar": "❌ إلغاء",
        "en": "❌ Cancel",
        "ku": "❌ هەڵوەشاندنەوە",
    },
    "wa.photos.prompt": {
        "ar": "📸 اختر نوع الصور:\n1️⃣ صور حادث سابق\n2️⃣ صور السيارة المخفية",
        "en": "📸 Choose photo type:\n1️⃣ Accident images\n2️⃣ Hidden car photos",
        "ku": "📸 جۆری وێنە هەڵبژێرە:\n1️⃣ وێنەکانی ڕووداو\n2️⃣ وێنەکانی ئۆتۆموبیڵی شاردراو",
        "ckb": "📸 جۆری وێنە هەڵبژێرە:\n1️⃣ وێنەکانی ڕووداوی پێشووتر\n2️⃣ وێنەکانی ئۆتۆمبێلی شاردراو",
    },
    "wa.photos.option.accident": {
        "ar": "1. صور حادث سابق 💥",
        "en": "1. Accident photos 💥",
        "ku": "1. وێنەی ڕووداو 💥",
        "ckb": "1. وێنەکانی ڕووداوی پێشووتر 💥",
    },
    "wa.photos.option.hidden": {
        "ar": "2. صور السيارة المخفية 📷",
        "en": "2. Hidden car photos 📷",
        "ku": "2. وێنەی ئۆتۆموبیڵی شاردراو 📷",
        "ckb": "2. وێنەکانی ئۆتۆمبێلی شاردراو 📷",
    },
    "wa.progress.processing": {
        "ar": "🔍 *جاري معالجة الطلب...*",
        "en": "🔍 *Processing your request...*",
        "ku": "🔍 *داواکاریەکەت لە چارەسازیدایە...*",
        "ckb": "🔍 *داواکاریەکەت لە پڕۆسەدایە...*",
    },
    "wa.progress.vin": {
        "ar": "🚗 *رقم الشاصي:* `{vin}`",
        "en": "🚗 *VIN:* `{vin}`",
        "ku": "🚗 *ژمارەی شاصی:* `{vin}`",
        "ckb": "🚗 *ژمارەی شاسی:* `{vin}`",
    },
    "wa.progress.balance": {
        "ar": "💳 *الرصيد:* {balance}",
        "en": "💳 *Balance:* {balance}",
        "ku": "💳 *باڵانس:* {balance}",
        "ckb": "💳 *باڵانس:* {balance}",
    },
    "wa.progress.expiry.remaining": {
        "ar": " - الانتهاء بعد {days} يوم",
        "en": " - expires in {days} day(s)",
        "ku": " - دەکاتەوە لە {days} ڕۆژدا",
        "ckb": " - دەکۆتایەوە لە {days} ڕۆژدا",
    },
    "wa.progress.expiry.today": {
        "ar": " - ينتهي اليوم",
        "en": " - expires today",
        "ku": " - ئەمڕۆ دەکاتەوە",
        "ckb": " - ئەمڕۆ دەکۆتایەوە",
    },
    "wa.progress.expiry.expired": {
        "ar": " - منتهي",
        "en": " - expired",
        "ku": " - بەسەرچووە",
        "ckb": " - بەسەرچووە",
    },
    "wa.progress.wait": {
        "ar": "⏳ *يرجى الانتظار، جاري جلب التقرير...*",
        "en": "⏳ *Please wait, fetching the report...*",
        "ku": "⏳ *چاوەڕێ بکە، ڕاپۆرتەکە دەهێنرێت...*",
        "ckb": "⏳ *چاوەڕێ بکە، ڕاپۆرتەکە دەهێنرێت...*",
    },
    "wa.photos.fetching": {
        "ar": "📸 جاري جلب الصور لـ VIN: {vin}",
        "en": "📸 Fetching photos for VIN: {vin}",
        "ku": "📸 وێنەکان بۆ VIN: {vin} دەهێنرێن", 
        "ckb": "📸 وێنەکان بۆ VIN: {vin} دەهێنرێن",
    },
    "wa.photos.sent_count": {
        "ar": "✅ تم إرسال {count} صورة.",
        "en": "✅ Sent {count} image(s).",
        "ku": "✅ {count} وێنە نێردرا.",
        "ckb": "✅ {count} وێنە نێردرا.",
    },
    "wa.language.updated": {
        "ar": "✅ تم تغيير لغة التقارير إلى العربية.",
        "en": "✅ Report language set to English.",
        "ku": "✅ زمانێ ڕاپۆرت کرا بە کوردی بادینی.",
        "ckb": "✅ زمانێ ڕاپۆرت کرا بە کوردی سۆرانی.",
    },
    "wa.language.invalid_choice": {
        "ar": "⚠️ خيار غير صالح. أرسل 1 أو 2 أو 3 أو 4.",
        "en": "⚠️ Invalid choice. Send 1, 2, 3, or 4.",
        "ku": "⚠️ هەڵبژاردە نادروستە. 1 یان 2 یان 3 یان 4 بنێرە.",
        "ckb": "⚠️ هەڵبژاردە نادروستە. 1 یان 2 یان 3 یان 4 بنێرە.",
    },
    "wa.photos.none.accident": {
        "ar": "⚠️ لا توجد صور حادث متاحة لهذا رقم الشاصي.",
        "en": "⚠️ No accident images available for this VIN.",
        "ku": "⚠️ وێنەی ڕووداو بۆ ئەم ژمارەی شاصیە بوونی نییە.",
        "ckb": "⚠️ هیچ وێنەی ڕووداو بۆ ئەم ژمارەی شاسیە نییە.",
    },
    "wa.photos.none.generic": {
        "ar": "⚠️ لا توجد صور متاحة لهذا الرقم.",
        "en": "⚠️ No images available for this VIN.",
        "ku": "⚠️ هیچ وێنەیەک بوونی نییە بۆ ئەم شاصیە.",
        "ckb": "⚠️ هیچ وێنەیەک بۆ ئەم شاسیە نییە.",
    },
    "wa.photos.fetch_error.accident": {
        "ar": "⚠️ حدث خطأ أثناء جلب صور الحادث.",
        "en": "⚠️ Error while fetching accident images.",
        "ku": "⚠️ هەڵە ڕوویدا لە هێنانى وێنەکانی ڕووداو.",
        "ckb": "⚠️ هەڵە ڕوویدا لە هێنانی وێنەکانی ڕووداو.",
    },
    "wa.photos.fetch_error.generic": {
        "ar": "⚠️ حدث خطأ أثناء جلب الصور.",
        "en": "⚠️ Error while fetching images.",
        "ku": "⚠️ هەڵە ڕوویدا لە هێنانى وێنەکان.",
        "ckb": "⚠️ هەڵە ڕوویدا لە هێنانی وێنەکان.",
    },
    "wa.photos.send_error": {
        "ar": "⚠️ تعذر إرسال الصور حالياً.",
        "en": "⚠️ Could not send images right now.",
        "ku": "⚠️ نەتوانرا ئێستا وێنەکان بنێردرێن.",
        "ckb": "⚠️ نەتوانرا ئێستا وێنەکان بنێردرێن.",
    },
    "wa.footer.brand": {
        "ar": "خدمات بوت كارفاكس",
        "en": "Carfax Bot Services",
        "ku": "خزمەتگوزاریی بۆتی کارفاکس",
        "ckb": "خزمەتگوزاریی بۆتی کارفاکس",
    },
}

# Auto-extend Sorani (ckb) entries using Badini (ku) text when missing so every
# key resolves for Sorani without falling back to English/Arabic.
for _k, _vals in TRANSLATIONS.items():
    if "ckb" not in _vals and "ku" in _vals:
        _vals["ckb"] = _vals["ku"]


LANG_DIR = {"ar": "rtl", "ku": "rtl", "ckb": "rtl", "en": "ltr"}


def t(key: str, language: Optional[str], *, preserve_latin: bool = False, **kwargs: Any) -> str:
    """Strict translation resolver: never fall back إلى لغة أخرى.

    - يستخدم اللغة المطلوبة فقط.
    - إذا لم توجد ترجمة للمفتاح، يعاد المفتاح نفسه (أفضل من خلط لغات).
    - يضمن ألا يحدث fallback صامت للعربية/الإنجليزية عند غياب الترجمة.
    """

    lang = (language or "ar").strip().lower()
    templates = TRANSLATIONS.get(key)
    if not templates:
        template = key
    else:
        template = templates.get(lang) or templates.get("ckb") or templates.get("ku") or key
    try:
        rendered = template.format(**kwargs)
    except Exception:
        rendered = template

    if lang in KURDISH_LANGS and not preserve_latin:
        rendered = _ku_to_arabic(rendered)
    return rendered


def normalize_language(lang: Optional[str]) -> str:
    candidate = (lang or "").strip().lower()
    if candidate in ("ar", "en", "ku", "ckb"):
        return candidate
    default_candidate = (get_report_default_lang() or "ar").strip().lower()
    return default_candidate if default_candidate in ("ar", "en", "ku", "ckb") else "ar"


def _limit_reason_label(language: Optional[str], reason: Optional[str]) -> str:
    mapping = {
        "daily": "limit.reason.daily",
        "monthly": "limit.reason.monthly",
        "both": "limit.reason.both",
    }
    key = mapping.get(reason or "")
    return t(key or "limit.reason.monthly", language)


def _language_label(code: str) -> str:
    mapping = {"ar": "العربية", "en": "English", "ku": "کوردی بادینی", "ckb": "کوردی سۆرانی"}
    return mapping.get(code, code)


def _persist_user_language(user_id: str, lang: str) -> None:
    db = load_db()
    db_user = ensure_user(db, user_id, None)
    db_user["language"] = lang
    db_user["report_lang"] = lang
    save_db(db)


def _persist_user_state(user_id: str, state: Optional[str]) -> None:
    db = load_db()
    db_user = ensure_user(db, user_id, None)
    if state:
        db_user["state"] = state
    else:
        db_user.pop("state", None)
    save_db(db)


async def handle_text(
    user: UserContext,
    message: IncomingMessage,
    *,
    context: Any = None,
    skip_limit_validation: bool = False,
    deduct_credit: bool = True,
    pre_reserved_credit: bool = False,
) -> BridgeResponse:
    """Process text from any platform and return a structured response."""

    text = (message.text or "").strip()
    if not text:
        return await render_main_menu(user)

    # Global shortcuts to return to the main menu
    lowered_dot = text.strip().lower()
    if lowered_dot in {".", "0", "menu", "main menu", "القائمة", "القائمه"}:
        _persist_user_state(user.user_id, None)
        resp = await render_main_menu(user)
        resp.actions["clear_state"] = True
        return resp

    # Handle language choice state
    if (user.state or "").lower() == "language_choice":
        resp = await _handle_language_choice(user, text)
        return await _localize_response(resp, user.language)

    expects_activation = (user.state or "").lower() == "activation_phone"
    if expects_activation:
        cc = _extract_pending_country_code(user)
        normalized_phone = _normalize_phone(text, cc)
        if normalized_phone:
            resp = await _handle_activation_submission(user, message, normalized_phone, context)
            return await _localize_response(resp, user.language)
        warn = _activation_invalid_message(user.language, cc)
        resp = BridgeResponse()
        resp.messages.append(warn)
        return await _localize_response(resp, user.language)

    lowered = text.lower()
    if lowered.startswith("/start") or lowered == "start":
        resp = await _handle_start_flow(user, message)
        return await _localize_response(resp, user.language)

    vin_candidate = _extract_vin_candidate(text)
    if not vin_candidate:
        sanitized = _sanitize_for_vin(text)
        if len(sanitized) == 17 and VIN_RE.match(sanitized):
            vin_candidate = sanitized
    if vin_candidate:
        resp = await _handle_vin_request(
            user,
            message,
            vin_candidate,
            context=context,
            skip_limit_validation=skip_limit_validation,
            deduct_credit=deduct_credit,
            pre_reserved_credit=pre_reserved_credit,
        )
        return await _localize_response(resp, user.language)

    if _looks_like_vin(text) or _looks_like_vin(_sanitize_for_vin(text)):
        resp = BridgeResponse()
        resp.messages.append(t("vin.invalid", user.language))
        return await _localize_response(resp, user.language)

    phone_candidate = _extract_general_phone_candidate(user, text)
    if phone_candidate:
        resp = await _handle_activation_submission(user, message, phone_candidate, context)
        return await _localize_response(resp, user.language)

    # Check for capabilities question
    capabilities_patterns = [
        "ماذا يمكن", "ماذا تستطيع", "ماذا يمكنك", "what can", "what do", "چی دەکرێت"
    ]
    if any(pattern in lowered for pattern in capabilities_patterns):
        resp = BridgeResponse()
        resp.messages.append(t("help.capabilities", user.language))
        return await _localize_response(resp, user.language)

    return await render_main_menu(user)


async def handle_photo(
    user: UserContext,
    message: IncomingMessage,
    *,
    media_fetcher: Optional[
        Callable[[IncomingMessage, Optional[str], Optional[str]], Awaitable[Tuple[Optional[bytes], Optional[str], Optional[str]]]]
    ] = None,
) -> BridgeResponse:
    """Process inbound media and record it for follow-up across platforms."""

    resp = BridgeResponse()
    # Normalize and persist user language up-front to avoid drift across sessions/platforms.
    language = normalize_language(user.language)
    user.language = language
    try:
        if user.metadata is not None:
            user.metadata.setdefault("language", language)
    except Exception:
        pass
    try:
        _persist_user_language(user.user_id, language)
    except Exception:
        LOGGER.debug("Failed to persist user language", exc_info=True)

    source = (message.media_url or "").strip()
    if not source:
        resp.messages.append(t("media.not_found", user.language))
        return await _localize_response(resp, user.language)

    filename = _infer_media_filename(message)
    mime_type = (message.mime_type or _guess_mime_from_name(filename))
    caption = (message.text or message.caption or "").strip()

    content_bytes: Optional[bytes] = None
    resolved_name: Optional[str] = filename
    resolved_mime: Optional[str] = mime_type

    if media_fetcher:
        try:
            fetched = await media_fetcher(message, filename, mime_type)
        except TypeError:
            fetched = await media_fetcher(message, filename)  # pragma: no cover - backward compat
        if isinstance(fetched, tuple):
            if len(fetched) == 3:
                content_bytes, resolved_name, resolved_mime = fetched
            elif len(fetched) == 2:
                content_bytes, resolved_name = fetched

    if content_bytes is None and source.lower().startswith(("http://", "https://")):
        content_bytes, resolved_mime = await _download_remote_media(source, resolved_mime)

    stored_path: Optional[str] = None
    if content_bytes:
        stored_path = _persist_incoming_media(user.user_id, resolved_name, content_bytes)

    entry = {
        "id": f"media-{int(time.time() * 1000)}",
        "ts": now_str(),
        "platform": message.platform,
        "source": source,
        "caption": caption,
        "filename": resolved_name,
        "mime": resolved_mime,
        "path": stored_path,
    }
    stored_entry = _record_media_entry(user, entry)

    resp.messages.append(_compose_media_ack(user, stored_entry))
    resp.actions["media_upload"] = stored_entry
    if stored_path:
        resp.actions.setdefault("stored_media_paths", []).append(stored_path)
    return await _localize_response(resp, user.language)


async def _handle_language_choice(user: UserContext, selection: str) -> BridgeResponse:
    normalized = (selection or "").strip().lower()
    mapping = {
        "1": "ar",
        "2": "en",
        "3": "ku",
        "4": "ckb",
        "arabic": "ar",
        "عربي": "ar",
        "العربية": "ar",
        "english": "en",
        "en": "en",
        "inglizî": "en",
        "kurdish": "ku",
        "ku": "ku",
        "کوردی": "ku",
        "sorani": "ckb",
        "soranî": "ckb",
        "سۆرانی": "ckb",
    }
    lang_code = mapping.get(normalized)

    resp = BridgeResponse()

    if not lang_code:
        resp.messages.append(t("language.choice_invalid", user.language))
        resp.messages.append(_compose_language_prompt(user.language))
        resp.actions["await_language_choice"] = True
        return resp

    _persist_user_language(user.user_id, lang_code)
    _persist_user_state(user.user_id, None)
    user.language = lang_code
    resp.actions["clear_state"] = True
    resp.actions["language_changed"] = lang_code
    resp.messages.append(t("language.changed", lang_code, label=_language_label(lang_code)))
    return resp


async def render_main_menu(user: UserContext) -> BridgeResponse:
    """Return a transport-neutral representation of the main menu."""

    entries = _menu_entries_for_user(user)
    resp = BridgeResponse()
    resp.messages.append(_compose_menu_text(entries, user.language))
    resp.actions["menu"] = _build_menu_action_payload(entries)
    # Hint to platforms that this payload is the base menu to avoid duplicating menu text.
    resp.actions["menu_only"] = True
    return await _localize_response(resp, user.language)


async def handle_menu_selection(
    user: UserContext,
    message: IncomingMessage,
    storage: Optional[Dict[str, Any]] = None,
    *,
    context: Any = None,
) -> BridgeResponse:
    """Process a menu selection regardless of platform."""

    entries = _menu_entries_for_user(user)
    resp = BridgeResponse()
    resp.actions["menu"] = _build_menu_action_payload(entries)

    selection = (message.text or "").strip()
    if not selection:
        resp.messages.append(t("menu.selection_required", user.language))
        return await _localize_response(resp, user.language)

    entry = _select_menu_entry(entries, selection)
    if not entry:
        resp.messages.append(t("menu.selection_unknown", user.language))
        return await _localize_response(resp, user.language)

    db = storage if isinstance(storage, dict) else load_db()
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    normalized_platform = (message.platform or "").lower()
    wants_html = normalized_platform == "telegram"
    wants_whatsapp = normalized_platform == "whatsapp"

    entry_id = entry["id"]
    show_text = not (entry.get("delegate") and wants_html)

    if entry_id == "profile":
        if show_text:
            resp.messages.append(_compose_profile_overview(db_user, user.language))
    elif entry_id == "balance":
        if show_text:
            resp.messages.append(_compose_balance_overview(db_user, user.language))
    elif entry_id == "report":
        if show_text:
            resp.messages.append(_compose_report_instructions(db_user, user.language))
    elif entry_id == "activation":
        left_days = days_left(db_user.get("expiry_date"))
        is_active = db_user.get("is_active") and (left_days is None or left_days > 0)

        # On WhatsApp we skip the activation prompt text to avoid sending the long instructions screen.
        if show_text and not wants_whatsapp:
            resp.messages.append(_compose_activation_prompt(db_user))

        if is_active:
            # Do not keep the user in activation flow if already active
            resp.actions["clear_activation_state"] = True
        else:
            # If we already have a phone, submit the activation directly; otherwise collect it.
            phone_candidate = user.phone or db_user.get("phone")
            if phone_candidate:
                auto_resp = await submit_activation_request(user, phone_candidate, message.platform, context=context)
                # Merge messages/actions while keeping menu payload
                resp.messages.extend(auto_resp.messages)
                resp.actions.update(auto_resp.actions)
                resp.actions["clear_activation_state"] = True
            else:
                resp.actions["await_activation_phone"] = True
                cc = _extract_pending_country_code(user)
                if cc:
                    resp.actions["activation_cc"] = cc
    elif entry_id == "help":
        if show_text:
            resp.messages.append(_compose_help_text(user.language))
    elif entry_id == "language":
        if wants_html:
            resp.actions["delegate"] = "lang_panel"
        else:
            resp.messages.append(_compose_language_prompt(user.language))
            resp.actions["await_language_choice"] = True
    elif entry_id in {"users", "stats", "pending", "settings", "notifications"}:
        if not wants_html:
            resp.messages.append(_compose_admin_redirect_message(entry["label"], user.language))
        resp.actions["delegate"] = entry_id
    else:
        resp.messages.append(t("menu.unavailable", user.language))

    delegate = entry.get("delegate")
    if delegate:
        resp.actions.setdefault("delegate", delegate)

    # Persist expected states for cross-platform consistency
    if resp.actions.get("await_activation_phone"):
        _persist_user_state(user.user_id, "activation_phone")
    if resp.actions.get("await_language_choice"):
        _persist_user_state(user.user_id, "language_choice")
    if resp.actions.get("clear_activation_state") or resp.actions.get("clear_state"):
        _persist_user_state(user.user_id, None)

    return await _localize_response(resp, user.language)


async def check_user_limits(
    user: UserContext,
    storage: Optional[Dict[str, Any]] = None,
) -> LimitCheckResult:
    """Validate subscription/service/usage constraints for the given user."""

    db, owns_storage = _resolve_storage(storage)
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    bump_usage(db_user)

    if _auto_suspend_if_expired(db_user):
        pass

    allowed = True
    reason: Optional[str] = None
    message: Optional[str] = None

    if not db_user.get("is_active"):
        allowed = False
        reason = "inactive"
        message = _compose_inactive_message(db_user, user.language)
    elif not _service_enabled(db_user, "carfax"):
        allowed = False
        reason = "service_disabled"
        message = t("service.carfax.disabled", user.language)
    else:
        limits = db_user.get("limits", {}) or {}
        daily_limit = _safe_int(limits.get("daily"))
        monthly_limit = _safe_int(limits.get("monthly"))
        today_used = _safe_int(limits.get("today_used"))
        month_used = _safe_int(limits.get("month_used"))
        exceeded_day = daily_limit > 0 and today_used >= daily_limit
        exceeded_month = monthly_limit > 0 and month_used >= monthly_limit
        if exceeded_day or exceeded_month:
            allowed = False
            if exceeded_day and exceeded_month:
                reason = "both"
            elif exceeded_day:
                reason = "daily"
            else:
                reason = "monthly"
            message = _compose_limit_block_message(
                user.language,
                reason,
                today_used,
                daily_limit,
                month_used,
                monthly_limit,
            )

    if owns_storage:
        # Persist any counter resets or auto-suspension updates we just made.
        save_db(db)

    return LimitCheckResult(allowed, message, reason)


async def request_limit_increase(
    user: UserContext,
    storage: Optional[Dict[str, Any]] = None,
    notifications: Any = None,
    *,
    reason: Optional[str] = None,
) -> BridgeResponse:
    """Record and escalate a limit-increase request for a given user."""

    db, owns_storage = _resolve_storage(storage)
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    inferred_reason = reason or _infer_limit_reason(db_user)
    db_user.setdefault("language", user.language)

    resp = BridgeResponse()
    resp.messages.append(_compose_limit_request_user_message(user.language, inferred_reason))
    resp.actions["limit_request"] = {
        "reason": inferred_reason or "unknown",
        "ts": now_str(),
        "user_id": user.user_id,
    }

    admin_text = _compose_limit_request_admin_text(db_user, inferred_reason)
    if admin_text and notifications:
        kb = None
        if InlineKeyboardMarkup and InlineKeyboardButton and inferred_reason in {"daily", "both"}:
            try:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(t("limits.buttons.reset_today", user.language), callback_data=f"limits:reset_today:{user.user_id}")],
                ])
            except Exception:
                kb = None

        try:
            await notify_supers(notifications, admin_text, kb)
            resp.actions["limit_request"]["notified_supers"] = True
        except Exception:  # pragma: no cover - best-effort notification
            LOGGER.exception("Failed to notify super admins about limit request for user_id=%s", user.user_id)
    elif admin_text:
        LOGGER.debug("No notification context available for limit request user_id=%s", user.user_id)

    if owns_storage:
        save_db(db)

    return resp


async def submit_activation_request(
    user: UserContext,
    phone: str,
    platform: str,
    context: Any = None,
) -> BridgeResponse:
    """Directly submit an activation request for the user."""
    db = load_db()
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    resp = BridgeResponse()

    # Check if already active
    left_days = days_left(db_user.get("expiry_date"))
    if db_user.get("is_active") and (left_days is None or left_days > 0):
        resp.messages.append(t("activation.already_active", user.language))
        return resp

    # Check if pending
    pending = db.setdefault("activation_requests", [])
    existing = next((req for req in pending if str(req.get("tg_id")) == user.user_id), None)
    
    # Update phone in DB user
    db_user["phone"] = phone

    if existing:
        # Update existing request and re-notify supers (user may be updating phone)
        existing["phone"] = phone
        existing["ts"] = now_str()
        resp.messages.append(t("activation.request_pending", user.language))
        await _maybe_notify_supers(context, db_user, platform)
    else:
        # Create new request
        pending.append(
            {
                "tg_id": user.user_id,
                "ts": now_str(),
                "phone": phone,
                "platform": platform,
            }
        )
        resp.messages.append(t("activation.request_received", user.language))
        await _maybe_notify_supers(context, db_user, platform)

    save_db(db)
    return resp


async def _handle_start_flow(user: UserContext, message: IncomingMessage) -> BridgeResponse:
    db = load_db()
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    db_user["onboarded"] = True
    save_db(db)

    resp = BridgeResponse()
    resp.messages.append(_compose_start_message(db_user, user, message.platform))
    resp.actions["welcome"] = {"platform": message.platform}
    return resp


async def _handle_activation_submission(
    user: UserContext,
    message: IncomingMessage,
    phone: str,
    context: Any,
) -> BridgeResponse:
    db = load_db()
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    resp = BridgeResponse()

    left_days = days_left(db_user.get("expiry_date"))
    if db_user.get("is_active") and (left_days is None or left_days > 0):
        resp.messages.append(t("activation.already_active", user.language))
        resp.actions["clear_activation_state"] = True
        return resp

    pending = db.setdefault("activation_requests", [])
    existing = next((req for req in pending if str(req.get("tg_id")) == user.user_id), None)
    db_user["phone"] = phone

    if existing:
        existing["phone"] = phone
        existing["ts"] = now_str()
        resp.messages.append(t("activation.request_pending", user.language))
        await _maybe_notify_supers(context, db_user, message.platform)
    else:
        pending.append(
            {
                "tg_id": user.user_id,
                "ts": now_str(),
                "phone": phone,
                "platform": message.platform,
            }
        )
        resp.messages.append(t("activation.request_received", user.language))
        await _maybe_notify_supers(context, db_user, message.platform)

    save_db(db)
    resp.actions["clear_activation_state"] = True
    return resp


async def _maybe_notify_supers(context: Any, user: Dict[str, Any], platform: Optional[str]) -> None:
    if not context:
        LOGGER.warning("No Telegram context available to notify super admins about activation request")
        return
    tg_id = str(user.get("tg_id") or "") or "unknown"
    name = display_name(user)
    platform_label = (platform or "unknown").upper()
    msg = (
        f"📥 طلب تفعيل جديد\n"
        f"👤 {name} ({format_tg_with_phone(tg_id)})\n"
        f"📱 المنصة: {platform_label}"
    )

    kb = None
    if InlineKeyboardMarkup and InlineKeyboardButton:
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧪 تجربة (1,25,25)", callback_data=f"ucard:trial:{tg_id}")],
                [InlineKeyboardButton("🟢 شهري (30,25,500)", callback_data=f"ucard:monthly:{tg_id}")],
                [InlineKeyboardButton("♻️ تجديد 30 يوم", callback_data=f"ucard:renew30:{tg_id}")],
                [InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"ucard:open:{tg_id}")],
            ])
        except Exception:
            kb = None
    try:
        await notify_supers(context, msg, kb)
    except Exception:  # pragma: no cover - notification best-effort
        LOGGER.exception("Failed to notify super admins about activation request")


def _compose_start_message(db_user: Dict[str, Any], ctx_user: UserContext, platform: Optional[str]) -> str:
    lang = (ctx_user.language or db_user.get("language") or "ar").lower()
    user_name = _infer_username(ctx_user)
    is_active = bool(db_user.get("is_active"))
    credit_left = remaining_monthly_reports(db_user)
    unlimited_label = {"ar": "غير محدود", "en": "Unlimited", "ku": "بێ سنوور"}.get(lang, "Unlimited")
    credit_label = str(credit_left) if credit_left is not None else unlimited_label
    left_days = days_left(db_user.get("expiry_date"))
    status_label = t("account.status.active", lang) if is_active else t("account.status.inactive", lang)

    parts = [
        t("start.greeting", lang, name=user_name),
        t("start.status.header", lang),
        t("start.status.line", lang, status=status_label),
        t("start.balance.line", lang, credit=credit_label),
    ]

    if left_days is not None and left_days > 0:
        parts.append(t("start.days_left", lang, days=left_days))
    elif left_days == 0:
        parts.append(t("start.ends_today", lang))

    parts.append(t("start.options.header", lang))
    parts.append(
        t(
            "start.options.list",
            lang,
            report=t("menu.report.label", lang),
            profile=t("menu.profile.label", lang),
            balance=t("menu.balance.label", lang),
            activation=t("menu.activation.label", lang),
            help=t("menu.help.label", lang),
            language=t("menu.language.label", lang),
        )
    )

    if _is_admin_tg(ctx_user.user_id) or _is_super_admin(ctx_user.user_id):
        settings_label = t("menu.settings.label", lang) if _is_super_admin(ctx_user.user_id) else ""
        parts.append(t("start.admin.header", lang))
        parts.append(
            t(
                "start.admin.list",
                lang,
                users=t("menu.users.label", lang),
                stats=t("menu.stats.label", lang),
                pending=t("menu.pending.label", lang),
                settings=t("start.admin.settings", lang, settings=settings_label) if settings_label else "",
            )
        )

    footer_key = "start.footer.telegram" if (platform or "telegram").lower() == "telegram" else "start.footer.other"
    parts.append(t(footer_key, lang))

    return "\n".join([p for p in parts if p])


def _infer_username(user: UserContext) -> str:
    metadata = user.metadata or {}
    return (
        metadata.get("first_name")
        or metadata.get("sender_name")
        or metadata.get("username")
        or user.user_id
    )


def _extract_vin_candidate(text: Optional[str]) -> Optional[str]:
    """Extract a normalized VIN from free-form user input."""

    if not text:
        return None

    raw = _sanitize_for_vin(text)
    try:
        LOGGER.debug("vin.extract candidate", extra={"raw": raw, "orig": text[:80]})
    except Exception:
        pass
    if not raw:
        return None

    normalized = normalize_vin(raw)
    if normalized:
        return normalized

    # Fallback: scan any 17-char alphanumeric window inside sanitized text
    if len(raw) >= 17:
        for idx in range(0, len(raw) - 16):
            window = raw[idx:idx+17]
            if VIN_RE.match(window):
                return window

    # RTL safety: if bidi markers flipped ordering, try reversed text as a last resort
    if len(raw) >= 17:
        reversed_raw = raw[::-1]
        for idx in range(0, len(reversed_raw) - 16):
            window = reversed_raw[idx:idx+17]
            if VIN_RE.match(window):
                return window[::-1]

    lowered = raw.lower()
    for prefix in VIN_COMMAND_PREFIXES:
        if lowered.startswith(prefix):
            remainder = raw[len(prefix):].strip(" :=")
            normalized = normalize_vin(remainder)
            if normalized:
                return normalized

    for token in VIN_TOKEN_SPLIT_RE.split(raw):
        candidate = normalize_vin(token)
        if candidate:
            return candidate

    return None


def _looks_like_vin(text: Optional[str]) -> bool:
    """Heuristic to detect VIN-like strings (even if invalid) to avoid falling back to menu."""

    if not text:
        return False
    cleaned = _VIN_CONTROL_RE.sub("", text)
    cleaned = cleaned.translate(_VIN_DIGIT_TRANSLATE)
    cleaned = re.sub(r"[\s:-]", "", cleaned).upper()
    cleaned = re.sub(r"[^A-Z0-9]", "", cleaned)
    if len(cleaned) < 10:
        return False
    if re.fullmatch(r"[A-Z0-9]+", cleaned):
        return True

    # RTL safety: if bidi markers flipped order, retry on reversed string
    reversed_cleaned = cleaned[::-1]
    if len(reversed_cleaned) >= 10 and re.fullmatch(r"[A-Z0-9]+", reversed_cleaned):
        return True
    return False


async def _handle_vin_request(
    user: UserContext,
    message: IncomingMessage,
    vin: str,
    *,
    context: Any = None,
    skip_limit_validation: bool = False,
    deduct_credit: bool = True,
    pre_reserved_credit: bool = False,
) -> BridgeResponse:
    """Invoke the VIN report service and convert the result into a response."""

    response = BridgeResponse()
    language = (user.language or "en").lower()

    credit_reserved = bool(pre_reserved_credit and deduct_credit)

    if not skip_limit_validation:
        allowed, limit_message, limit_reason = await check_user_limits(user)
        if not allowed:
            if deduct_credit and credit_reserved:
                refund_credit(user.user_id)
                credit_reserved = False

            if limit_reason in {"daily", "monthly", "both"}:
                limit_response = await request_limit_increase(
                    user,
                    notifications=context,
                    reason=limit_reason,
                )
                if limit_message:
                    limit_response.messages.insert(0, limit_message)
            else:
                limit_response = BridgeResponse()
                if limit_message:
                    limit_response.messages.append(limit_message)
            limit_response.actions.setdefault("limit_blocked", {})["reason"] = limit_reason or "unknown"
            return limit_response

    # Reserve credit before attempting generation (unless already reserved upstream)
    if deduct_credit and not credit_reserved:
        reserve_credit(user.user_id)
        credit_reserved = True

    try:
        report_result = await generate_vin_report(vin, language=language)
        
        # If successful, commit the credit usage
        if deduct_credit and credit_reserved:
            commit_credit(user.user_id)
            
    except Exception as exc:  # pylint: disable=broad-except
        # If failed, refund the credit
        if deduct_credit and credit_reserved:
            refund_credit(user.user_id)
            
        LOGGER.exception("VIN report generation failed for user_id=%s", user.user_id)
        response.messages.append(t("vin.error", user.language))
        response.actions["error"] = str(exc)
        response.actions["vin"] = vin
        return response

    response.actions["report_result"] = report_result
    response.actions["vin"] = report_result.vin or vin
    response.actions["source_text"] = message.text or ""

    user_message = report_result.user_message or "📄 تم تجهيز تقرير VIN الخاص بك."
    response.messages.append(user_message)

    if report_result.success and report_result.pdf_bytes and _should_attach_pdf(message.platform):
        pdf_path = _persist_pdf_to_temp(report_result, user)
        if pdf_path:
            response.documents.append(
                {
                    "type": "pdf",
                    "path": pdf_path,
                    "caption": user_message,
                    "filename": os.path.basename(pdf_path),
                }
            )
            response.actions.setdefault("temp_files", []).append(pdf_path)

    return response


def _should_attach_pdf(platform: Optional[str]) -> bool:
    if not platform:
        return False
    return platform.lower() in {"telegram", "whatsapp"}


def _persist_pdf_to_temp(result: ReportResult, user: UserContext) -> Optional[str]:
    if not result.pdf_bytes:
        return None

    filename = _sanitize_filename(result.pdf_filename or f"{result.vin or 'report'}.pdf")
    tmp_dir = Path(tempfile.gettempdir())
    unique_name = f"vin-{user.user_id}-{int(time.time() * 1000)}-{filename}"
    target = tmp_dir / unique_name
    try:
        with open(target, "wb") as handler:
            handler.write(result.pdf_bytes)
    except OSError:
        LOGGER.exception("Failed to persist VIN PDF for user_id=%s", user.user_id)
        return None
    return str(target)


def _sanitize_filename(filename: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", filename or "report.pdf")
    return base if base.lower().endswith(".pdf") else f"{base}.pdf"


def _extract_pending_country_code(user: UserContext) -> Optional[str]:
    metadata = user.metadata or {}
    user_data = metadata.get("user_data") or {}
    cc = user_data.get("activation_cc")
    if isinstance(cc, str) and cc.strip():
        return cc.strip()
    return None


def _normalize_phone(raw: Optional[str], cc: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    sanitized = re.sub(r"[\s()_-]", "", raw)
    if sanitized.startswith("+") and sanitized[1:].isdigit() and 9 <= len(sanitized) <= 16:
        return sanitized
    if cc and sanitized.isdigit():
        local = sanitized.lstrip("0")
        if not local:
            return None
        prefix = cc if cc.startswith("+") else f"+{cc}"
        candidate = f"{prefix}{local}"
        candidate = candidate.replace("++", "+")
        if candidate.startswith("+") and candidate[1:].isdigit() and 9 <= len(candidate) <= 16:
            return candidate
    return None


def _activation_invalid_message(language: Optional[str], cc: Optional[str]) -> str:
    cc_hint = t("activation.invalid_cc_hint", language, cc=cc) if cc else ""
    return t("activation.invalid", language, cc_hint=cc_hint)


def _extract_general_phone_candidate(user: UserContext, text: str) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
    if not PHONE_INPUT_RE.match(stripped):
        return None
    cc = _extract_pending_country_code(user)
    normalized = _normalize_phone(stripped, cc)
    if normalized:
        return normalized
    # fallback for numbers that already include + but were rejected due to length bounds
    if stripped.startswith("+") and len(stripped) >= 9:
        digits_only = "+" + re.sub(r"[^0-9]", "", stripped)
        if len(digits_only) >= 9:
            return digits_only
    return None


def _infer_media_filename(message: IncomingMessage) -> str:
    if message.file_name:
        return message.file_name
    raw = message.raw or {}
    for key in ("file_name", "filename", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    source = message.media_url or ""
    if "/" in source or "\\" in source:
        candidate = source.rstrip("/").split("/")[-1]
        if candidate:
            return candidate
    return "upload.bin"


def _guess_mime_from_name(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    mime, _ = mimetypes.guess_type(filename)
    return mime


async def _download_remote_media(url: str, mime_hint: Optional[str]) -> Tuple[Optional[bytes], Optional[str]]:
    data = await download_image_bytes(url)
    if data:
        return data, mime_hint or _guess_mime_from_name(url)
    try:
        import httpx  # local import to avoid mandatory dependency if unused

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type") or mime_hint
    except Exception:
        return None, mime_hint


def _persist_incoming_media(user_id: str, filename: Optional[str], payload: bytes) -> Optional[str]:
    safe_name = _sanitize_filename(filename or "upload.bin")
    root = Path(tempfile.gettempdir()) / "bot_media_uploads"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = root / f"{user_id}-{int(time.time() * 1000)}-{safe_name}"
    try:
        with open(target, "wb") as handler:
            handler.write(payload)
    except OSError:
        LOGGER.exception("Failed to persist incoming media for user_id=%s", user_id)
        return None
    return str(target)


def _record_media_entry(user: UserContext, entry: Dict[str, Any]) -> Dict[str, Any]:
    db = load_db()
    db_user = ensure_user(db, user.user_id, _infer_username(user))
    media_log = db_user.setdefault("media_uploads", [])
    media_log.append(dict(entry))
    db_user["media_uploads"] = media_log[-20:]
    save_db(db)
    return media_log[-1]


def _compose_media_ack(user: UserContext, entry: Dict[str, Any]) -> str:
    state = (user.state or "").lower()
    if state in {"vin_photo", "vin_attachment"}:
        return t("media.ack.vin", user.language)
    if state in {"support_media", "support_attachment"}:
        return t("media.ack.support", user.language)
    caption = entry.get("caption")
    if caption:
        return t("media.ack.default", user.language) + "\n\n" + caption
    return t("media.ack.default", user.language)


def _menu_entries_for_user(user: UserContext) -> List[Dict[str, Any]]:
    is_admin = _is_admin_tg(user.user_id)
    is_super = _is_super_admin(user.user_id)
    allowed: List[Dict[str, Any]] = []
    for item in MENU_REGISTRY:
        if item.get("requires_super") and not is_super:
            continue
        if item.get("requires_admin") and not (is_admin or is_super):
            continue
        entry = dict(item)
        label_key = entry.get("label_key")
        desc_key = entry.get("description_key")
        entry["label"] = t(label_key, user.language) if label_key else entry.get("label", "")
        if desc_key:
            entry["description"] = t(desc_key, user.language)
        allowed.append(entry)
    allowed.sort(key=lambda entry: (entry.get("row", 1000), entry.get("col", 0), entry["label"]))
    return allowed


def _build_menu_action_payload(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload_items = []
    for idx, entry in enumerate(entries, start=1):
        payload_items.append(
            {
                "id": entry["id"],
                "label": entry["label"],
                "description": entry.get("description"),
                "row": entry.get("row"),
                "col": entry.get("col"),
                "order": idx,
                "requires_admin": entry.get("requires_admin", False),
                "requires_super": entry.get("requires_super", False),
            }
        )
    return {"items": payload_items}


def _compose_menu_text(entries: List[Dict[str, Any]], language: Optional[str]) -> str:
    if not entries:
        return t("menu.empty", language)
    lines = [t("menu.header", language), "", t("menu.instructions", language)]
    for idx, entry in enumerate(entries, start=1):
        desc = entry.get("description") or ""
        if desc:
            lines.append(f"{idx}) {entry['label']} — {desc}")
        else:
            lines.append(f"{idx}) {entry['label']}")
    return "\n".join(lines)


def _select_menu_entry(entries: List[Dict[str, Any]], selection: str) -> Optional[Dict[str, Any]]:
    normalized = selection.strip().lower()
    if normalized.isdigit():
        idx = int(normalized) - 1
        if 0 <= idx < len(entries):
            return entries[idx]
    for entry in entries:
        if normalized == entry["id"]:
            return entry
        if normalized == entry["label"].lower():
            return entry
    return None


def _compose_profile_overview(db_user: Dict[str, Any], language: Optional[str] = None) -> str:
    lang = (language or db_user.get("language") or "ar").lower()
    phone = _format_phone_value(db_user.get("phone"))

    limits = db_user.get("limits", {}) or {}
    monthly_limit = _safe_int(limits.get("monthly"))
    monthly_left = remaining_monthly_reports(db_user)
    unlimited_label = {"ar": "غير محدود", "en": "Unlimited", "ku": "بێ سنوور"}.get(lang, "Unlimited")
    monthly_display = (
        f"{monthly_left}/{monthly_limit}"
        if monthly_limit > 0 and monthly_left is not None
        else unlimited_label
    )

    activation_date = fmt_date(db_user.get("activation_date")) or "-"
    expiry_date = fmt_date(db_user.get("expiry_date")) or "-"

    services = db_user.get("services", {}) or {}
    carfax_status = "✅" if services.get("carfax", True) else "⛔"
    # Per requirement: Photos should always appear enabled for users
    photos_status = "✅"

    today_used = _safe_int(limits.get("today_used"))
    daily_limit = _safe_int(limits.get("daily"))
    month_used = _safe_int(limits.get("month_used"))

    daily_str = f"{today_used} / {daily_limit}" if daily_limit > 0 else f"{today_used} / ∞"
    monthly_str = f"{month_used} / {monthly_limit}" if monthly_limit > 0 else f"{month_used} / ∞"

    left_days = days_left(db_user.get("expiry_date"))
    status_key = "account.status.active"
    if left_days is not None and left_days <= 0:
        status_key = "account.status.expired"
    elif not db_user.get("is_active"):
        status_key = "account.status.inactive"
    status_label = t(status_key, lang)

    lines = [
        t("account.header", lang),
        "━━━━━━━━━━━━━━━━━━━━",
        t("account.section.basic", lang),
        t("account.field.phone", lang, value=f"{phone} 📞"),
        "━━━━━━━━━━━━━━━━━━━━",
        t("account.section.status", lang),
        t("account.field.status", lang, value=status_label),
        t("account.field.monthly_remaining", lang, value=monthly_display),
        t("account.field.activation_date", lang, value=activation_date),
        t("account.field.expiry_date", lang, value=expiry_date),
        "━━━━━━━━━━━━━━━━━━━━",
        t("account.section.services", lang),
        t("account.field.service.carfax", lang, value=carfax_status),
        t("account.field.service.photos", lang, value=photos_status),
        "━━━━━━━━━━━━━━━━━━━━",
        t("account.section.limits", lang),
        t("account.field.daily", lang, value=daily_str),
        t("account.field.monthly_limit", lang, value=monthly_str),
    ]

    return "\n".join([line for line in lines if line.strip()])


def _compose_balance_overview(db_user: Dict[str, Any], language: Optional[str]) -> str:
    lang = (language or db_user.get("language") or "ar").lower()
    limits = db_user.get("limits", {}) or {}
    today_used = _safe_int(limits.get("today_used"))
    daily_limit = _safe_int(limits.get("daily"))
    monthly_limit = _safe_int(limits.get("monthly"))
    monthly_left = remaining_monthly_reports(db_user)
    left_days = days_left(db_user.get("expiry_date"))
    lines = [
        t("balance.title", lang),
        "",
        t("balance.daily", lang, today=today_used, daily=daily_limit or "—"),
    ]
    if monthly_limit and monthly_limit > 0 and monthly_left is not None:
        lines.append(t("balance.monthly", lang, remaining=monthly_left, monthly=monthly_limit))
    elif monthly_left is not None:
        lines.append(t("balance.remaining", lang, remaining=monthly_left))
    else:
        lines.append(t("balance.unlimited", lang))
    if left_days is not None:
        if left_days > 0:
            lines.append(t("balance.expiring_in", lang, days=left_days))
        elif left_days == 0:
            lines.append(t("balance.expires_today", lang))
        else:
            lines.append(t("balance.expired", lang))
    lines.append(t("balance.deduction", lang))
    return "\n".join(lines)


def _compose_report_instructions(db_user: Dict[str, Any], language: Optional[str]) -> str:
    lang = (language or db_user.get("language") or "ar").lower()
    monthly_left = remaining_monthly_reports(db_user)
    limit_line = t("report.limit_line", lang, value=monthly_left) if monthly_left is not None else t("report.limit_unlimited", lang)
    return t("report.instructions", lang, limit_line=limit_line)


def _compose_activation_prompt(db_user: Dict[str, Any]) -> str:
    if db_user.get("is_active"):
        return t("activation.already_active", db_user.get("language") or "ar")
    return t("activation.prompt", db_user.get("language") or "ar")


def _compose_help_text(language: Optional[str]) -> str:
    return t(
        "help.body",
        language,
        site="https://www.dejavuplus.com",
        email="info@dejavuplus.com",
        support="https://wa.me/962795378832",
    )


def _compose_language_prompt(current_code: Optional[str]) -> str:
    current = current_code or "ar"
    return t("language.prompt", current, current=_language_label(current))


def _compose_admin_redirect_message(label: str, language: Optional[str]) -> str:
    return t("menu.admin_redirect", language, label=label)


async def _localize_response(response: Optional[BridgeResponse], language: Optional[str]) -> BridgeResponse:
    if not isinstance(response, BridgeResponse):
        response = BridgeResponse()
    lang = (language or "ar").lower()

    # Translate non-Arabic languages
    if response.messages and lang not in {"", "ar"}:
        await _translate_messages_in_place(response.messages, lang)

    # Append footer to non-menu replies
    if not response.actions.get("menu"):
        hint = t("main_menu.hint", lang)
        for idx in range(len(response.messages) - 1, -1, -1):
            msg = response.messages[idx]
            if not msg:
                continue
            if hint in msg:
                break
            suffix = "\n\n" if not msg.endswith("\n") else "\n"
            response.messages[idx] = msg + suffix + hint
            break

    return response


async def _translate_messages_in_place(messages: List[str], language: str) -> None:
    payload = [(idx, msg) for idx, msg in enumerate(messages) if isinstance(msg, str) and msg.strip()]
    if not payload:
        return
    _, texts = zip(*payload)
    try:
        translated = await translate_batch(list(texts), target=language)
    except Exception:
        LOGGER.debug("Translation fallback for language=%s", language, exc_info=True)
        return
    for (idx, _), translated_text in zip(payload, translated):
        messages[idx] = translated_text


def _resolve_storage(storage: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
    if isinstance(storage, dict):
        return storage, False
    return load_db(), True


def _auto_suspend_if_expired(user: Dict[str, Any]) -> bool:
    expiry_raw = user.get("expiry_date")
    if not expiry_raw:
        return False
    try:
        expiry_date = datetime.strptime(expiry_raw, "%Y-%m-%d").date()
    except Exception:
        return False
    today = date.today()
    if expiry_date < today and user.get("is_active"):
        user["is_active"] = False
        return True
    return False


def _service_enabled(user: Dict[str, Any], key: str) -> bool:
    services = user.get("services", {}) or {}
    return bool(services.get(key, True))


def _compose_inactive_message(user: Dict[str, Any], language: Optional[str]) -> str:
    lang = (language or user.get("language") or "ar").lower()
    expiry = fmt_date(user.get("expiry_date"))
    if user.get("expiry_date"):
        try:
            exp_date = datetime.strptime(user["expiry_date"], "%Y-%m-%d").date()
        except Exception:
            exp_date = None
        if exp_date and exp_date < date.today():
            return t("account.inactive.expired", lang, expiry=expiry or "-")
    return t("account.inactive", lang)


def _compose_limit_block_message(
    language: Optional[str],
    reason: Optional[str],
    today_used: int,
    daily_limit: int,
    month_used: int,
    monthly_limit: int,
) -> str:
    if reason == "daily":
        body = t("limit.block.daily", language, today_used=today_used, daily_limit=daily_limit)
    elif reason == "monthly":
        body = t("limit.block.monthly", language, month_used=month_used, monthly_limit=monthly_limit)
    else:
        body = t(
            "limit.block.both",
            language,
            today_used=today_used,
            daily_limit=daily_limit,
            month_used=month_used,
            monthly_limit=monthly_limit,
        )
    return body + "\n" + t("limit.block.notice", language)


def _infer_limit_reason(user: Dict[str, Any]) -> Optional[str]:
    limits = user.get("limits", {}) or {}
    daily_limit = _safe_int(limits.get("daily"))
    monthly_limit = _safe_int(limits.get("monthly"))
    today_used = _safe_int(limits.get("today_used"))
    month_used = _safe_int(limits.get("month_used"))
    exceeded_day = daily_limit > 0 and today_used >= daily_limit
    exceeded_month = monthly_limit > 0 and month_used >= monthly_limit
    if exceeded_day and exceeded_month:
        return "both"
    if exceeded_day:
        return "daily"
    if exceeded_month:
        return "monthly"
    return None


def _compose_limit_request_user_message(language: Optional[str], reason: Optional[str]) -> str:
    label = _limit_reason_label(language, reason)
    return t("limit.request.user", language, label=label)


def _compose_limit_request_admin_text(user: Dict[str, Any], reason: Optional[str]) -> Optional[str]:
    limits = user.get("limits", {}) or {}
    today_used = _safe_int(limits.get("today_used"))
    daily_limit = _safe_int(limits.get("daily"))
    month_used = _safe_int(limits.get("month_used"))
    monthly_limit = _safe_int(limits.get("monthly"))
    reason_label = _limit_reason_label(user.get("language"), reason)
    return t(
        "limit.request.admin",
        user.get("language"),
        user_name=display_name(user),
        contact=format_tg_with_phone(user.get("tg_id") or user.get("id") or ""),
        today_used=today_used,
        daily_limit=daily_limit or "—",
        month_used=month_used,
        monthly_limit=monthly_limit or "—",
        reason=reason_label,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_phone_value(raw: Optional[str]) -> str:
    sanitized = (raw or "").strip()
    return sanitized or "—"
