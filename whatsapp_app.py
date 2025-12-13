# type: ignore
# pyright: reportGeneralTypeIssues=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportPrivateUsage=false, reportConstantRedefinition=false, reportUnusedImport=false, reportUnusedFunction=false, reportUnnecessaryIsInstance=false, reportDeprecated=false
"""UltraMsg-powered WhatsApp entrypoint running alongside the Telegram bot."""
# FastAPI webhook served via uvicorn; start with `python whatsapp_app.py` (defaults to
# WHATSAPP_HOST=0.0.0.0 and WHATSAPP_PORT=5005) and tunnel `/whatsapp/webhook` with ngrok.

from __future__ import annotations

import asyncio
import sys

# Ensure ProactorEventLoop is used on Windows for Playwright compatibility
if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined, deprecated-call]

import base64
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import shutil
import httpx
from telegram import Bot

from bot_core import bridge as _bridge
from bot_core.clients.ultramsg import UltraMsgClient, UltraMsgCredentials, UltraMsgError
from bot_core.config import get_report_default_lang, get_ultramsg_settings, is_super_admin
from bot_core.services.images import (
    get_badvin_images,
    get_apicar_current_images,
    get_apicar_history_images,
    get_apicar_accident_images,
    download_image_bytes,
    _select_images,
)
from bot_core.storage import (
    ensure_user as _ensure_user, 
    load_db as _load_db, 
    save_db as _save_db,
    remaining_monthly_reports,
    days_left,
    now_str as _now_str,
    reserve_credit,
    refund_credit,
)
from bot_core.utils.vin import is_valid_vin, make_progress_bar

load_dotenv(override=True)

LOGGER = logging.getLogger(__name__)


def _coerce_port(raw: Optional[str], default: int) -> int:
    try:
        return int((raw or "").strip() or default)
    except ValueError:
        return default

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _reserve_report_slot(db: Dict[str, Any], user_id: str) -> None:
    u = _ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = _safe_int(limits.get("today_used")) + 1
    limits["month_used"] = _safe_int(limits.get("month_used")) + 1
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = stats.get("pending_reports", 0) + 1
    _save_db(db)

def _refund_report_slot(db: Dict[str, Any], user_id: str) -> None:
    u = _ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = max(0, _safe_int(limits.get("today_used")) - 1)
    limits["month_used"] = max(0, _safe_int(limits.get("month_used")) - 1)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    _save_db(db)

def _commit_report_success(db: Dict[str, Any], user_id: str) -> None:
    u = _ensure_user(db, user_id, None)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    stats["total_reports"] = stats.get("total_reports", 0) + 1
    stats["last_report_ts"] = _now_str()
    _save_db(db)

def _build_vin_progress_header(
    vin: str,
    monthly_remaining: Optional[int] = None,
    monthly_limit: int = 0,
    today_used: int = 0,
    daily_limit: int = 0,
    days_left: Optional[int] = None,
    *,
    language: Optional[str] = None,
) -> str:
    """Build the text header for VIN processing status (localized)."""

    lang = (language or "ar").lower()

    if monthly_remaining is None:
        balance_txt = _bridge.t("balance.unlimited", lang)
    else:
        balance_txt = f"{monthly_remaining}/{monthly_limit}"

    if days_left is None:
        expiry_txt = ""
    elif days_left > 0:
        expiry_txt = _bridge.t("wa.progress.expiry.remaining", lang, days=days_left)
    elif days_left == 0:
        expiry_txt = _bridge.t("wa.progress.expiry.today", lang)
    else:
        expiry_txt = _bridge.t("wa.progress.expiry.expired", lang)

    if daily_limit and daily_limit > 0:
        daily_line = _bridge.t("progress.vin.daily.remaining", lang, used=today_used, limit=daily_limit)
    else:
        daily_line = _bridge.t("progress.vin.daily.unlimited", lang, used=today_used)

    # Clean HTML for WhatsApp markdown to avoid raw tags like <b> showing up
    balance_txt = _clean_html_for_whatsapp(balance_txt)
    daily_line = _clean_html_for_whatsapp(daily_line)

    processing_label = _clean_html_for_whatsapp(_bridge.t("wa.progress.processing", lang))
    vin_label = _clean_html_for_whatsapp(_bridge.t("wa.progress.vin", lang, vin=vin, preserve_latin=True))
    balance_label = _clean_html_for_whatsapp(_bridge.t("wa.progress.balance", lang, balance=balance_txt))

    parts = [
        processing_label,
        vin_label,
        balance_label,
        daily_line,
    ]
    if expiry_txt:
        parts.append(_clean_html_for_whatsapp(f"📅 {expiry_txt}"))
    parts.append(_clean_html_for_whatsapp(_bridge.t("wa.progress.wait", lang)))

    return "\n\n".join([p for p in parts if p])

WHATSAPP_HOST = os.getenv("WHATSAPP_HOST", "0.0.0.0").strip() or "0.0.0.0"
WHATSAPP_PORT = _coerce_port(os.getenv("WHATSAPP_PORT"), 5005)
ULTRAMSG_INSTANCE_ID = os.getenv("ULTRAMSG_INSTANCE_ID", "").strip()
ULTRAMSG_TOKEN = os.getenv("ULTRAMSG_TOKEN", "").strip()
ULTRAMSG_BASE_URL = os.getenv("ULTRAMSG_BASE_URL", "https://api.ultramsg.com").strip() or "https://api.ultramsg.com"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

app = FastAPI(title="Carfax WhatsApp Bridge", version="1.0.0")

# Mount static directory for serving temporary files (PDFs)
STATIC_DIR = Path("temp_static")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

