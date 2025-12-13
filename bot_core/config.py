"""Environment and runtime configuration helpers for the Carfax bot."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Set

from dotenv import load_dotenv


def _parse_super_admins(raw: str) -> Set[str]:
    ids: Set[str] = set()
    if not raw:
        return ids
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("@"):
            ids.add(token)
        else:
            ids.add(str(token))
    return ids


@dataclass
class EnvConfig:
    db_path: str
    bot_token: str
    api_url: str
    api_token: str
    badvin_email: str
    badvin_password: str
    apicar_base_url: str
    apicar_api_key: str
    apicar_timeout: float
    apicar_image_timeout: float
    translator_defaults: dict[str, str]
    super_admins: Set[str]
    ultramsg_instance_id: str
    ultramsg_token: str
    ultramsg_base_url: str


def _load_env_values() -> EnvConfig:
    load_dotenv(override=True)

    translator_defaults = {
        "REPORT_DEFAULT_LANG": os.getenv("REPORT_DEFAULT_LANG", "en").lower().strip(),
        "AZURE_TRANSLATOR_KEY": os.getenv("AZURE_TRANSLATOR_KEY", "").strip(),
        "AZURE_TRANSLATOR_REGION": os.getenv("AZURE_TRANSLATOR_REGION", "").strip(),
        "GOOGLE_TRANSLATE_API_KEY": os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip(),
        "LIBRETRANSLATE_URL": os.getenv("LIBRETRANSLATE_URL", "").strip(),
        "LIBRETRANSLATE_API_KEY": os.getenv("LIBRETRANSLATE_API_KEY", "").strip(),
        "TRANSLATE_API_URL": os.getenv("TRANSLATE_API_URL", "").strip(),
        "TRANSLATE_API_KEY": os.getenv("TRANSLATE_API_KEY", "").strip(),
        "AZURE_TRANSLATOR_ENDPOINT": os.getenv("AZURE_TRANSLATOR_ENDPOINT", "").strip(),
    }

    apicar_base_url = os.getenv("APICAR_API_BASE", "https://api.apicar.store/api").strip()
    apicar_api_key = os.getenv("APICAR_API_KEY", "1f14a9d6-14e2-49b6-bc12-fd881b5a3e08").strip()
    ultramsg_instance_id = os.getenv("ULTRAMSG_INSTANCE_ID", "").strip()
    ultramsg_token = os.getenv("ULTRAMSG_TOKEN", "").strip()
    ultramsg_base_url = os.getenv("ULTRAMSG_BASE_URL", "https://api.ultramsg.com").strip() or "https://api.ultramsg.com"

    def _float_or(default: float, raw: str | None) -> float:
        try:
            return float(raw) if raw is not None else default
        except ValueError:
            return default

    return EnvConfig(
        db_path=os.getenv("DB_PATH", "db.json").strip() or "db.json",
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        api_url=os.getenv("API_URL", "").strip(),
        api_token=os.getenv("API_TOKEN", "").strip(),
        badvin_email=os.getenv("BADVIN_EMAIL", "").strip(),
        badvin_password=os.getenv("BADVIN_PASSWORD", "").strip(),
        apicar_base_url=apicar_base_url or "https://api.apicar.store/api",
        apicar_api_key=apicar_api_key or "1f14a9d6-14e2-49b6-bc12-fd881b5a3e08",
        apicar_timeout=_float_or(25.0, os.getenv("APICAR_API_TIMEOUT")),
        apicar_image_timeout=_float_or(25.0, os.getenv("APICAR_IMAGE_TIMEOUT")),
        translator_defaults=translator_defaults,
        super_admins=_parse_super_admins(os.getenv("TELEGRAM_SUPER_ADMINS", "")),
        ultramsg_instance_id=ultramsg_instance_id,
        ultramsg_token=ultramsg_token,
        ultramsg_base_url=ultramsg_base_url,
    )


@lru_cache(maxsize=1)
def get_env() -> EnvConfig:
    """Return cached environment configuration."""
    return _load_env_values()


def reload_env() -> EnvConfig:
    """Force reloading .env contents and return the new configuration."""
    get_env.cache_clear()
    return get_env()


def is_super_admin(tg_id: str) -> bool:
    cfg = get_env()
    tid = str(tg_id or "").strip()
    return bool(tid and tid in cfg.super_admins)


def get_report_default_lang() -> str:
    return get_env().translator_defaults.get("REPORT_DEFAULT_LANG", "en")


def get_ultramsg_settings() -> tuple[str, str, str]:
    cfg = get_env()
    return cfg.ultramsg_instance_id, cfg.ultramsg_token, cfg.ultramsg_base_url
