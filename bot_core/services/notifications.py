"""Notification helpers (users + supers) plus auto reminders."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Dict, Any, List
import logging
import re

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot_core.auth import env_super_admins as _env_super_admins, db_super_admins as _db_super_admins
from bot_core.clients.ultramsg import UltraMsgClient, UltraMsgCredentials
from bot_core.config import get_ultramsg_settings
from bot_core.storage import (
    load_db as _load_db,
    save_db as _save_db,
    fmt_date as _fmt_date,
    remaining_monthly_reports as _remaining_monthly_reports,
    display_name as _display_name,
    format_tg_with_phone as _fmt_tg_with_phone,
)


LOGGER = logging.getLogger(__name__)


# Tunable smart notification settings (kept centralized for safe tweaking)
SMART_NOTIFY_RULES: Dict[str, Any] = {
    "expiry_days": [14, 7, 3, 1, 0],
    "inactivity_days": [7, 14],
    "activation_welcome_days": 3,
    "pending_sla_minutes": 20,
    "quiet_hours": {"start": 22, "end": 8},  # local server time window to defer user pings
    "daily_digest_hour": 9,
    "reactivate_every_days": 3,
    "low_balance_threshold": 5,
}


# Localized notification templates
NOTIFY_TEMPLATES: Dict[str, Dict[str, str]] = {
    "expiry_day_1": {
        "ar": "⏰ <b>اشتراكك ينتهي غدًا</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ بقي أقل من 24 ساعة على انتهاء اشتراكك.\n\n• تاريخ الانتهاء: <code>{expiry}</code>\n• التقارير المتبقية: <b>{monthly_left}</b>\n\n💡 <i>راسل الإدارة الآن لتجديد اشتراكك قبل الانقطاع</i>",
        "en": "⏰ <b>Your subscription ends tomorrow</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ Less than 24h remaining.\n\n• Expiry: <code>{expiry}</code>\n• Reports left: <b>{monthly_left}</b>\n\n💡 <i>Contact admins now to renew</i>",
        "ku": "⏰ <b>بەشداریکەتی تەواو دەبێت سبەی</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ کەمتر لە ٢٤ کاتژمێر ماوە.\n\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 <i>پەیوەندی بکە بۆ نوێکردنەوە</i>",
        "ckb": "⏰ <b>بەشداریکەتی تەواو دەبێت سبەی</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ کەمتر لە ٢٤ کاتژمێر ماوە.\n\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 <i>پەیوەندی بکە بۆ نوێکردنەوە</i>",
    },
    "expiry_week": {
        "ar": "⏰ <b>تنبيه انتهاء الاشتراك</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ اشتراكك سينتهي خلال <b>{days_left}</b> يوم.\n\n• تاريخ الانتهاء: <code>{expiry}</code>\n• التقارير المتبقية: <b>{monthly_left}</b>\n\n💡 <i>يرجى التواصل مع الإدارة لتجديد الاشتراك</i>",
        "en": "⏰ <b>Subscription expiring</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ Ends in <b>{days_left}</b> days.\n\n• Expiry: <code>{expiry}</code>\n• Reports left: <b>{monthly_left}</b>\n\n💡 <i>Please contact admins to renew</i>",
        "ku": "⏰ <b>بەشداریکەتی لە دوای <b>{days_left}</b> ڕۆژدا کۆتایی دێت</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 <i>تکایە پەیوەندی بکە بۆ نوێکردنەوە</i>",
        "ckb": "⏰ <b>بەشداریکەتی لە دوای <b>{days_left}</b> ڕۆژدا کۆتایی دێت</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 <i>تکایە پەیوەندی بکە بۆ نوێکردنەوە</i>",
    },
    "expiry_today": {
        "ar": "⛔ <b>انتهى اشتراكك اليوم</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• تاريخ الانتهاء: <code>{expiry}</code>\n• تم إيقاف استخراج التقارير مؤقتًا.\n\n💡 <i>تواصل مع الإدارة لإعادة التفعيل</i>",
        "en": "⛔ <b>Your subscription ended today</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• Expiry: <code>{expiry}</code>\n• Report generation is paused.\n\n💡 <i>Contact admins to reactivate</i>",
        "ku": "⛔ <b>بەشداریکەتی ئەمڕۆ کۆتایی هات</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرت وەردەگیرێت.\n\n💡 <i>پەیوەندی بکە بۆ چالاککردنەوە</i>",
        "ckb": "⛔ <b>بەشداریکەتی ئەمڕۆ کۆتایی هات</b>\n\n━━━━━━━━━━━━━━━━━━━━\n• بەسەرچوون: <code>{expiry}</code>\n• ڕاپۆرت وەردەگیرێت.\n\n💡 <i>پەیوەندی بکە بۆ چالاککردنەوە</i>",
    },
    "expired": {
        "ar": "⛔ <b>اشتراكك منتهي</b>\n\n━━━━━━━━━━━━━━━━━━━━\nانتهى الاشتراك منذ <b>{days_over}</b> يوم.\nلا يمكن استخراج التقارير حتى إعادة التفعيل.\n\n• تاريخ الانتهاء: <code>{expiry}</code>\n💡 <i>تواصل مع الإدارة للتجديد</i>",
        "en": "⛔ <b>Your subscription is expired</b>\n\n━━━━━━━━━━━━━━━━━━━━\nExpired <b>{days_over}</b> days ago.\nReports are blocked until reactivation.\n\n• Expiry: <code>{expiry}</code>\n💡 <i>Contact admins to renew</i>",
        "ku": "⛔ <b>بەشداریکەتی بەسەرچووە</b>\n\n━━━━━━━━━━━━━━━━━━━━\nلە <b>{days_over}</b> ڕۆژ پێشوو کۆتایی هاتووە.\nڕاپۆرت ناگاتەدەت تاوەکو چالاک نەکرێت.\n\n• بەسەرچوون: <code>{expiry}</code>\n💡 <i>پەیوەندی بکە بۆ نوێکردنەوە</i>",
        "ckb": "⛔ <b>بەشداریکەتی بەسەرچووە</b>\n\n━━━━━━━━━━━━━━━━━━━━\nلە <b>{days_over}</b> ڕۆژ پێشوو کۆتایی هاتووە.\nڕاپۆرت ناگاتەدەت تاوەکو چالاک نەکرێت.\n\n• بەسەرچوون: <code>{expiry}</code>\n💡 <i>پەیوەندی بکە بۆ نوێکردنەوە</i>",
    },
    "daily_warn": {
        "ar": "📊 <b>تنبيه الحد اليومي</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ اقتربت من الحد اليومي!\n\n• الاستخدام اليوم: <b>{used}/{limit}</b>\n• المتبقي: <b>{remaining}</b> تقرير\n\n💡 <i>سيتم إعادة تعيين العداد غدًا</i>",
        "en": "📊 <b>Daily limit warning</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ You are near the daily limit.\n\n• Today: <b>{used}/{limit}</b>\n• Remaining: <b>{remaining}</b> reports\n\n💡 <i>Resets tomorrow</i>",
        "ku": "📊 <b>ئاگاداری سنووری ڕۆژانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ نزیک بوونی سنوورەکە.\n\n• ئەمڕۆ: <b>{used}/{limit}</b>\n• ماوە: <b>{remaining}</b> ڕاپۆرت\n\n💡 <i>سبەی دەگەڕێتە صفر</i>",
        "ckb": "📊 <b>ئاگاداری سنووری ڕۆژانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ نزیک بوونی سنوورەکە.\n\n• ئەمڕۆ: <b>{used}/{limit}</b>\n• ماوە: <b>{remaining}</b> ڕاپۆرت\n\n💡 <i>سبەی دەگەڕێتە صفر</i>",
    },
    "monthly_warn": {
        "ar": "📊 <b>تنبيه الحد الشهري</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ اقتربت من الحد الشهري!\n\n• الاستخدام هذا الشهر: <b>{used}/{limit}</b>\n• المتبقي: <b>{remaining}</b> تقرير\n\n💡 <i>سيتم إعادة التعيين في بداية الشهر القادم</i>",
        "en": "📊 <b>Monthly limit warning</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ You are near the monthly limit.\n\n• This month: <b>{used}/{limit}</b>\n• Remaining: <b>{remaining}</b> reports\n\n💡 <i>Resets next month</i>",
        "ku": "📊 <b>ئاگاداری سنووری مانگانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ نزیک بوونی سنوورەکە.\n\n• ئەم مانگە: <b>{used}/{limit}</b>\n• ماوە: <b>{remaining}</b> ڕاپۆرت\n\n💡 <i>لە مانگی داهاتوودا دەگەڕێتە صفر</i>",
        "ckb": "📊 <b>ئاگاداری سنووری مانگانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ نزیک بوونی سنوورەکە.\n\n• ئەم مانگە: <b>{used}/{limit}</b>\n• ماوە: <b>{remaining}</b> ڕاپۆرت\n\n💡 <i>لە مانگی داهاتوودا دەگەڕێتە صفر</i>",
    },
    "daily_hit": {
        "ar": "📈 <b>بلغت الحد اليومي</b>\nالاستخدام: <b>{used}/{limit}</b> تقرير.\n💡 <i>سيُعاد التعيين تلقائيًا غدًا أو اطلب رفع الحد من الإدارة</i>",
        "en": "📈 <b>Daily limit reached</b>\nUsage: <b>{used}/{limit}</b>.\n💡 <i>Resets tomorrow or request an increase</i>",
        "ku": "📈 <b>گەیشتە سنووری ڕۆژانە</b>\nبەکارهێنان: <b>{used}/{limit}</b>.\n💡 <i>سبەی دەگەڕێتە صفر یان داوا لە زیادکردن بکە</i>",
        "ckb": "📈 <b>گەیشتە سنووری ڕۆژانە</b>\nبەکارهێنان: <b>{used}/{limit}</b>.\n💡 <i>سبەی دەگەڕێتە صفر یان داوا لە زیادکردن بکە</i>",
    },
    "monthly_hit": {
        "ar": "📊 <b>بلغت الحد الشهري</b>\nالاستخدام: <b>{used}/{limit}</b> تقرير.\n💡 <i>راسل الإدارة لزيادة الحد إذا احتجت</i>",
        "en": "📊 <b>Monthly limit reached</b>\nUsage: <b>{used}/{limit}</b>.\n💡 <i>Contact admins to raise the limit if needed</i>",
        "ku": "📊 <b>گەیشتە سنووری مانگانە</b>\nبەکارهێنان: <b>{used}/{limit}</b>.\n💡 <i>پەیوەندی بکە بۆ زیادکردن ئەگەر پێویستە</i>",
        "ckb": "📊 <b>گەیشتە سنووری مانگانە</b>\nبەکارهێنان: <b>{used}/{limit}</b>.\n💡 <i>پەیوەندی بکە بۆ زیادکردن ئەگەر پێویستە</i>",
    },
    "low_balance": {
        "ar": "💳 <b>تنبيه الرصيد</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ رصيدك منخفض!\n\n• التقارير المتبقية هذا الشهر: <b>{monthly_left}</b>\n• تكلفة التقرير: <b>1</b>\n\n💡 <i>يرجى شحن رصيدك لتجنب انقطاع الخدمة</i>",
        "en": "💳 <b>Low balance</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ Your balance is low.\n\n• Reports left this month: <b>{monthly_left}</b>\n• Cost per report: <b>1</b>\n\n💡 <i>Please top up to avoid interruption</i>",
        "ku": "💳 <b>ئاگاداری رەوشنی مانگانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ رەوشن کەمە.\n\n• ڕاپۆرتی ماوەی ئەم مانگە: <b>{monthly_left}</b>\n• تێچووی هەر ڕاپۆرت: <b>1</b>\n\n💡 <i>تکایە پارەدان بکە بۆ بەردەوامی</i>",
        "ckb": "💳 <b>ئاگاداری رەوشنی مانگانە</b>\n\n━━━━━━━━━━━━━━━━━━━━\n⚠️ رەوشن کەمە.\n\n• ڕاپۆرتی ماوەی ئەم مانگە: <b>{monthly_left}</b>\n• تێچووی هەر ڕاپۆرت: <b>1</b>\n\n💡 <i>تکایە پارەدان بکە بۆ بەردەوامی</i>",
    },
    "activation_welcome": {
        "ar": "✅ <b>تم تفعيل حسابك</b>\n\n• تاريخ الانتهاء: <code>{expiry}</code>\n• التقارير المتبقية: <b>{monthly_left}</b>\n\n💡 أرسل رقم الشاصي (17 خانة) لتحصل على تقريرك فوراً.",
        "en": "✅ <b>Your account is now active</b>\n\n• Expiry: <code>{expiry}</code>\n• Reports left: <b>{monthly_left}</b>\n\n💡 Send the 17-char VIN to get your report instantly.",
        "ku": "✅ <b>ئەکاونتەکەت چالاک کرا</b>\n\n• کۆتایی: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 ژمارەی VIN ـەکەی ١٧ خانە بنێرە بۆ وەرگرتنی ڕاپۆرت.",
        "ckb": "✅ <b>ئەکاونتەکەت چالاک کرا</b>\n\n• کۆتایی: <code>{expiry}</code>\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n\n💡 ژمارەی VIN ـەکەی ١٧ خانە بنێرە بۆ وەرگرتنی ڕاپۆرت.",
    },
    "inactive_7": {
        "ar": "👋 <b>لم نرَ تقارير منذ {days} أيام</b>\n\n• التقارير المتبقية: <b>{monthly_left}</b>\n• تاريخ الانتهاء: <code>{expiry}</code>\n\n💡 أرسل رقم الشاصي أو راسل الدعم إن احتجت مساعدة.",
        "en": "👋 <b>We haven't seen reports for {days} days</b>\n\n• Reports left: <b>{monthly_left}</b>\n• Expiry: <code>{expiry}</code>\n\n💡 Send a VIN or reach support if you need help.",
        "ku": "👋 <b>لە {days} ڕۆژەوە ڕاپۆرت نەبینراوە</b>\n\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n• کۆتایی: <code>{expiry}</code>\n\n💡 VIN بنێرە یان پەیوەندی بکە ئەگەر یارمەتی دەوێت.",
        "ckb": "👋 <b>لە {days} ڕۆژەوە ڕاپۆرت نەبینراوە</b>\n\n• ڕاپۆرتی ماوە: <b>{monthly_left}</b>\n• کۆتایی: <code>{expiry}</code>\n\n💡 VIN بنێرە یان پەیوەندی بکە ئەگەر یارمەتی دەوێت.",
    },
    "inactive_14": {
        "ar": "⏳ <b>حسابك بلا نشاط منذ {days} يوم</b>\n\n• الرصيد الشهري المتبقي: <b>{monthly_left}</b>\n• ينتهي الاشتراك في: <code>{expiry}</code>\n\n💡 إن احتجت مساعدة، راسلنا لنفعّل لك الخيارات المناسبة.",
        "en": "⏳ <b>No activity for {days} days</b>\n\n• Monthly balance left: <b>{monthly_left}</b>\n• Subscription ends: <code>{expiry}</code>\n\n💡 Need help? Message us to get going again.",
        "ku": "⏳ <b>هیچ چالاکیەک نیە لە {days} ڕۆژەوە</b>\n\n• باڵانسی مانگانەی ماوە: <b>{monthly_left}</b>\n• بەسەرچوون: <code>{expiry}</code>\n\n💡 یارمەتی پێویستە؟ پەیوەندی بکە بۆ دەستپێکردنەوە.",
        "ckb": "⏳ <b>هیچ چالاکیەک نیە لە {days} ڕۆژەوە</b>\n\n• باڵانسی مانگانەی ماوە: <b>{monthly_left}</b>\n• بەسەرچوون: <code>{expiry}</code>\n\n💡 یارمەتی پێویستە؟ پەیوەندی بکە بۆ دەستپێکردنەوە.",
    },
}


def _user_language(user: Dict[str, Any]) -> str:
    lang = (user.get("language") or user.get("report_lang") or "ar").lower()
    try:
        if user.get("tg_id") and user.get("tg_id") in _db_super_admins(_load_db()):
            return "ar"
    except Exception:
        pass
    return lang


def _preferred_channel(user: Dict[str, Any]) -> Optional[str]:
    pref = (user.get("preferred_channel") or "").strip().lower()
    if pref in {"wa", "whatsapp"}:
        return "wa"
    if pref in {"tg", "telegram", "tele"}:
        return "tg"
    return None


def _t(key: str, lang: str, default: Optional[str] = None, **kwargs: Any) -> str:
    try:
        from bot_core import bridge as _bridge

        return _bridge.t(key, lang, **kwargs)
    except Exception:
        if default is None:
            return key
        try:
            return default.format(**kwargs)
        except Exception:
            return default


def _render_notice(key: str, lang: str, **kwargs: Any) -> str:
    templates = NOTIFY_TEMPLATES.get(key)
    if not templates:
        return key
    template = templates.get(lang) or templates.get("ar") or next(iter(templates.values()))
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def _clean_html_for_whatsapp(text: str) -> str:
    """Convert basic HTML tags to WhatsApp Markdown and strip others."""
    if not text:
        return ""
    # Bold
    text = text.replace("<b>", "*").replace("</b>", "*")
    text = text.replace("<strong>", "*").replace("</strong>", "*")
    # Italic
    text = text.replace("<i>", "_").replace("</i>", "_")
    text = text.replace("<em>", "_").replace("</em>", "_")
    # Monospace
    text = text.replace("<pre>", "```").replace("</pre>", "```")
    text = text.replace("<code>", "`").replace("</code>", "`")
    # Strike
    text = text.replace("<strike>", "~").replace("</strike>", "~")
    text = text.replace("<s>", "~").replace("</s>", "~")
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except Exception:
            continue
    return None


def _days_since(ts: Optional[str], today: date) -> Optional[int]:
    parsed = _parse_ts(ts)
    if not parsed:
        return None
    return (today - parsed.date()).days


def _is_quiet_hours(now_dt: datetime) -> bool:
    window = SMART_NOTIFY_RULES.get("quiet_hours") or {}
    start = window.get("start")
    end = window.get("end")
    if start is None or end is None:
        return False
    hour = now_dt.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _record_last(last_map: Dict[str, str], key: str, value: str) -> None:
    try:
        last_map[key] = value
    except Exception:
        pass


def _log_preview(kind: str, target: str, meta: Optional[Dict[str, Any]] = None, text: Optional[str] = None) -> None:
    try:
        snippet = (text or "").replace("\n", " ")
        if len(snippet) > 140:
            snippet = snippet[:140] + "…"
        LOGGER.info("smart_notify[%s] target=%s meta=%s preview=%s", kind, target, meta or {}, snippet)
    except Exception:
        LOGGER.debug("smart_notify preview logging failed", exc_info=True)


async def _dispatch_user_notification(
    context: ContextTypes.DEFAULT_TYPE,
    user: Dict[str, Any],
    text: str,
    *,
    kind: str,
    preferred_channel: Optional[str],
    log_only: bool,
    quiet_hours: bool,
) -> bool:
    tg_id = user.get("tg_id") or ""
    meta = {
        "tg_id": tg_id,
        "channel_pref": preferred_channel,
        "quiet": quiet_hours,
        "log_only": log_only,
    }
    _log_preview(kind, str(tg_id), meta, text)

    if quiet_hours and not log_only:
        LOGGER.info("smart_notify[%s] suppressed by quiet hours tg_id=%s", kind, tg_id)
        return False

    if log_only:
        return False

    try:
        return await notify_user(
            context,
            str(tg_id),
            text,
            preferred_channel=preferred_channel,
        )
    except Exception:
        LOGGER.exception("smart_notify[%s] failed to deliver tg_id=%s", kind, tg_id)
        return False


async def _dispatch_super_notification(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    kind: str,
    kb: Optional[InlineKeyboardMarkup] = None,
    log_only: bool,
) -> bool:
    _log_preview(kind, "supers", None, text)
    if log_only:
        return False
    try:
        await notify_supers(context, text, kb)
        return True
    except Exception:
        LOGGER.exception("smart_notify[%s] failed to notify supers", kind)
        return False


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infer_plan_days(user) -> int:
    activation = user.get("activation_date")
    expiry = user.get("expiry_date")
    if activation and expiry:
        try:
            act_d = datetime.strptime(activation, "%Y-%m-%d").date()
            exp_d = datetime.strptime(expiry, "%Y-%m-%d").date()
            span = (exp_d - act_d).days
            if span > 0:
                return span
        except Exception:
            pass
    plan = (user.get("plan") or "").lower()
    if plan == "trial":
        return 3
    if plan == "monthly":
        return 30
    return 30


def _renewal_admin_keyboard(tg_id: str, days: int, daily: int, monthly: int) -> InlineKeyboardMarkup:
    data_accept = f"renewal:auto:{tg_id}:{days}:{daily}:{monthly}"
    data_dismiss = f"renewal:dismiss:{tg_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 جدّد الخطة", callback_data=data_accept)],
        [InlineKeyboardButton("✋ تجاهل اليوم", callback_data=data_dismiss)],
    ])


async def notify_supers(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    kb: Optional[InlineKeyboardMarkup] = None,
) -> None:
    db = _load_db()
    targets = set(_env_super_admins()) | set(_db_super_admins(db))
    for admin_id in list(targets):
        try:
            admin_id_int = int(str(admin_id).lstrip("@"))
        except ValueError:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id_int,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            pass


async def notify_user(
    context: ContextTypes.DEFAULT_TYPE,
    target_tg: str,
    text: str,
    *,
    preferred_channel: Optional[str] = None,
) -> bool:
    # Check if target is likely a WhatsApp number (e.g. > 10 digits, starts with 962/966 etc)
    # Telegram IDs are usually 9-10 digits. Phone numbers with CC are usually 11-13.
    # A simple heuristic: if length > 10, treat as WhatsApp.

    target_clean = str(target_tg).strip()
    prefer_wa = (preferred_channel or "").lower() == "wa"
    prefer_tg = (preferred_channel or "").lower() == "tg"

    is_whatsapp = False
    normalized_numeric = target_clean.replace("+", "")
    if prefer_wa:
        is_whatsapp = True
    elif prefer_tg:
        is_whatsapp = False
    elif normalized_numeric.isdigit() and len(normalized_numeric) > 10:
        is_whatsapp = True

    if is_whatsapp:
        try:
            instance_id, token, base_url = get_ultramsg_settings()
            if instance_id and token:
                creds = UltraMsgCredentials(instance_id=instance_id, token=token, base_url=base_url)
                client = UltraMsgClient(creds)
                wa_text = _clean_html_for_whatsapp(text)
                wa_target = normalized_numeric if target_clean.startswith("+") else f"+{normalized_numeric}"
                await client.send_text(wa_target, wa_text)
                return True
        except Exception:
            LOGGER.exception("Failed WhatsApp send, will try Telegram fallback", exc_info=True)

    try:
        await context.bot.send_message(chat_id=int(target_tg), text=text, parse_mode=ParseMode.HTML)
        return True
    except Exception:
        LOGGER.exception("Failed to send Telegram message to %s", target_tg)
        return False


async def check_and_send_auto_notifications(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    dry_run: bool = False,
    log_only: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> None:
    """Validate balances/limits/expiry and auto-message users (TG + WhatsApp)."""

    effective_log_only = bool(log_only if log_only is not None else dry_run)
    now_dt = now or datetime.now()
    today = now_dt.date()
    today_str = today.strftime("%Y-%m-%d")
    month_key_today = today.strftime("%Y-%m")
    quiet_hours = _is_quiet_hours(now_dt)

    db = _load_db()
    users = list(db.get("users", {}).values())
    settings = db.setdefault("settings", {})
    notif_state = settings.setdefault("notification_state", {})

    digest: Dict[str, Any] = {
        "expiring": [],
        "expired": [],
        "low_balance": [],
        "limit_hits": [],
        "inactive": [],
    }

    for user in users:
        tg_id = user.get("tg_id")
        if not tg_id:
            continue

        exp = user.get("expiry_date")
        monthly_left = _remaining_monthly_reports(user)
        limits = user.get("limits", {})
        daily_used = _safe_int(limits.get("today_used"))
        daily_limit = _safe_int(limits.get("daily"), 200)
        monthly_used = _safe_int(limits.get("month_used"))
        monthly_limit = _safe_int(limits.get("monthly"), 500)
        last_notifications = user.setdefault("last_auto_notifications", {})

        lang = _user_language(user)
        preferred_channel = _preferred_channel(user)

        # Activation welcome (once per activation window)
        act_date_raw = user.get("activation_date")
        if user.get("is_active") and act_date_raw:
            try:
                act_dt = datetime.strptime(act_date_raw, "%Y-%m-%d").date()
                if (today - act_dt).days <= SMART_NOTIFY_RULES.get("activation_welcome_days", 3):
                    key = "activation_welcome"
                    if last_notifications.get(key) != today_str:
                        msg = _render_notice(
                            "activation_welcome",
                            lang,
                            expiry=_fmt_date(user.get("expiry_date")),
                            monthly_left=monthly_left if monthly_left is not None else "—",
                        )
                        await _dispatch_user_notification(
                            context,
                            user,
                            msg,
                            kind="welcome",
                            preferred_channel=preferred_channel,
                            log_only=effective_log_only,
                            quiet_hours=quiet_hours,
                        )
                        _record_last(last_notifications, key, today_str)
            except Exception:
                LOGGER.debug("Failed activation welcome check", exc_info=True)

        # Expiry ladder and status transitions
        days_left = None
        if exp:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                days_left = (exp_date - today).days
            except Exception:
                days_left = None

            if days_left is not None and days_left in SMART_NOTIFY_RULES.get("expiry_days", [14, 7, 3, 1, 0]):
                if days_left == 1:
                    user_key = "expiry_day_1"
                    if last_notifications.get(user_key) != today_str:
                        msg = _render_notice(
                            "expiry_day_1",
                            lang,
                            expiry=_fmt_date(exp),
                            monthly_left=monthly_left if monthly_left is not None else "—",
                        )
                        await _dispatch_user_notification(
                            context,
                            user,
                            msg,
                            kind="expiry:1",
                            preferred_channel=preferred_channel,
                            log_only=effective_log_only,
                            quiet_hours=quiet_hours,
                        )
                        _record_last(last_notifications, user_key, today_str)

                    admin_key = "expiry_admin_day1"
                    if last_notifications.get(admin_key) != today_str:
                        plan_days = _infer_plan_days(user)
                        kb = _renewal_admin_keyboard(
                            tg_id,
                            plan_days,
                            max(1, _safe_int(daily_limit, 25)),
                            max(1, _safe_int(monthly_limit, 50)),
                        )
                        super_msg = (
                            "⏳ <b>اشتراك ينتهي غدًا</b>\n"
                            f"• المستخدم: <b>{_display_name(user)}</b> ({_fmt_tg_with_phone(tg_id)})\n"
                            f"• الخطة الحالية: {plan_days} يوم | يومي {daily_limit} / شهري {monthly_limit}\n"
                            f"• ينتهي في: <code>{_fmt_date(exp)}</code>\n\n"
                            "🔁 اضغط زر التجديد لإعادة تفعيل نفس الخطة أو تجاهل اليوم."
                        )
                        await _dispatch_super_notification(
                            context,
                            super_msg,
                            kind="expiry:admin_day1",
                            kb=kb,
                            log_only=effective_log_only,
                        )
                        _record_last(last_notifications, admin_key, today_str)
                elif days_left >= 2:
                    last_key = f"expiry_{days_left}"
                    if last_notifications.get(last_key) != today_str:
                        msg = _render_notice(
                            "expiry_week",
                            lang,
                            days_left=days_left,
                            expiry=_fmt_date(exp),
                            monthly_left=monthly_left if monthly_left is not None else "—",
                        )
                        await _dispatch_user_notification(
                            context,
                            user,
                            msg,
                            kind=f"expiry:{days_left}",
                            preferred_channel=preferred_channel,
                            log_only=effective_log_only,
                            quiet_hours=quiet_hours,
                        )
                        _record_last(last_notifications, last_key, today_str)
                elif days_left == 0:
                    last_key = "expiry_0"
                    if last_notifications.get(last_key) != today_str:
                        msg = _render_notice("expiry_today", lang, expiry=_fmt_date(exp))
                        await _dispatch_user_notification(
                            context,
                            user,
                            msg,
                            kind="expiry:0",
                            preferred_channel=preferred_channel,
                            log_only=effective_log_only,
                            quiet_hours=quiet_hours,
                        )
                        _record_last(last_notifications, last_key, today_str)
                    if user.get("is_active"):
                        user["is_active"] = False
            if days_left is not None and days_left < 0:
                overdue_key = "expiry_overdue"
                if last_notifications.get(overdue_key) != today_str:
                    msg = _render_notice("expired", lang, days_over=abs(days_left), expiry=_fmt_date(exp))
                    await _dispatch_user_notification(
                        context,
                        user,
                        msg,
                        kind="expiry:overdue",
                        preferred_channel=preferred_channel,
                        log_only=effective_log_only,
                        quiet_hours=quiet_hours,
                    )
                    _record_last(last_notifications, overdue_key, today_str)
                if user.get("is_active"):
                    user["is_active"] = False

        # Record digest stats
        if days_left is not None:
            if 0 <= days_left <= 7:
                digest["expiring"].append((tg_id, days_left))
            if days_left < 0:
                digest["expired"].append((tg_id, abs(days_left)))

        if not user.get("is_active"):
            continue

        # Inactivity nudges
        inactivity_thresholds = SMART_NOTIFY_RULES.get("inactivity_days", [7, 14])
        last_report_days = _days_since(user.get("stats", {}).get("last_report_ts"), today)
        if last_report_days is not None and last_report_days >= min(inactivity_thresholds or [0]):
            for threshold in inactivity_thresholds:
                if last_report_days >= threshold:
                    key = f"inactive_{threshold}"
                    if last_notifications.get(key) != today_str:
                        template_key = "inactive_14" if threshold >= 14 else "inactive_7"
                        msg = _render_notice(
                            template_key,
                            lang,
                            days=last_report_days,
                            monthly_left=monthly_left if monthly_left is not None else "—",
                            expiry=_fmt_date(user.get("expiry_date")),
                        )
                        await _dispatch_user_notification(
                            context,
                            user,
                            msg,
                            kind=f"inactive:{threshold}",
                            preferred_channel=preferred_channel,
                            log_only=effective_log_only,
                            quiet_hours=quiet_hours,
                        )
                        _record_last(last_notifications, key, today_str)
                        digest["inactive"].append((tg_id, last_report_days))
                    break

        # Daily and monthly warnings/hits
        if daily_limit > 0 and daily_used >= daily_limit * 0.9:
            last_key = "daily_limit_warning"
            if last_notifications.get(last_key) != today_str:
                msg = _render_notice(
                    "daily_warn",
                    lang,
                    used=daily_used,
                    limit=daily_limit,
                    remaining=max(0, daily_limit - daily_used),
                )
                await _dispatch_user_notification(
                    context,
                    user,
                    msg,
                    kind="daily:warn",
                    preferred_channel=preferred_channel,
                    log_only=effective_log_only,
                    quiet_hours=quiet_hours,
                )
                _record_last(last_notifications, last_key, today_str)

        if monthly_limit > 0 and monthly_used >= monthly_limit * 0.9:
            last_key = "monthly_limit_warning"
            if last_notifications.get(last_key) != month_key_today:
                msg = _render_notice(
                    "monthly_warn",
                    lang,
                    used=monthly_used,
                    limit=monthly_limit,
                    remaining=max(0, monthly_limit - monthly_used),
                )
                await _dispatch_user_notification(
                    context,
                    user,
                    msg,
                    kind="monthly:warn",
                    preferred_channel=preferred_channel,
                    log_only=effective_log_only,
                    quiet_hours=quiet_hours,
                )
                _record_last(last_notifications, last_key, month_key_today)

        if daily_limit > 0 and daily_used >= daily_limit:
            hit_key = "daily_limit_hit"
            if last_notifications.get(hit_key) != today_str:
                msg = _render_notice(
                    "daily_hit",
                    lang,
                    used=daily_used,
                    limit=daily_limit,
                )
                await _dispatch_user_notification(
                    context,
                    user,
                    msg,
                    kind="daily:hit",
                    preferred_channel=preferred_channel,
                    log_only=effective_log_only,
                    quiet_hours=quiet_hours,
                )
                _record_last(last_notifications, hit_key, today_str)

                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            _t("limits.buttons.reset_today", "ar", "🔄 إعادة تعيين اليوم"),
                            callback_data=f"limits:reset_today:{tg_id}",
                        )
                    ]
                ])
                super_text = (
                    "📈 المستخدم بلغ الحد اليومي\n"
                    f"• {_fmt_tg_with_phone(tg_id)}\n"
                    f"• الاستخدام: {daily_used}/{daily_limit}"
                )
                await _dispatch_super_notification(
                    context,
                    super_text,
                    kind="daily:hit:super",
                    kb=kb,
                    log_only=effective_log_only,
                )
                digest["limit_hits"].append((tg_id, "daily", daily_used, daily_limit))

        if monthly_limit > 0 and monthly_used >= monthly_limit:
            hit_key = "monthly_limit_hit"
            if last_notifications.get(hit_key) != month_key_today:
                msg = _render_notice(
                    "monthly_hit",
                    lang,
                    used=monthly_used,
                    limit=monthly_limit,
                )
                await _dispatch_user_notification(
                    context,
                    user,
                    msg,
                    kind="monthly:hit",
                    preferred_channel=preferred_channel,
                    log_only=effective_log_only,
                    quiet_hours=quiet_hours,
                )
                _record_last(last_notifications, hit_key, month_key_today)
                digest["limit_hits"].append((tg_id, "monthly", monthly_used, monthly_limit))

        if monthly_left is not None and 0 < monthly_left <= SMART_NOTIFY_RULES.get("low_balance_threshold", 5):
            last_key = "low_balance"
            if last_notifications.get(last_key) != today_str:
                msg = _render_notice("low_balance", lang, monthly_left=monthly_left)
                await _dispatch_user_notification(
                    context,
                    user,
                    msg,
                    kind="balance:low",
                    preferred_channel=preferred_channel,
                    log_only=effective_log_only,
                    quiet_hours=quiet_hours,
                )
                _record_last(last_notifications, last_key, today_str)
                digest["low_balance"].append((tg_id, monthly_left))

    # Pending activation SLA pings to supers
    sla_minutes = SMART_NOTIFY_RULES.get("pending_sla_minutes", 20)
    pending_requests: List[Dict[str, Any]] = db.get("activation_requests", [])
    for req in pending_requests:
        try:
            req_ts = _parse_ts(req.get("ts"))
            if not req_ts:
                continue
            age_minutes = (now_dt - req_ts).total_seconds() / 60.0
            if age_minutes < sla_minutes:
                continue
            ping_key = f"pending_sla_{req.get('tg_id')}"
            if notif_state.get(ping_key) == today_str:
                continue
            tg_raw = str(req.get("tg_id"))
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧪 تجربة (1,25,25)", callback_data=f"ucard:trial:{tg_raw}")],
                [InlineKeyboardButton("🟢 شهري (30,25,500)", callback_data=f"ucard:monthly:{tg_raw}")],
                [InlineKeyboardButton("🔎 فتح البطاقة", callback_data=f"ucard:open:{tg_raw}")],
            ])
            msg = (
                "⏰ طلب تفعيل متأخر\n"
                f"• المستخدم: { _fmt_tg_with_phone(tg_raw) }\n"
                f"• رقم الهاتف: {req.get('phone') or '—'}\n"
                f"• العمر: {int(age_minutes)} دقيقة"
            )
            await _dispatch_super_notification(
                context,
                msg,
                kind="pending:sla",
                kb=kb,
                log_only=effective_log_only,
            )
            notif_state[ping_key] = today_str
        except Exception:
            LOGGER.exception("smart_notify pending SLA ping failed")

    # Daily digest for supers (Arabic only)
    digest_hour = SMART_NOTIFY_RULES.get("daily_digest_hour", 9)
    last_digest = notif_state.get("last_digest_date")
    if now_dt.hour >= digest_hour and last_digest != today_str:
        lines = [
            "🧾 <b>ملخص الإشعارات الذكية</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"• تنتهي قريباً (≤7 أيام): <b>{len(digest['expiring'])}</b>",
            f"• منتهية: <b>{len(digest['expired'])}</b>",
            f"• رصيد منخفض: <b>{len(digest['low_balance'])}</b>",
            f"• ضرب حدود (يومي/شهري): <b>{len(digest['limit_hits'])}</b>",
            f"• غير نشطين (٧/١٤ يوم): <b>{len(digest['inactive'])}</b>",
        ]
        preview_users = digest.get("expiring", [])[:3]
        if preview_users:
            extra = "\n".join(f"• {_fmt_tg_with_phone(tg)} (يتبقى {days} يوم)" for tg, days in preview_users)
            lines.append("\nأبرز من ينتهي قريباً:\n" + extra)
        digest_msg = "\n".join(lines)
        await _dispatch_super_notification(
            context,
            digest_msg,
            kind="digest:daily",
            kb=None,
            log_only=effective_log_only,
        )
        notif_state["last_digest_date"] = today_str

    _save_db(db)


# Backwards-compatible alias for legacy imports.
default_check_and_send_auto_notifications = check_and_send_auto_notifications