async def _get_ngrok_url() -> Optional[str]:
    """Attempt to fetch the public URL from local ngrok API."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:4040/api/tunnels")
            data = resp.json()
            tunnels = data.get("tunnels", [])
            for t in tunnels:
                if t.get("proto") == "https":
                    return t.get("public_url")
    except Exception:
        pass
    return None

_TELEGRAM_CONTEXT: Optional[SimpleNamespace] = None
_TELEGRAM_CONTEXT_WARNED = False

MENU_SHOW_KEYWORDS_BASE = {"/menu", "menu", "main menu", "mainmenu", "."}
MENU_SHOW_KEYWORDS = set(MENU_SHOW_KEYWORDS_BASE)
for _lang in ("ar", "en", "ku", "ckb"):
    _header = _bridge.t("menu.header", _lang)
    MENU_SHOW_KEYWORDS.add((_header or "").strip().lower())
    MENU_SHOW_KEYWORDS.add((_header or "").replace("🏠", "").strip().lower())

SUPPORTED_LANGS = {"ar", "en", "ku", "ckb"}

# Cache latest rendered menu items per user so we can map numeric replies to the
# same options the bridge exposed (keeps parity with Telegram menus).
LAST_MENU_ITEMS: Dict[str, List[Dict[str, Any]]] = {}


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


def _get_notification_context() -> Optional[SimpleNamespace]:
    global _TELEGRAM_CONTEXT, _TELEGRAM_CONTEXT_WARNED
    if _TELEGRAM_CONTEXT is not None:
        return _TELEGRAM_CONTEXT
    if not TELEGRAM_BOT_TOKEN:
        if not _TELEGRAM_CONTEXT_WARNED:
            LOGGER.warning(
                "TELEGRAM_BOT_TOKEN is not configured; WhatsApp activation alerts cannot notify Telegram super admins.",
            )
            _TELEGRAM_CONTEXT_WARNED = True
        return None
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    _TELEGRAM_CONTEXT = SimpleNamespace(bot=bot)
    return _TELEGRAM_CONTEXT


def _normalize_language_code(value: Optional[str]) -> str:
    candidate = (value or "").strip().lower()
    if candidate in SUPPORTED_LANGS:
        return candidate
    fallback = (get_report_default_lang() or "ar").strip().lower()
    return fallback if fallback in SUPPORTED_LANGS else "ar"


def _normalize_sender(raw: Any) -> Optional[str]:
    if not raw:
        return None
    sender = str(raw)
    if "@" in sender:
        sender = sender.split("@", 1)[0]
    return sender.strip() or None


def _normalize_recipient(raw: str) -> Optional[str]:
    candidate = (raw or "").strip()
    if "@" in candidate:
        candidate = candidate.split("@", 1)[0]
    candidate = candidate.replace(" ", "").replace("-", "")
    if candidate.startswith("00"):
        candidate = f"+{candidate[2:]}"
    if candidate and not candidate.startswith("+") and candidate.isdigit():
        candidate = f"+{candidate}"
    return candidate or None


def _detect_media_url(event: Dict[str, Any]) -> Optional[str]:
    for key in ("media", "mediaUrl", "file", "image", "document", "url"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _detect_message_type(event: Dict[str, Any]) -> str:
    msg_type = event.get("type") or event.get("message_type") or event.get("messageType")
    return str(msg_type or "chat").lower()


def _detect_media_filename(event: Dict[str, Any]) -> Optional[str]:
    for key in ("fileName", "filename", "name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    media_url = _detect_media_url(event)
    if media_url and "/" in media_url:
        candidate = media_url.rstrip("/").split("/")[-1]
        if candidate:
            return candidate
    return None

def _detect_media_mime(event: Dict[str, Any]) -> Optional[str]:
    for key in ("mimeType", "mime", "contentType"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_entries(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    data = payload.get("data") or payload.get("messages") or payload.get("entries")
    if isinstance(data, list) and data:
        for item in data:
            if isinstance(item, dict):
                merged = dict(item)
                merged.setdefault("event_type", payload.get("event_type") or payload.get("type"))
                yield merged
        return
    if isinstance(data, dict):
        merged_dict = dict(data)
        merged_dict.setdefault("event_type", payload.get("event_type") or payload.get("type"))
        yield merged_dict
        return
    if isinstance(payload, dict):
        yield payload


def _build_user_context(sender: str, event: Dict[str, Any]) -> _bridge.UserContext:
    db = _load_db()
    user = _ensure_user(db, sender, None)
    if not user.get("phone"):
        user["phone"] = sender
        _save_db(db)
    lang_candidate = (
        user.get("report_lang")
        or user.get("language")
        or user.get("lang")
        or event.get("language")
        or event.get("languageCode")
        or event.get("lang")
        or get_report_default_lang()
    )
    language = _normalize_language_code(lang_candidate)

    # Super admin UX stays Arabic only (policy: admin panels remain Arabic).
    try:
        if is_super_admin(sender) or is_super_admin(user.get("tg_id")):
            language = "ar"
    except Exception:
        pass
    if user.get("language") != language or user.get("report_lang") != language:
        user["language"] = language
        user["report_lang"] = language
        _save_db(db)
    metadata = {
        "platform": "whatsapp",
        "event": event,
        "db_user": user,
        "sender_name": event.get("senderName") or event.get("authorName"),
    }
    return _bridge.UserContext(
        user_id=sender,
        phone=user.get("phone") or sender,
        language=language,
        state=user.get("state"),
        metadata=metadata,
    )


def _is_menu_selection_candidate(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.isdigit():
        return False
    if len(stripped) > 2:
        return False
    try:
        value = int(stripped)
    except ValueError:
        return False
    return 1 <= value <= 99


def _update_user_state(user_id: str, state: Optional[str]) -> None:
    db = _load_db()
    _ensure_user(db, user_id, None)
    if state:
        db["users"][user_id]["state"] = state
    else:
        db["users"][user_id].pop("state", None)
    from bot_core.storage import save_db
    save_db(db)


def _update_user_lang(user_id: str, lang: str) -> None:
    db = _load_db()
    _ensure_user(db, user_id, None)
    normalized = _normalize_language_code(lang)
    db["users"][user_id]["report_lang"] = normalized
    db["users"][user_id]["language"] = normalized
    db["users"][user_id]["lang"] = normalized
    from bot_core.storage import save_db
    save_db(db)


def _update_user_activation_cc(user_id: str, cc: str) -> None:
    _update_user_state(user_id, "activation")


async def send_whatsapp_list(
    to: str,
    body: str,
    button_text: str,
    sections: List[Dict[str, Any]],
    title: Optional[str] = None,
    footer: Optional[str] = None,
    client: Optional[UltraMsgClient] = None,
) -> Dict[str, Any]:
    """Send a WhatsApp interactive list message via UltraMsg with text fallback."""
    recipient = _normalize_recipient(to)
    if not recipient:
        raise UltraMsgError("Recipient phone number is missing or invalid.")
    
    active_client = client or _build_client()
    
    try:
        LOGGER.info("📤 Sending WhatsApp list to %s", recipient)
        if hasattr(active_client, "send_list"):
             return await active_client.send_list(recipient, body, button_text, sections, title=title, footer=footer)
    except Exception as e:
        LOGGER.warning("Failed to send interactive list (fallback to text): %s", e)
    
    # Fallback to text
    lines = []
    if title:
        lines.append(f"*{title}*")
    lines.append(body)
    lines.append("")
    
    idx = 1
    for section in sections:
        sec_title = section.get("title")
        if sec_title:
            lines.append(f"*{sec_title}*")
        for row in section.get("rows", []):
            lines.append(f"{idx}. {row.get('title')}")
            idx += 1
        lines.append("")
    
    if footer:
        lines.append(f"_{footer}_")
        
    text_body = "\n".join(lines).strip()
    return await send_whatsapp_text(to, text_body, client=active_client)


async def send_whatsapp_buttons(
    to: str,
    body: str,
    buttons: List[Dict[str, str]],
    *,
    client: Optional[UltraMsgClient] = None,
    footer: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a WhatsApp interactive button message via UltraMsg with text fallback."""
    recipient = _normalize_recipient(to)
    if not recipient:
        raise UltraMsgError("Recipient phone number is missing or invalid.")

    active_client = client or _build_client()
    
    try:
        LOGGER.info("📤 Sending WhatsApp buttons to %s", recipient)
        if hasattr(active_client, "send_buttons"):
             return await active_client.send_buttons(recipient, body, buttons, footer=footer)
    except Exception as e:
        LOGGER.warning("Failed to send interactive buttons (fallback to text): %s", e)
        
    # Fallback
    lines = [body, ""]
    for i, btn in enumerate(buttons, 1):
        label = btn.get("title") or btn.get("label") or "Option"
        lines.append(f"{i}. {label}")
    
    if footer:
        lines.append(f"\n_{footer}_")
        
    return await send_whatsapp_text(to, "\n".join(lines), client=active_client)


