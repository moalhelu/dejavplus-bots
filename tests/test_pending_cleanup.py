import copy

from app import _remove_pending_request


def test_activate_removes_pending_request():
    # Fake in-memory DB with mixed ID types
    db = {
        "users": {"123": {"tg_id": "123", "is_active": False}},
        "activation_requests": [
            {"tg_id": "123", "ts": "now", "phone": "+111"},
            {"tg_id": 456, "ts": "later", "phone": "+222"},
        ],
    }

    # Simulate activation of tg_id=123
    db["users"]["123"]["is_active"] = True

    # Ensure removal handles string/int ids
    removed = _remove_pending_request(copy.deepcopy(db), "123")
    assert removed

    # Remove in the original db and validate list is pruned
    _remove_pending_request(db, "123")
    assert all(str(req.get("tg_id")) != "123" for req in db.get("activation_requests", []))

    # Other entries stay intact
    assert any(str(req.get("tg_id")) == "456" for req in db.get("activation_requests", []))

    # No stray pending queues
    assert db.get("pending") in (None, [])
    assert db.get("pending_activation") in (None, [])
    assert db.get("pending_activations") in (None, [])
