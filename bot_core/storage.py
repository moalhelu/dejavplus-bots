"""DB helpers extracted from the legacy monolith."""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, date, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Final

from contextlib import contextmanager

from bot_core.request_ledger import reserve_once as _ledger_reserve_once, commit_once as _ledger_commit_once, refund_once as _ledger_refund_once

from bot_core.config import get_env
from bot_core.telemetry import log_timing, timed

_DB_LOCK = Lock()
# Default to 1 retained backup; env DB_BACKUP_RETENTION can override
_BACKUP_RETENTION: Final[int] = max(1, int(os.getenv("DB_BACKUP_RETENTION", "1") or "1"))


@contextmanager
def _db_file_lock(path: str):
    """Cross-process lock to protect db.json read/write.

    We run both Telegram and WhatsApp bots concurrently in separate processes.
    A plain threading.Lock is not enough and can lead to db corruption on Windows.
    """

    lock_path = path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)

    fh = None
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
        if os.name == "nt":
            import msvcrt

            # Lock 1 byte; must seek to start for consistent locking.
            try:
                fh.seek(0)
            except Exception:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    try:
                        fh.seek(0)
                    except Exception:
                        pass
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


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
    with timed("db.load", file=Path(path).name):
        with _db_file_lock(path):
            if not os.path.exists(path):
                return _blank_db()
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                # If the DB is corrupted/truncated, try the most recent backup.
                try:
                    src = Path(path)
                    backup_dir = src.parent / "backups"
                    pattern = f"{src.stem}-*{src.suffix or '.json'}"
                    backups = sorted(backup_dir.glob(pattern))
                    if backups:
                        with open(backups[-1], "r", encoding="utf-8") as bfh:
                            data = json.load(bfh)
                    else:
                        return _blank_db()
                except Exception:
                    return _blank_db()
        for key in ("users", "activation_requests", "settings"):
            data.setdefault(key, _blank_db()[key])
        _sanitize_settings(data.get("settings", {}))
        return data


def save_db(db: Dict[str, Any]) -> None:
    path = _db_path()
    # Per-process temp file to avoid cross-process collisions.
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with _DB_LOCK:
        with timed("db.save", file=Path(path).name):
            with _db_file_lock(path):
                _sanitize_settings(db.setdefault("settings", {}))
                serialized = json.dumps(db, ensure_ascii=False, indent=2)

                try:
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as existing_fh:
                            existing = existing_fh.read()
                        if existing == serialized:
                            log_timing("db.save.noop", 0.0, file=Path(path).name, bytes=len(serialized))
                            return
                except Exception:
                    # If we can't read existing content, fall back to normal save.
                    pass

                _backup_existing_db(path)
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    fh.write(serialized)
                os.replace(tmp_path, path)

                # Best-effort cleanup if older temp files exist (e.g. previous crash).
                try:
                    stale = Path(path).parent.glob(f"{Path(path).name}.*.tmp")
                    for p in stale:
                        if str(p) != tmp_path:
                            try:
                                p.unlink()
                            except Exception:
                                pass
                except Exception:
                    pass


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
        # Feature cleanup: drop any legacy photo service flags.
        services.pop("photos", None)
        for _k in list(services.keys()):
            if _k.startswith("photos_"):
                services.pop(_k, None)
        services.setdefault("carfax", True)
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
    settings.pop("api_token", None)


def _backup_existing_db(path: str) -> None:
    src = Path(path)
    if not src.exists():
        return
    with timed("db.backup", file=src.name):
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


def reserve_credit(user_id: str, *, rid: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> bool:
    """Reserve 1 report credit (idempotent when rid is provided).

    Returns True only when the reservation is applied (first time for this rid).
    """

    db = load_db()
    if rid:
        decision = _ledger_reserve_once(db, rid, meta=meta)
        if not decision.changed:
            save_db(db)
            return False

    u = ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = _safe_int(limits.get("today_used")) + 1
    limits["month_used"] = _safe_int(limits.get("month_used")) + 1
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = stats.get("pending_reports", 0) + 1
    save_db(db)
    return True


def refund_credit(user_id: str, *, rid: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> bool:
    """Refund 1 reserved credit (idempotent when rid is provided).

    Returns True only when the refund is applied (first time for this rid).
    """

    db = load_db()
    if rid:
        decision = _ledger_refund_once(db, rid, outcome_meta=meta)
        if not decision.changed:
            save_db(db)
            return False

    u = ensure_user(db, user_id, None)
    limits = u.setdefault("limits", {})
    limits["today_used"] = max(0, _safe_int(limits.get("today_used")) - 1)
    limits["month_used"] = max(0, _safe_int(limits.get("month_used")) - 1)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    save_db(db)
    return True


def commit_credit(user_id: str, *, rid: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> bool:
    """Commit successful report delivery (idempotent when rid is provided).

    Returns True only when the commit is applied (first time for this rid).
    """

    db = load_db()
    if rid:
        decision = _ledger_commit_once(db, rid, outcome_meta=meta)
        if not decision.changed:
            save_db(db)
            return False

    u = ensure_user(db, user_id, None)
    stats = u.setdefault("stats", {})
    stats["pending_reports"] = max(0, stats.get("pending_reports", 0) - 1)
    stats["total_reports"] = stats.get("total_reports", 0) + 1
    stats["last_report_ts"] = now_str()
    save_db(db)
    return True



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