async def _send_bridge_menu(
    to: str,
    user_ctx: _bridge.UserContext,
    client: UltraMsgClient,
    *,
    resp: Optional[_bridge.BridgeResponse] = None,
):
    """Render the unified bridge menu and send it as a WhatsApp list message."""

    _update_user_state(user_ctx.user_id, None)

    resp = resp or await _bridge.render_main_menu(user_ctx)
    menu_items = (resp.actions.get("menu") or {}).get("items", [])
    # Cache items so numeric replies map to the same ordering
    LAST_MENU_ITEMS[user_ctx.user_id] = menu_items

    if not menu_items:
        return

    # Build a lightweight WhatsApp-specific menu text (header + instruction only)
    # to avoid rendering the options twice; the list rows already contain them.
    header = _bridge.t("menu.header", user_ctx.language)
    instructions = _bridge.t("menu.instructions", user_ctx.language)
    body_text = f"{header}\n{instructions}"

    rows = []
    for item in menu_items:
        rows.append({
            "id": f"menu:{item['id']}",
            "title": item.get("label") or item.get("id", ""),
            "description": item.get("description")
        })

    section_title = _bridge.t("menu.header", user_ctx.language)
    sections = [{"title": section_title, "rows": rows}]

    await send_whatsapp_list(
        to,
        body=body_text,
        button_text=section_title,
        sections=sections,
        client=client,
    )


async def _resolve_menu_selection(token: str, user_ctx: _bridge.UserContext) -> Optional[str]:
    """Map a button/list token or numeric reply to a bridge menu entry id."""

    if not token:
        return None

    normalized = token.strip().lower()
    if normalized.startswith("menu:"):
        return normalized.split(":", 1)[1]

    # Ensure we have the latest menu items for this user
    items = LAST_MENU_ITEMS.get(user_ctx.user_id)
    if items is None:
        resp = await _bridge.render_main_menu(user_ctx)
        items = (resp.actions.get("menu") or {}).get("items", [])
        LAST_MENU_ITEMS[user_ctx.user_id] = items

    if normalized.isdigit():
        idx = int(normalized) - 1
        if 0 <= idx < len(items):
            return str(items[idx]["id"])

    for item in items:
        label = str(item.get("label") or "").lower()
        item_id = str(item.get("id") or "").lower()
        if normalized == label or normalized == item_id:
            return str(item.get("id"))
    return None


def _apply_bridge_actions_to_state(user_id: str, resp: Any) -> None:
    """Sync bridge actions that affect user state back to persistent storage."""

    if not isinstance(resp, _bridge.BridgeResponse):
        return
    actions = resp.actions or {}
    if actions.get("clear_activation_state"):
        _update_user_state(user_id, None)
    elif actions.get("await_activation_phone"):
        _update_user_state(user_id, "activation_phone")
    if actions.get("await_language_choice"):
        _update_user_state(user_id, "language_choice")
    if actions.get("clear_state"):
        _update_user_state(user_id, None)


BROADCAST_DRAFTS: Dict[str, Dict[str, Any]] = {}


async def _send_broadcast_menu(to: str, user_id: str, client: UltraMsgClient):
    _update_user_state(user_id, "menu_broadcast")

    db = _load_db()
    user = _ensure_user(db, user_id, None)
    lang = (user.get("language") or user.get("report_lang") or "ar").lower()

    buttons = [
        {"id": "wa_broadcast_all", "title": _bridge.t("wa.broadcast.button.all", lang)},
        {"id": "wa_broadcast_specific", "title": _bridge.t("wa.broadcast.button.user", lang)},
        {"id": "wa_cancel", "title": _bridge.t("wa.broadcast.button.cancel", lang)}
    ]

    await send_whatsapp_buttons(
        to,
        body=_bridge.t("wa.broadcast.prompt", lang),
        buttons=buttons,
        client=client
    )


def _report_options_prompt(lang: str, vin: Optional[str] = None) -> str:
    return _bridge.t("wa.photos.prompt", lang)


def _photo_no_images_message(lang: str, choice: Optional[str] = None) -> str:
    if choice == "wa_opt_accident":
        return _bridge.t("wa.photos.none.accident", lang)
    return _bridge.t("wa.photos.none.generic", lang)


def _photo_send_error_message(lang: str) -> str:
    return _bridge.t("wa.photos.send_error", lang)


def _photo_fetch_error_message(lang: str, choice: Optional[str] = None) -> str:
    if choice == "wa_opt_accident":
        return _bridge.t("wa.photos.fetch_error.accident", lang)
    return _bridge.t("wa.photos.fetch_error.generic", lang)


async def _send_report_options_prompt(to: str, client: UltraMsgClient, vin: Optional[str] = None) -> None:
    db = _load_db()
    user = _ensure_user(db, to, None)
    lang = (user.get("language") or user.get("report_lang") or "ar").lower()
    await send_whatsapp_text(to, _report_options_prompt(lang, vin), client=client)


async def _send_report_options_menu(to: str, user_id: str, vin: str, client: UltraMsgClient):
    LOGGER.info("whatsapp: entering report_options flow vin=%s user=%s", vin, user_id)
    _update_user_state(user_id, f"report_options:{vin}")
    
    db = _load_db()
    user = _ensure_user(db, user_id, None)
    lang = (user.get("language") or user.get("report_lang") or "ar").lower()

    buttons = [
        {"id": "wa_opt_accident", "title": _bridge.t("wa.photos.option.accident", lang)},
        {"id": "wa_opt_badvin", "title": _bridge.t("wa.photos.option.hidden", lang)},
    ]
    
    body_text = _report_options_prompt(lang)
    
    await send_whatsapp_buttons(
        to,
        body=body_text,
        buttons=buttons,
        footer=_bridge.t("wa.footer.brand", lang),
        client=client
    )


