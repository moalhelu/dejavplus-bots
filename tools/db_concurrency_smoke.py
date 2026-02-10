"""Smoke-test for db.json cross-process safety.

Runs two spawned processes that concurrently load/save the DB many times.
Intended for Windows where Telegram + WhatsApp bots run in separate processes.

Usage:
  python tools/db_concurrency_smoke.py
"""

from __future__ import annotations

import json
import os
import multiprocessing as mp
from pathlib import Path
import sys


# Allow running as `python tools/db_concurrency_smoke.py`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _init_env(db_path: str) -> Path:
    os.environ["DB_PATH"] = db_path
    from bot_core.config import reload_env

    reload_env()
    return Path(db_path)


def _clean_files(path: Path) -> None:
    for suffix in ("", ".lock"):
        try:
            Path(str(path) + suffix).unlink()
        except Exception:
            pass


def _worker(tag: int, loops: int) -> None:
    from bot_core.storage import load_db, save_db

    for _ in range(loops):
        db = load_db()
        user = db.setdefault("users", {}).setdefault(str(tag), {})
        user["count"] = int(user.get("count") or 0) + 1
        save_db(db)


def main() -> None:
    db_path = "db_test_concurrency.json"
    path = _init_env(db_path)
    _clean_files(path)

    from bot_core.storage import save_db

    save_db({"users": {}, "activation_requests": [], "settings": {}, "super_admins": []})

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_worker, args=(1, 120)),
        ctx.Process(target=_worker, args=(2, 120)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)

    for p in procs:
        if p.exitcode not in (0, None):
            raise SystemExit(f"process failed: pid={p.pid} exitcode={p.exitcode}")

    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    assert "users" in data and "1" in data["users"] and "2" in data["users"], "missing users"
    print(
        "OK: JSON valid; counts=",
        data["users"]["1"].get("count"),
        data["users"]["2"].get("count"),
    )


if __name__ == "__main__":
    main()
