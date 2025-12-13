"""DB helpers extracted from the legacy monolith."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, date, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Final

from bot_core.config import get_env

_DB_LOCK = Lock()
# Default to 1 retained backup; env DB_BACKUP_RETENTION can override
_BACKUP_RETENTION: Final[int] = max(1, int(os.getenv("DB_BACKUP_RETENTION", "1") or "1"))


def _blank_db() -> Dict[str, Any]:
    return {
        "users": {},
        "activation_requests": [],
        "settings": {},
        "super_admins": [],
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _db_path() -> str:
    return get_env().db_path


def load_db() -> Dict[str, Any]:
    path = _db_path()
    if not os.path.exists(path):
        return _blank_db()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _blank_db()
    for key in ("users", "activation_requests", "settings"):
        data.setdefault(key, _blank_db()[key])
    _sanitize_settings(data.get("settings", {}))
    return data


def save_db(db: Dict[str, Any]) -> None:
    path = _db_path()
    tmp_path = path + ".tmp"
    with _DB_LOCK:
        _sanitize_settings(db.setdefault("settings", {}))
        _backup_existing_db(path)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(db, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def _default_user(tg_id: str, tg_username: Optional[str]) -> Dict[str, Any]:
    return {
        "tg_id": tg_id,
        "tg_username": tg_username or "",
        "custom_name": "",
        "is_active": False,
        "activation_date": None,
        "expiry_date": None,
        "balance": 0,
        "plan": "basic",
        "services": {
            "carfax": True,
            "photos_badvin": True,
            "photos_auction": True,
            "photos_accident": True,
        },
        "limits": {"daily": 200, "monthly": 500, "today_used": 0, "month_used": 0, "last_day": None, "last_month": None},
        "stats": {"total_reports": 0, "last_report_ts": None},
        "notes": "",
        "audit": [],
    }


def ensure_user(db: Dict[str, Any], tg_id: str, tg_username: Optional[str]) -> Dict[str, Any]:
    tg_id = str(tg_id)
    users = db.setdefault("users", {})
    if tg_id not in users:
        users[tg_id] = _default_user(tg_id, tg_username)
    else:
        if tg_username:
            users[tg_id]["tg_username"] = tg_username
        template = _default_user(tg_id, tg_username)
        for key, value in template.items():
            users[tg_id].setdefault(key, value)
        users[tg_id].pop("sessions", None)
        services = users[tg_id].setdefault("services", {})
        if "photos" in services and "photos_badvin" not in services:
            services["photos_badvin"] = bool(services.pop("photos"))
        services.setdefault("carfax", True)
        services.setdefault("photos_badvin", True)
        services.setdefault("photos_auction", True)
        services.setdefault("photos_accident", True)
    return users[tg_id]


def ensure_settings(db: Dict[str, Any]) -> Dict[str, str]:
    settings = db.setdefault("settings", {})
    _sanitize_settings(settings)
    return settings


def fmt_date(value: Optional[str]) -> str:
    return value or "-"


def display_name(user: Dict[str, Any]) -> str:
    if user.get("custom_name"):
        return user["custom_name"]
    username = user.get("tg_username")
    if username:
        return f"@{username}"
    return f"TG:{user.get('tg_id')}"


def remaining_monthly_reports(user: Dict[str, Any]) -> Optional[int]:
    limits: Dict[str, Any] = user.get("limits") or {}
    monthly = _safe_int(limits.get("monthly"))
    if monthly <= 0:
        return None
    used = _safe_int(limits.get("month_used"))
    return max(0, monthly - used)


def days_left(expiry: Optional[str]) -> Optional[int]:
    if not expiry:
        return None
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    except Exception:
        return None
    today = date.today()
    return max(0, (expiry_date - today).days)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sanitize_settings(settings: Dict[str, Any]) -> None:
    for key in ("api_token", "badvin_email", "badvin_password"):
        settings.pop(key, None)


def _backup_existing_db(path: str) -> None:
    src = Path(path)
    if not src.exists():
        return
    backup_dir = src.parent / "backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_name = f"{src.stem}-{timestamp}{src.suffix or '.json'}"
    backup_path = backup_dir / backup_name
    try:
        shutil.copy2(src, backup_path)
    except Exception:
        return
    pattern = f"{src.stem}-*{src.suffix or '.json'}"
    backups = sorted(backup_dir.glob(pattern))
    excess = len(backups) - _BACKUP_RETENTION
    if excess <= 0:
        return
    for old in backups[:excess]:
        try:
            old.unlink()
        except Exception:
            pass


def audit(user: Dict[str, Any], admin_tg: str, operation: str, **extra: Any) -> None:
    entry = {"ts": now_str(), "admin": admin_tg, "op": operation}
    entry.update(extra or {})
    user.setdefault("audit", []).append(entry)
    if len(user["audit"]) > 50:
        user["audit"] = user["audit"][-50:]


def bump_usage(user: Dict[str, Any]) -> None:
    today = date.today()
    month_key = today.strftime("%Y-%m")
    limits = user.setdefault("limits", {})
    if limits.get("last_day") != str(today):
        limits["today_used"] = 0
        limits["last_day"] = str(today)
    if limits.get("last_month") != month_key:
        limits["month_used"] = 0
        limits["last_month"] = month_key


def reserve_credit(user_id: str) -> None:
    """Deduct 1 credit from the user's balance (increment usage)."""
    db = load_db()
    u = ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = _safe_int(limits.get("today_used")) + 1
    limits["month_used"] = _safe_int(limits.get("month_used")) + 1
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = stats.get("pending_reports", 0) + 1
    save_db(db)


def refund_credit(user_id: str) -> None:
    """Refund 1 credit to the user's balance (decrement usage)."""
    db = load_db()
    u = ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = max(0, _safe_int(limits.get("today_used")) - 1)
    limits["month_used"] = max(0, _safe_int(limits.get("month_used")) - 1)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    save_db(db)


def commit_credit(user_id: str) -> None:
    """Confirm successful report generation (update stats)."""
    db = load_db()
    u = ensure_user(db, user_id, None)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    stats["total_reports"] = stats.get("total_reports", 0) + 1
    stats["last_report_ts"] = now_str()
    save_db(db)



def format_tg_with_phone(tg_id: str) -> str:
    try:
        db = load_db()
        user = db.get("users", {}).get(str(tg_id), {})
        phone = user.get("phone") or "—"
        if phone and phone != "—":
            wa = phone.lstrip("+")
            return f"TG:{tg_id} — 📞 <a href='https://wa.me/{wa}'>{phone}</a>"
        return f"TG:{tg_id} — 📞 —"
    except Exception:
        return f"TG:{tg_id} — 📞 —"