async def _send_photo_batch(
    msisdn: str,
    user_ctx: _bridge.UserContext,
    vin: str,
    urls: List[str],
    client: UltraMsgClient,
    *,
    choice: str,
) -> Dict[str, Any]:
    cleaned = _select_images(urls, limit=10)
    LOGGER.info(
        "whatsapp: photos fetched choice=%s vin=%s total_urls=%s cleaned=%s",
        choice,
        vin,
        len(urls),
        len(cleaned),
    )

    if not cleaned:
        await send_whatsapp_text(
            msisdn,
            _photo_no_images_message(user_ctx.language, choice),
            client=client,
        )
        await _send_report_options_prompt(msisdn, client, vin)
        return {"status": "ok", "images": 0, "empty": True}

    sent = 0
    failed_urls: List[str] = []
    for url in cleaned:
        payload: Dict[str, Any] = {}

        try:
            await client.send_image(msisdn, image_url=url, **payload)
            sent += 1
            continue
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.debug(
                "whatsapp: send_image via url failed vin=%s choice=%s url=%s error=%s",
                vin,
                choice,
                url,
                exc,
            )

        data = await download_image_bytes(url)
        if not data:
            failed_urls.append(url)
            LOGGER.warning(
                "whatsapp: failed to download image vin=%s choice=%s url=%s",
                vin,
                choice,
                url,
            )
            continue

        try:
            b64 = base64.b64encode(data).decode("ascii")
            await client.send_image(msisdn, image_base64=b64, **payload)
            sent += 1
        except Exception as exc:  # pragma: no cover - network dependent
            failed_urls.append(url)
            LOGGER.warning(
                "whatsapp: failed to send image_base64 vin=%s choice=%s url=%s error=%s",
                vin,
                choice,
                url,
                exc,
            )

    LOGGER.info(
        "whatsapp: photo send summary vin=%s choice=%s sent=%s failed=%s total=%s",
        vin,
        choice,
        sent,
        len(failed_urls),
        len(cleaned),
    )

    if sent > 0:
        await send_whatsapp_text(msisdn, _bridge.t("wa.photos.sent_count", user_ctx.language, count=sent), client=client)
        await _send_report_options_prompt(msisdn, client, vin)
        return {"status": "ok", "images": sent}

    # If nothing was sent, notify the user without sending raw URLs.
    LOGGER.warning(
        "whatsapp: all image sends failed vin=%s choice=%s total=%s failed=%s",
        vin,
        choice,
        len(cleaned),
        len(failed_urls),
    )
    await send_whatsapp_text(msisdn, _photo_send_error_message(user_ctx.language), client=client)
    await _send_report_options_prompt(msisdn, client, vin)
    return {"status": "ok", "images": 0, "failed": len(failed_urls) or len(cleaned)}


async def _handle_report_option_choice(
    msisdn: str,
    user_ctx: _bridge.UserContext,
    choice: str,
    vin: str,
    client: UltraMsgClient,
) -> Dict[str, Any]:
    """Fetch and send the chosen photo bundle (badvin/accident/auction)."""

    fetchers = {
        "wa_opt_accident": get_apicar_accident_images,
        "wa_opt_badvin": get_badvin_images,
    }
    fetcher = fetchers.get(choice)
    if not fetcher:
        LOGGER.warning("whatsapp: unknown photo choice=%s vin=%s", choice, vin)
        await _send_report_options_prompt(msisdn, client, vin)
        return {"status": "ignored", "reason": "unknown_choice"}

    LOGGER.info("whatsapp: report photos choice=%s vin=%s user=%s", choice, vin, user_ctx.user_id)
    await send_whatsapp_text(msisdn, _bridge.t("wa.photos.fetching", user_ctx.language, vin=vin), client=client)

    try:
        if choice == "wa_opt_accident":
            LOGGER.info("whatsapp: fetching accident images via apicar vin=%s", vin)
        urls = await fetcher(vin)
    except Exception as exc:  # pragma: no cover - network dependent
        LOGGER.warning("Failed to fetch images for choice=%s vin=%s: %s", choice, vin, exc)
        await send_whatsapp_text(msisdn, _photo_fetch_error_message(user_ctx.language, choice), client=client)
        await _send_report_options_prompt(msisdn, client, vin)
        return {"status": "error", "reason": "fetch_failed"}

    return await _send_photo_batch(msisdn, user_ctx, vin, urls or [], client, choice=choice)

def _map_text_to_button(text: str, state: Optional[str], is_admin: bool) -> Optional[str]:
    if not text.isdigit():
        return None
    idx = int(text)
    
    if state is None:
        return None
        
    if state == "menu_activation":
        mapping = {
            1: "wa_cc_962", 2: "wa_cc_966", 3: "wa_cc_971",
            4: "wa_cc_964", 5: "wa_cc_20", 6: "wa_cc_other",
            7: "wa_cancel"
        }
        return mapping.get(idx)
        
    if state == "menu_lang":
        mapping = {
            1: "wa_lang_ar", 2: "wa_lang_en", 3: "wa_lang_ku", 4: "wa_lang_ckb",
            5: "wa_cancel"
        }
        return mapping.get(idx)
        
    if state == "menu_support":
        mapping = {
            1: "wa_help_whatsapp", 2: "wa_help_site", 3: "wa_help_faq",
            4: "wa_cancel"
        }
        return mapping.get(idx)
        
    if state == "menu_broadcast":
        mapping = {1: "wa_broadcast_all", 2: "wa_broadcast_specific", 3: "wa_cancel"}
        return mapping.get(idx)
        
    if state and state.startswith("report_options:"):
        mapping = {1: "wa_opt_accident", 2: "wa_opt_badvin", 0: "menu:main"}
        return mapping.get(idx)
        
    return None

