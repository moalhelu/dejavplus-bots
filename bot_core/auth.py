"""Authorization helpers for admin checks."""
from __future__ import annotations

from typing import Dict, Any, Set

from bot_core.config import get_env, reload_env as _reload_env
from bot_core.storage import load_db


def env_super_admins() -> Set[str]:
    return set(get_env().super_admins)


def reload_env() -> None:
    _reload_env()


def db_super_admins(db: Dict[str, Any]) -> list[str]:
    return db.setdefault("super_admins", [])


def is_super_admin(tg_id: str) -> bool:
    tid = str(tg_id)
    db = load_db()
    return tid in env_super_admins() or tid in db_super_admins(db)


def is_admin_tg(tg_id: str) -> bool:
    # Currently admins == super admins (env or db)
    return is_super_admin(tg_id)


def is_ultimate_super(tg_id: str) -> bool:
    return str(tg_id) in env_super_admins()
