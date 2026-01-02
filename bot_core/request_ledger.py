from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


_LEDGER_KEY = "requests"


@dataclass(frozen=True, slots=True)
class LedgerDecision:
    changed: bool
    state: str
    entry: Dict[str, Any]


def _ledger(db: Dict[str, Any]) -> Dict[str, Any]:
    led = db.setdefault(_LEDGER_KEY, {})
    if not isinstance(led, dict):
        led = {}
        db[_LEDGER_KEY] = led
    return led


def _now() -> float:
    return time.time()


def _max_entries() -> int:
    raw = (os.getenv("REQUEST_LEDGER_MAX", "20000") or "20000").strip()
    try:
        n = int(raw)
    except Exception:
        n = 20000
    return max(500, min(n, 200_000))


def _prune_best_effort(db: Dict[str, Any]) -> None:
    led = _ledger(db)
    max_n = _max_entries()
    if len(led) <= max_n:
        return
    # Drop oldest by updated_ts
    items = []
    for rid, ent in led.items():
        try:
            ts = float((ent or {}).get("updated_ts") or (ent or {}).get("created_ts") or 0.0)
        except Exception:
            ts = 0.0
        items.append((ts, rid))
    items.sort()
    excess = len(items) - max_n
    for _, rid in items[:excess]:
        led.pop(rid, None)


def get_entry(db: Dict[str, Any], rid: str) -> Optional[Dict[str, Any]]:
    led = _ledger(db)
    ent = led.get(rid)
    return ent if isinstance(ent, dict) else None


def reserve_once(db: Dict[str, Any], rid: str, *, meta: Optional[Dict[str, Any]] = None) -> LedgerDecision:
    led = _ledger(db)
    now = _now()
    ent = get_entry(db, rid)
    if not ent:
        ent = {
            "rid": rid,
            "created_ts": now,
            "updated_ts": now,
            "meta": meta or {},
            "reserved": True,
            "committed": False,
            "refunded": False,
            "state": "reserved",
        }
        led[rid] = ent
        _prune_best_effort(db)
        return LedgerDecision(True, "reserved", ent)

    ent.setdefault("meta", {})
    if meta:
        try:
            ent["meta"].update(meta)
        except Exception:
            pass

    # If already terminal, do not re-reserve.
    if ent.get("committed"):
        ent["updated_ts"] = now
        ent["state"] = "committed"
        return LedgerDecision(False, "committed", ent)
    if ent.get("refunded"):
        ent["updated_ts"] = now
        ent["state"] = "refunded"
        return LedgerDecision(False, "refunded", ent)

    if ent.get("reserved"):
        ent["updated_ts"] = now
        ent["state"] = "reserved"
        return LedgerDecision(False, "reserved", ent)

    ent["reserved"] = True
    ent["updated_ts"] = now
    ent["state"] = "reserved"
    return LedgerDecision(True, "reserved", ent)


def commit_once(db: Dict[str, Any], rid: str, *, outcome_meta: Optional[Dict[str, Any]] = None) -> LedgerDecision:
    led = _ledger(db)
    now = _now()
    ent = get_entry(db, rid) or {"rid": rid, "created_ts": now, "meta": {}}
    ent.setdefault("meta", {})
    if outcome_meta:
        try:
            ent["meta"].update(outcome_meta)
        except Exception:
            pass

    if ent.get("committed"):
        ent["updated_ts"] = now
        ent["state"] = "committed"
        led[rid] = ent
        return LedgerDecision(False, "committed", ent)

    ent["reserved"] = True
    ent["committed"] = True
    ent["refunded"] = False
    ent["updated_ts"] = now
    ent["state"] = "committed"
    led[rid] = ent
    _prune_best_effort(db)
    return LedgerDecision(True, "committed", ent)


def refund_once(db: Dict[str, Any], rid: str, *, outcome_meta: Optional[Dict[str, Any]] = None) -> LedgerDecision:
    led = _ledger(db)
    now = _now()
    ent = get_entry(db, rid) or {"rid": rid, "created_ts": now, "meta": {}}
    ent.setdefault("meta", {})
    if outcome_meta:
        try:
            ent["meta"].update(outcome_meta)
        except Exception:
            pass

    if ent.get("refunded"):
        ent["updated_ts"] = now
        ent["state"] = "refunded"
        led[rid] = ent
        return LedgerDecision(False, "refunded", ent)

    # If already committed, do not refund.
    if ent.get("committed"):
        ent["updated_ts"] = now
        ent["state"] = "committed"
        led[rid] = ent
        return LedgerDecision(False, "committed", ent)

    ent["reserved"] = True
    ent["committed"] = False
    ent["refunded"] = True
    ent["updated_ts"] = now
    ent["state"] = "refunded"
    led[rid] = ent
    _prune_best_effort(db)
    return LedgerDecision(True, "refunded", ent)