async def handle_incoming_whatsapp_message(
    event: Dict[str, Any],
    client: UltraMsgClient,
    *,
    event_type: Optional[str] = None,
) -> Dict[str, Any]:
    # Debug trace for incoming state/text
    LOGGER.debug("whatsapp inbound raw event=%s", event)
    normalized_event_type = (event_type or str(event.get("event_type") or "")).strip().lower()
    if normalized_event_type and normalized_event_type != "message_received":
        LOGGER.debug("Skipping webhook: unsupported event_type=%s", normalized_event_type)
        return {"status": "ignored", "reason": f"event_type:{normalized_event_type or 'unknown'}"}

    msg_category = str(event.get("type") or "").strip().lower()
    
    # Handle interactive button replies
    button_id = None
    if msg_category == "interactive":
        interactive = event.get("interactive") or {}
        int_type = interactive.get("type")
        if int_type == "button_reply":
            button_reply = interactive.get("button_reply") or {}
            button_id = button_reply.get("id")
            LOGGER.info("🔘 Button clicked: %s", button_id)
        elif int_type == "list_reply":
            list_reply = interactive.get("list_reply") or {}
            button_id = list_reply.get("id")
            LOGGER.info("📜 List item selected: %s", button_id)
    
    if msg_category and msg_category not in ("chat", "interactive"):
        LOGGER.debug("Skipping webhook: unsupported message type=%s", msg_category)
        return {"status": "ignored", "reason": f"type:{msg_category or 'unknown'}"}

    from_me_flag = event.get("fromMe")
    if isinstance(from_me_flag, str):
        from_me_flag = from_me_flag.strip().lower() == "true"
    if from_me_flag:
        LOGGER.debug("Skipping webhook: message originated from our account (jid=%s)", event.get("from"))
        return {"status": "ignored", "reason": "from_me"}

    raw_sender = event.get("from") or event.get("chatId") or event.get("author")
    if not raw_sender:
        LOGGER.warning("Skipping webhook: no 'from' JID in payload: %s", event)
        return {"status": "ignored", "reason": "missing_from"}

    bridge_sender = _normalize_sender(raw_sender)
    msisdn = _normalize_recipient(raw_sender) or bridge_sender
    if not bridge_sender or not msisdn:
        LOGGER.warning("Skipping webhook: unable to normalize sender JID=%s", raw_sender)
        return {"status": "ignored", "reason": "invalid_sender"}

    text_body = (event.get("body") or event.get("text") or "").strip()
    LOGGER.debug("whatsapp inbound normalized text='%s'", text_body)
    
    # If it's a button click, we might not have body text, or we might want to use the ID
    if button_id:
        text_body = f"BUTTON:{button_id}"

    if not text_body and not button_id:
        LOGGER.debug("Skipping webhook: empty body from %s", msisdn)
        return {"status": "ignored", "reason": "empty_body"}

    LOGGER.info("📩 Incoming WhatsApp from %s: %s", msisdn, text_body)

    enriched_event = dict(event)
    enriched_event.setdefault("sender", bridge_sender)
    telegram_context = _get_notification_context()

    msg_type = _detect_message_type(enriched_event)
    media_url = _detect_media_url(enriched_event)
    user_ctx = _build_user_context(bridge_sender, enriched_event)
    report_vin = None
    if (user_ctx.state or "").startswith("report_options:"):
        report_vin = (user_ctx.state or "").split(":", 1)[1] or None
    LOGGER.debug("whatsapp inbound state=%s vin_state=%s", user_ctx.state, report_vin)
    pre_reserved_credit = False

    if (user_ctx.state or "").startswith("report_options"):
        LOGGER.debug("whatsapp: entering photo-options handler (vin=%s text=%s)", report_vin, text_body)

    # Map text fallback to button_id (non-main-menu flows only; main menu handled via bridge menu items)
    state_lower = (user_ctx.state or "").lower()
    if state_lower == "language_choice":
        LOGGER.debug("whatsapp: in language_choice flow, skip button text mapping")
    elif not button_id and text_body and text_body.isdigit():
        mapped_id = _map_text_to_button(text_body, user_ctx.state, is_super_admin(user_ctx.user_id))
        if mapped_id:
            button_id = mapped_id
            LOGGER.info("🔀 Mapped text '%s' to button_id '%s' (state=%s)", text_body, button_id, user_ctx.state)

    incoming = _bridge.IncomingMessage(
        platform="whatsapp",
        user_id=user_ctx.user_id,
        text=text_body or None,
        media_url=media_url,
        caption=text_body or None,
        file_name=_detect_media_filename(enriched_event),
        mime_type=_detect_media_mime(enriched_event),
        raw=enriched_event,
    )

    response_batches: List[Any] = []
    bridge_kwargs: Dict[str, Any] = {}
    if telegram_context is not None:
        bridge_kwargs["context"] = telegram_context

    # --- Unified handling using the bridge ---

    menu_selection_text: Optional[str] = None

    # Language-choice state has priority: digits here mean language selection only.
    language_choice_handled = False
    manual_texts: List[str] = []
    manual_send_menu = False

    if state_lower == "language_choice":
        LOGGER.debug("whatsapp: entering language_choice handler (state=%s, text=%s)", state_lower, text_body)
        if text_body and text_body.isdigit():
            choice = text_body.strip()
            lang_map = {"1": "ar", "2": "en", "3": "ku", "4": "ckb"}
            selected_lang = lang_map.get(choice)
            if selected_lang:
                LOGGER.info("whatsapp: handling language choice %s -> %s for user %s", choice, selected_lang, user_ctx.user_id)
                _update_user_lang(user_ctx.user_id, selected_lang)
                _update_user_state(user_ctx.user_id, None)
                user_ctx.language = selected_lang
                manual_texts.append(_bridge.t("wa.language.updated", selected_lang))
                manual_send_menu = True
                LOGGER.debug("whatsapp: menu will be rebuilt after language update in lang=%s", selected_lang)
            else:
                manual_texts.append(_bridge.t("wa.language.invalid_choice", user_ctx.language))
            language_choice_handled = True
        else:
            language_choice_handled = True  # Ignore other inputs inside language flow (no menu fallback)

    report_option_choice: Optional[str] = None
    exit_to_main_menu = False

    if language_choice_handled:
        menu_selection_text = None
    elif button_id:
        if button_id == "menu:main":
            exit_to_main_menu = True
        elif button_id.startswith("wa_opt_") and report_vin:
            report_option_choice = button_id
        else:
            tmp = _resolve_menu_selection(button_id, user_ctx)
            menu_selection_text = await tmp if asyncio.iscoroutine(tmp) else tmp
    elif text_body and text_body.isdigit():
        if state_lower.startswith("report_options"):
            mapped_id = _map_text_to_button(text_body, user_ctx.state, is_super_admin(user_ctx.user_id))
            LOGGER.debug(
                "whatsapp: report_options digit input=%s mapped_id=%s state=%s vin=%s",
                text_body,
                mapped_id,
                user_ctx.state,
                report_vin,
            )
            if mapped_id == "menu:main":
                exit_to_main_menu = True
            elif mapped_id:
                report_option_choice = mapped_id
                LOGGER.info("whatsapp: mapped digit %s to report option %s (vin=%s)", text_body, mapped_id, report_vin)
        elif state_lower in {None, "", "main_menu"}:
            tmp = _resolve_menu_selection(text_body, user_ctx)
            mapped = await tmp if asyncio.iscoroutine(tmp) else tmp
            if mapped:
                LOGGER.info("whatsapp: handling main-menu choice %s for user %s", text_body, user_ctx.user_id)
                menu_selection_text = mapped
        else:
            LOGGER.debug("whatsapp: digit '%s' ignored because state=%s (flow active)", text_body, state_lower)

    if exit_to_main_menu:
        LOGGER.info("whatsapp: exiting photo flow to main menu (state=%s vin=%s)", user_ctx.state, report_vin)
        _update_user_state(user_ctx.user_id, None)
        await _send_bridge_menu(msisdn, user_ctx, client)
        return {"status": "ok", "responses": 1}

    if report_option_choice and report_vin:
        return await _handle_report_option_choice(msisdn, user_ctx, report_option_choice, report_vin, client)

    if menu_selection_text:
        selection_msg = _bridge.IncomingMessage(
            platform="whatsapp",
            user_id=user_ctx.user_id,
            text=menu_selection_text,
            raw=event,
        )
        resp_candidate = _bridge.handle_menu_selection(user_ctx, selection_msg, **bridge_kwargs)
        resp = await resp_candidate if asyncio.iscoroutine(resp_candidate) else resp_candidate
        _apply_bridge_actions_to_state(bridge_sender, resp)
        response_batches.append(resp)

    elif msg_type in {"image", "document", "video", "audio", "ptt"} or media_url:
        resp = await _bridge.handle_photo(user_ctx, incoming, **bridge_kwargs)
        _apply_bridge_actions_to_state(bridge_sender, resp)
        response_batches.append(resp)

    else:
        lower_text = (text_body or "").strip().lower()

        # Guard: photo-options flow (report_options)
        if state_lower.startswith("report_options"):
            LOGGER.debug("whatsapp: photo-options active vin=%s text=%s", report_vin, text_body)
            if text_body and is_valid_vin(text_body):
                LOGGER.info("whatsapp: recognized VIN inside photo flow -> restart report (vin=%s)", text_body)
                _update_user_state(user_ctx.user_id, None)
                user_ctx.state = None
            elif lower_text in {"1", "2"}:
                LOGGER.debug("whatsapp: photo option digit will be handled upstream (text=%s)", lower_text)
                # Do nothing here; mapping/handlers already covered above.
            else:
                LOGGER.info(
                    "whatsapp: invalid input in photo flow -> main menu (input=%s state=%s)",
                    lower_text,
                    state_lower,
                )
                _update_user_state(user_ctx.user_id, None)
                await _send_bridge_menu(msisdn, user_ctx, client)
                return {"status": "ok", "responses": 1}
        else:
            # Show main menu on demand (dot and menu keywords already included)
            if lower_text in MENU_SHOW_KEYWORDS or lower_text == "0":
                if state_lower in {None, "", "main_menu"} or lower_text == ".":
                    if lower_text == "." and state_lower not in {None, "", "main_menu"}:
                        LOGGER.debug("whatsapp: dot cancel clears active flow state=%s", state_lower)
                    _update_user_state(user_ctx.user_id, None)
                    LOGGER.debug("whatsapp: explicit menu request, sending menu (state cleared)")
                    await _send_bridge_menu(msisdn, user_ctx, client)
                    return {"status": "ok", "responses": 1}
                else:
                    LOGGER.debug("whatsapp: skipping fallback menu because state=%s is active", state_lower)

        # If user sent a VIN directly، أرسل رسالة تقدم فورية (مثل تلغرام) بالأرقام الحالية
        if text_body and is_valid_vin(text_body):
            vin_clean = text_body.strip().upper()
            db_snapshot = _load_db()
            user_record = db_snapshot.get("users", {}).get(user_ctx.user_id, {}) or {}
            limits = user_record.get("limits", {})
            monthly_limit = _safe_int(limits.get("monthly"))
            monthly_remaining = remaining_monthly_reports(user_record)
            daily_limit = _safe_int(limits.get("daily"))
            daily_used = _safe_int(limits.get("today_used"))
            expiry_days = days_left(user_record.get("expiry_date"))

            # Guard: inactive or expired accounts get blocked immediately (no progress message)
            if not user_record.get("is_active"):
                await send_whatsapp_text(msisdn, _bridge.t("account.inactive", user_ctx.language), client=client)
                return {"status": "ok", "responses": 1}

            if expiry_days is not None and expiry_days <= 0:
                expiry_label = user_record.get("expiry_date") or "-"
                await send_whatsapp_text(
                    msisdn,
                    _bridge.t("account.inactive.expired", user_ctx.language, expiry=expiry_label),
                    client=client,
                )
                return {"status": "ok", "responses": 1}

            if not (user_record.get("services", {}) or {}).get("carfax", True):
                await send_whatsapp_text(msisdn, _bridge.t("service.carfax.disabled", user_ctx.language), client=client)
                return {"status": "ok", "responses": 1}

            # Reserve credit immediately on VIN receipt; downstream handler will commit/refund
            try:
                reserve_credit(user_ctx.user_id)
                pre_reserved_credit = True
                LOGGER.info("whatsapp: credit reserved on receipt for vin=%s user=%s", vin_clean, user_ctx.user_id)
            except Exception as exc:
                LOGGER.exception("whatsapp: failed to reserve credit vin=%s user=%s", vin_clean, user_ctx.user_id)

            progress_msg = _build_vin_progress_header(
                vin_clean,
                monthly_remaining=monthly_remaining,
                monthly_limit=monthly_limit,
                today_used=daily_used,
                daily_limit=daily_limit,
                days_left=expiry_days,
                language=user_ctx.language,
            )
            await send_whatsapp_text(msisdn, progress_msg, client=client)

        try:
            resp = await _bridge.handle_text(
                user_ctx,
                incoming,
                skip_limit_validation=False,
                deduct_credit=True,
                pre_reserved_credit=pre_reserved_credit,
                **bridge_kwargs,
            )
        except Exception:
            if pre_reserved_credit:
                try:
                    refund_credit(user_ctx.user_id)
                    LOGGER.info("whatsapp: refunded pre-reserved credit after handler error user=%s", user_ctx.user_id)
                except Exception:
                    LOGGER.exception("whatsapp: failed to refund credit after handler error user=%s", user_ctx.user_id)
            raise
        _apply_bridge_actions_to_state(bridge_sender, resp)
        response_batches.append(resp)

    # --- Process Bridge Responses ---
    text_payloads: List[str] = []
    documents: List[Dict[str, Any]] = []
    media_payloads: List[Dict[str, Any]] = []
    temp_files: List[str] = []
    photo_tasks: List[asyncio.Task[Any]] = []

    def _extend_payloads(resp: Any) -> None:
        if not resp:
            return
        if isinstance(resp, _bridge.BridgeResponse):
            # If this response is a menu, avoid pushing its text to prevent duplicate menus; let _send_bridge_menu handle it.
            is_menu_resp = bool(resp.actions.get("menu"))
            is_lang_prompt = bool(resp.actions.get("await_language_choice"))
            is_pure_menu = bool(resp.actions.get("menu_only"))
            # Keep language prompts and any non-menu payloads; skip only pure menu bodies to avoid duplication.
            if not (is_menu_resp and is_pure_menu and not is_lang_prompt):
                for msg in resp.messages:
                    text_payloads.append(msg)
            documents.extend(resp.documents)
            media_payloads.extend(resp.media)
            temp_files.extend(resp.actions.get("temp_files", []))
        else:
            if isinstance(resp, (list, tuple, set)):
                for item in resp:
                    if item:
                        text_payloads.append(str(item))
            else:
                text_payloads.append(str(resp))

    vin_from_response: Optional[str] = None
    pdf_present = False
    activation_prompt: Optional[str] = None

    for batch in response_batches:
        _extend_payloads(batch)
        if isinstance(batch, _bridge.BridgeResponse):
            actions = batch.actions or {}
            if actions.get("await_activation_phone") and activation_prompt is None:
                activation_prompt = _bridge.t("activation.prompt.cc", user_ctx.language)
            if not vin_from_response:
                vin_from_response = actions.get("vin")
            photos_action = actions.get("photos")
            if photos_action:
                vin_for_photos = None
                urls: Optional[List[str]] = None
                if isinstance(photos_action, dict):
                    vin_for_photos = photos_action.get("vin") or photos_action.get("id") or photos_action.get("car_vin")
                    candidate = (
                        photos_action.get("urls")
                        or photos_action.get("images")
                        or photos_action.get("photos")
                        or photos_action.get("data")
                    )
                    if isinstance(candidate, (list, tuple, set)):
                        urls = list(candidate)
                elif isinstance(photos_action, (list, tuple, set)):
                    urls = list(photos_action)
                elif isinstance(photos_action, str):
                    urls = [photos_action]
                if urls:
                    vin_for_photos = vin_for_photos or report_vin or vin_from_response or ""
                    LOGGER.info(
                        "whatsapp: processing photos action from bridge (urls=%s vin=%s state=%s)",
                        len(urls),
                        vin_for_photos,
                        user_ctx.state,
                    )
                    photo_tasks.append(
                        asyncio.create_task(
                            _send_photo_batch(
                                msisdn,
                                user_ctx,
                                vin_for_photos or "UNKNOWN",
                                urls,
                                client,
                                choice="bridge_photos",
                            )
                        )
                    )
                else:
                    LOGGER.debug("whatsapp: photos action present but no urls extracted; skipping send")
            for doc in batch.documents:
                if isinstance(doc, dict) and doc.get("type") == "pdf":
                    pdf_present = True

    if activation_prompt:
        text_payloads.insert(0, _clean_html_for_whatsapp(activation_prompt))

    # Add manual messages (e.g., language confirmation) collected outside bridge
    text_payloads.extend(manual_texts)

    send_tasks: List[asyncio.Task[Any]] = []
    
    # Check if we need to send menu buttons based on response actions
    should_send_menu = False
    menu_resp: Optional[_bridge.BridgeResponse] = None
    for batch in response_batches:
        if isinstance(batch, _bridge.BridgeResponse):
            if batch.actions.get("menu") and menu_resp is None:
                menu_resp = batch
            if batch.actions.get("menu") or batch.actions.get("welcome"):
                should_send_menu = True

    if manual_send_menu:
        should_send_menu = False  # We will send menu manually once after language change

    lang_for_user = (user_ctx.language or "ar").lower()
    sending_photo_menu = bool(pdf_present and vin_from_response)

    # Suppress auto PDF notes and duplicate photo prompts across all languages
    suppressed_texts = {
        (_bridge.t("report.success.pdf_created", lang_for_user) or "").strip(),
        (_bridge.t("report.success.pdf_note", lang_for_user) or "").strip(),
        (_bridge.t("report.success.note", lang_for_user) or "").strip(),
    }
    suppressed_contains = {
        (_bridge.t("main_menu.hint", lang_for_user) or "").strip(),
    }

    prompt_text = (_report_options_prompt(lang_for_user) or "").strip()
    if should_send_menu or manual_send_menu or sending_photo_menu:
        suppressed_texts.add(prompt_text)
        first_line = prompt_text.splitlines()[0] if prompt_text else ""
        if first_line:
            suppressed_contains.add(first_line.strip())

    filtered_payloads: List[str] = []
    for body in text_payloads:
        if not body:
            continue
        normalized = body.strip()
        if normalized in suppressed_texts:
            LOGGER.debug("whatsapp: suppressing text payload (exact): %s", normalized)
            continue
        skip = False
        for frag in suppressed_contains:
            if frag and frag in normalized:
                LOGGER.debug("whatsapp: suppressing text payload (contains): %s", normalized)
                skip = True
                break
        if skip:
            continue
        filtered_payloads.append(body)
    text_payloads = filtered_payloads

    for body in text_payloads:
        if not body:
            continue
        clean_body = _clean_html_for_whatsapp(body)
        send_tasks.append(asyncio.create_task(send_whatsapp_text(msisdn, clean_body, client=client)))

    # Avoid double menu and avoid fallback menu while still in a sub-flow
    latest_state = (_load_db().get("users", {}).get(user_ctx.user_id, {}) or {}).get("state")
    if should_send_menu:
        if latest_state:
            LOGGER.debug("whatsapp: skipping menu send because state is active (%s)", latest_state)
        else:
            LOGGER.debug("whatsapp: sending menu from bridge actions (single render)")
            send_tasks.append(asyncio.create_task(_send_bridge_menu(msisdn, user_ctx, client, resp=menu_resp)))

    if manual_send_menu:
        LOGGER.debug("whatsapp: sending menu after language update (single render, lang=%s)", user_ctx.language)
        send_tasks.append(asyncio.create_task(_send_bridge_menu(msisdn, user_ctx, client)))

    doc_tasks: List[asyncio.Task[Any]] = []
    LOGGER.info("Found %d documents to send", len(documents))
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("type") != "pdf":
            continue
        LOGGER.info("Queueing PDF document: %s", doc.get("filename"))
        doc_tasks.append(asyncio.create_task(_relay_pdf_document(client, msisdn, doc)))

    image_tasks: List[asyncio.Task[Any]] = []
    for media in media_payloads:
        if not isinstance(media, dict):
            continue
        if media.get("type") not in {"image", "photo"}:
            continue
        image_tasks.append(asyncio.create_task(_relay_image_document(client, msisdn, media)))

    try:
        if send_tasks:
            await asyncio.gather(*send_tasks)
        if doc_tasks:
            await asyncio.gather(*doc_tasks)
        if image_tasks:
            await asyncio.gather(*image_tasks)
        if photo_tasks:
            await asyncio.gather(*photo_tasks)
        if pdf_present and vin_from_response:
            await _send_report_options_menu(msisdn, user_ctx.user_id, vin_from_response, client)
    except UltraMsgError as exc:
        LOGGER.error("Failed to relay WhatsApp response: %s", exc)
        return {"status": "error", "reason": str(exc)}
    finally:
        _cleanup_temp_files(temp_files)

    total_responses = len(text_payloads) + len(doc_tasks) + len(image_tasks)
    return {"status": "ok", "responses": total_responses}


