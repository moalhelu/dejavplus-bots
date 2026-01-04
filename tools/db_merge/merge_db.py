"""Deterministic DB merge utility.

Rules:
- Output schema follows the current production db.json schema (source of truth).
- Legacy DBs only affect platform activation flags; never overwrite balances/limits/etc.
- Removes runtime/stateless keys from users.

Usage (example):
  python tools/db_merge/merge_db.py \
    --prod tools/db_merge/db.prod.json \
    --tele tools/db_merge/dbtele.legacy.json \
    --wa tools/db_merge/dbwhatsapp.legacy.json \
    --out db.json

Note: Do NOT commit any DB files.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


_ARABIC_DIGITS = str.maketrans(
    {
        "٠": "0",
        "١": "1",
        "٢": "2",
        "٣": "3",
        "٤": "4",
        "٥": "5",
        "٦": "6",
        "٧": "7",
        "٨": "8",
        "٩": "9",
        "۰": "0",
        "۱": "1",
        "۲": "2",
        "۳": "3",
        "۴": "4",
        "۵": "5",
        "۶": "6",
        "۷": "7",
        "۸": "8",
        "۹": "9",
    }
)


def _norm_digits(raw: str) -> str:
    s = (raw or "").strip().translate(_ARABIC_DIGITS)
    if "@" in s:
        s = s.split("@", 1)[0]
    s = s.replace(" ", "").replace("-", "")
    if s.startswith("00"):
        s = s[2:]
    if s.startswith("+"):
        s = s[1:]
    s = re.sub(r"\D+", "", s)
    return s


def _norm_phone_display(raw: str) -> str:
    d = _norm_digits(raw)
    if not d:
        return ""
    if 7 <= len(d) <= 15:
        return "+" + d
    return d


_DROP_KEYS = {"state", "temp_state", "session", "sessions", "sessions_data", "pending_actions"}


def _clean_user(u: dict) -> None:
    for k in list(u.keys()):
        if k in _DROP_KEYS:
            u.pop(k, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", required=True, help="Production db.json (source-of-truth schema)")
    ap.add_argument("--tele", required=True, help="Legacy Telegram db json")
    ap.add_argument("--wa", required=True, help="Legacy WhatsApp db json")
    ap.add_argument("--out", required=True, help="Output db.json path")
    args = ap.parse_args()

    prod_path = Path(args.prod)
    tele_path = Path(args.tele)
    wa_path = Path(args.wa)
    out_path = Path(args.out)

    prod = json.loads(prod_path.read_text(encoding="utf-8"))
    tele = json.loads(tele_path.read_text(encoding="utf-8"))
    wa = json.loads(wa_path.read_text(encoding="utf-8"))

    # Import the runtime default-user builder to match production defaults.
    from bot_core.storage import _default_user  # type: ignore

    users = prod.setdefault("users", {})

    # Build phone->userKey index for existing users.
    phone_to_userkey: dict[str, str] = {}
    for user_key, user in list(users.items()):
        if not isinstance(user, dict):
            continue
        phone = user.get("phone")
        if isinstance(phone, str) and phone.strip():
            d = _norm_digits(phone)
            if d:
                phone_to_userkey[d] = str(user_key)

    # Clean existing users.
    for u in users.values():
        if isinstance(u, dict):
            _clean_user(u)

    # Telegram legacy: match by tg_id (dict key).
    tele_users = tele.get("users") if isinstance(tele, dict) else None
    if isinstance(tele_users, dict):
        for tg_id, legacy_u in tele_users.items():
            tg_id = str(tg_id)
            if tg_id in users:
                continue
            tg_username = legacy_u.get("tg_username") if isinstance(legacy_u, dict) else None
            nu = _default_user(tg_id, tg_username if isinstance(tg_username, str) else None)
            _clean_user(nu)
            users[tg_id] = nu

    # WhatsApp legacy: match by phone number; key new users by digits (matches WA sender normalization).
    wa_users = wa.get("users") if isinstance(wa, dict) else None
    if isinstance(wa_users, dict):
        for legacy_key, legacy_u in wa_users.items():
            phone_raw = None
            if isinstance(legacy_u, dict):
                phone_raw = legacy_u.get("phone") or legacy_u.get("id")
            phone_raw = phone_raw or legacy_key
            digits = _norm_digits(str(phone_raw))
            if not digits:
                continue
            if digits in users:
                continue
            if digits in phone_to_userkey:
                continue
            nu = _default_user(digits, None)
            display_phone = _norm_phone_display(str(phone_raw))
            if display_phone:
                nu["phone"] = display_phone
            _clean_user(nu)
            users[digits] = nu

    # Ensure top-level keys exist.
    for k in ("users", "activation_requests", "settings", "super_admins"):
        prod.setdefault(k, {} if k in {"users", "settings"} else [])

    out_path.write_text(json.dumps(prod, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
