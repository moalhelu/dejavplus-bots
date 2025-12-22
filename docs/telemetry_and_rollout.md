# Telemetry + safe rollout notes

This repo supports opt-in timing telemetry and a low-risk DB write optimization.

## Enable timing telemetry

Set the env var:

- `ENABLE_TIMING_LOGS=1`

When disabled (default), the helpers short-circuit and add effectively no overhead beyond tiny `if` checks.

### Correlation id (`rid`)

- WhatsApp webhook handler sets a per-entry request id with prefix `wa-`.
- Timings emitted from code paths executed within that request will include the same `rid`.

## Timing events emitted

Events are emitted via logger name `dejavu.timing`.

- DB
  - `db.load` (path)
  - `db.save` (path)
  - `db.backup` (path)
- Reports
  - `report.fetch` (vin, lang, prefer_non_pdf)
  - `report.render_pdf` (vin, lang)
- PDF
  - `pdf.chromium` (mode, html_len)
- Translation
  - `translate.batch` (target, total, missing, method)
  - `translate.html` (target, html_len)
- WhatsApp
  - `wa.handle` (event_type)
  - `wa.ultramsg.send_text` (to, body_len)
  - `wa.ultramsg.send_image` (to)
  - `wa.ultramsg.send_document` (to, filename)

Notes:
- The telemetry helper intentionally truncates long string/bytes fields.
- No message bodies or secrets are logged (only lengths/metadata).

## Low-risk optimization: skip unchanged DB saves

`bot_core/storage.py::save_db` now:
- serializes the DB to JSON (same formatting as before)
- compares it to the current file contents
- if identical, it **skips** backup + atomic replace

This reduces unnecessary filesystem writes and backup spam when a flow calls `save_db()` without actually changing persistent state.

## Rollout plan (recommended)

1) Staging or one-node canary
- Turn on `ENABLE_TIMING_LOGS=1`.
- Run normal traffic for 30–60 minutes.

2) Observe
- Look for p50/p95 spikes in:
  - `report.fetch` vs `report.render_pdf`
  - `translate.*`
  - `pdf.chromium`
  - `wa.ultramsg.send_*`
  - `db.save` / `db.backup`

3) Production
- Enable `ENABLE_TIMING_LOGS=1` temporarily (e.g., 1–3 hours).
- Collect the timings, then disable again.

## Rollback

- Telemetry: unset `ENABLE_TIMING_LOGS` (or set to `0`).
- DB save optimization: revert the change in `bot_core/storage.py::save_db` (return to always-backup+write).

## Success criteria

Telemetry phase (Phase 1)
- All requests continue to succeed.
- Timing logs appear with consistent `rid` per WhatsApp webhook entry.

Optimization phase (Phase 2)
- Under steady traffic, `db.backup` frequency decreases notably.
- No functional regressions in user state/credit accounting.

## Next medium-risk items (not applied yet)

- Offload blocking disk I/O to threads (DB load/save/backups).
- Add cross-process locking around `db.json` to prevent corruption when Telegram + WhatsApp run concurrently.