def _cleanup_temp_files(files: List[str]) -> None:
    for entry in files:
        try:
            if entry and os.path.exists(entry):
                os.remove(entry)
        except OSError:
            LOGGER.debug("Failed to cleanup temp file: %s", entry)


def _encode_file_to_base64(path: str) -> Optional[str]:
    try:
        data = Path(path).read_bytes()
        return base64.b64encode(data).decode("ascii")
    except FileNotFoundError:
        LOGGER.warning("File not found for media relay: %s", path)
    except OSError:
        LOGGER.exception("Failed to read media file: %s", path)
    return None


async def _relay_pdf_document(client: UltraMsgClient, msisdn: str, document: Dict[str, Any]) -> None:
    filename = document.get("filename") or document.get("file_name") or "report.pdf"
    caption = document.get("caption")
    payload: Dict[str, Any] = {"filename": filename}

    base64_payload = document.get("document_base64") or document.get("base64")
    path_value = document.get("path")
    url_value = document.get("url")

    # Try to serve file via public URL if available (to avoid 413 Payload Too Large)
    public_url = os.getenv("WHATSAPP_PUBLIC_URL")
    if path_value and public_url:
        try:
            # Copy file to static dir
            src_path = Path(path_value)
            if src_path.exists():
                dst_path = STATIC_DIR / filename
                shutil.copy2(src_path, dst_path)
                url_value = f"{public_url}/static/{filename}"
                LOGGER.info("Serving PDF via public URL: %s", url_value)
        except Exception as e:
            LOGGER.warning("Failed to serve PDF via static URL: %s", e)

    if not base64_payload and not url_value and path_value:
        base64_payload = _encode_file_to_base64(str(path_value))

    if url_value:
        payload["document_url"] = url_value
    elif base64_payload:
        payload["document_base64"] = base64_payload
    else:
        LOGGER.warning("Skipping pdf document without path/url/base64: %s", document)
        return

    if caption:
        payload["caption"] = caption
    if document.get("mime_type"):
        payload["mime_type"] = document["mime_type"]

    LOGGER.info("Sending PDF document to %s (filename=%s)", msisdn, filename)
    try:
        resp = await client.send_document(msisdn, **payload)
        LOGGER.info("UltraMsg send_document response: %s", resp)
    except Exception as e:
        LOGGER.error("Failed to send document: %s", e, exc_info=True)


async def _relay_image_document(client: UltraMsgClient, msisdn: str, media: Dict[str, Any]) -> None:
    payload: Dict[str, Any] = {}

    base64_payload = media.get("image_base64") or media.get("base64")
    path_value = media.get("path")
    url_value = media.get("url") or media.get("media")

    if not base64_payload and path_value:
        base64_payload = _encode_file_to_base64(str(path_value))

    if base64_payload:
        payload["image_base64"] = base64_payload
    elif url_value:
        payload["url"] = url_value
    else:
        LOGGER.warning("Skipping image document without path or url: %s", media)
        return

    if media.get("filename"):
        payload["filename"] = media["filename"]

    await client.send_image(msisdn, **payload)


async def send_whatsapp_text(
    to: str,
    body: str,
    *,
    preview_url: bool = False,
    client: Optional[UltraMsgClient] = None,
) -> Dict[str, Any]:
    """Send a WhatsApp text message via UltraMsg in international format."""

    recipient = _normalize_recipient(to)
    if not recipient:
        raise UltraMsgError("Recipient phone number is missing or invalid.")

    active_client = client or _build_client()
    extra: Dict[str, Any] = {}
    if preview_url:
        extra["previewUrl"] = "1"

    try:
        LOGGER.info("📤 Sending WhatsApp reply to %s", recipient)
        response = await active_client.send_text(recipient, body, **extra)
        LOGGER.debug("UltraMsg send_text response: %s", response)
        return response
    except UltraMsgError:
        LOGGER.exception("Failed to send WhatsApp text to %s", recipient)
        raise


def _resolve_ultramsg_settings() -> tuple[str, str, str]:
    if ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN:
        return ULTRAMSG_INSTANCE_ID, ULTRAMSG_TOKEN, ULTRAMSG_BASE_URL
    return get_ultramsg_settings()


def _build_client() -> UltraMsgClient:
    instance_id, token, base_url = _resolve_ultramsg_settings()
    creds = UltraMsgCredentials(instance_id=instance_id, token=token, base_url=base_url)
    return UltraMsgClient(creds)


def _get_ultramsg_client(request: Optional[Request] = None) -> UltraMsgClient:
    storage = (request.app.state if request else app.state)
    client = getattr(storage, "ultramsg_client", None)
    if client is None:
        client = _build_client()
        storage.ultramsg_client = client
    return client


@app.on_event("startup")
async def _on_startup() -> None:
    loop = asyncio.get_running_loop()
    LOGGER.info(f"Event Loop Policy: {asyncio.get_event_loop_policy()}")
    LOGGER.info(f"Current Event Loop: {loop}")
    
    _get_ultramsg_client()
    LOGGER.info("WhatsApp webhook server ready on %s:%s", WHATSAPP_HOST, WHATSAPP_PORT)
    
    # Try to detect public URL
    public_url = os.getenv("WHATSAPP_PUBLIC_URL")
    if not public_url:
        ngrok_url = await _get_ngrok_url()
        if ngrok_url:
            LOGGER.info("Detected ngrok URL: %s", ngrok_url)
            os.environ["WHATSAPP_PUBLIC_URL"] = ngrok_url
        else:
            LOGGER.warning("Could not detect public URL. PDF sending might fail if files are too large for base64.")


@app.get("/whatsapp/health")
async def whatsapp_health() -> Dict[str, str]:
    return {"status": "ok"}


async def _safe_background_handler(entry: Dict[str, Any], client: UltraMsgClient, event_type: str) -> None:
    """Wrapper to handle background processing safely."""
    try:
        await handle_incoming_whatsapp_message(entry, client, event_type=event_type)
    except Exception:
        LOGGER.exception("Background processing failed for WhatsApp message")


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        LOGGER.warning("Received invalid JSON payload from UltraMsg")
        return JSONResponse({"status": "error", "reason": "invalid_json"})

    LOGGER.info("🔥 WEBHOOK RECEIVED: %s", payload)

    entries = list(_extract_entries(payload))
    if not entries:
        LOGGER.debug("UltraMsg payload did not contain entries")
        return JSONResponse({"status": "ok", "results": []})

    root_event_type = str(payload.get("event_type") or "").lower()
    client = _get_ultramsg_client(request)
    
    for entry in entries:
        entry_event_type = str(entry.get("event_type") or root_event_type or "").lower()
        # Process in background to avoid blocking the webhook response (UltraMsg timeout)
        background_tasks.add_task(_safe_background_handler, entry, client, entry_event_type)

    return JSONResponse({"status": "ok", "queued": len(entries)})


# Some providers may post to "/whatsapp" instead of "/whatsapp/webhook".
# Accept it and delegate to the main handler to avoid 404s.
@app.post("/whatsapp")
async def whatsapp_webhook_alias(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    return await whatsapp_webhook(request, background_tasks)


def run() -> None:
    LOGGER.info(
        "Starting WhatsApp webhook server on %s:%s (UltraMsg instance=%s)",
        WHATSAPP_HOST,
        WHATSAPP_PORT,
        ULTRAMSG_INSTANCE_ID or "<unset>",
    )
    # reload=False to avoid subprocess event loop issues on Windows
    uvicorn.run(
        "whatsapp_app:app",
        host=WHATSAPP_HOST,
        port=WHATSAPP_PORT,
        reload=False,
        log_level=os.getenv("LOG_LEVEL", "info"),
        loop="asyncio"
    )


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    run()
