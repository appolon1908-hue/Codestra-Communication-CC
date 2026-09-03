# Codestra Communication CC

Customer communications control-plane repository for email, SMS, WhatsApp, push, templates, consent, suppression, delivery state, and communication history.

## Runtime processes

The repository contains the HTTP control plane plus two workers built from the
same immutable image:

- `python -m app.delivery_worker` submits governed commands to Middleware. It
  does no work unless `EXTERNAL_DELIVERY_ENABLED=true`.
- `python -m app.event_worker` publishes sanitized lifecycle events from the
  transactional event outbox. Configure `EVENT_REDIS_URL`; non-loopback Redis
  connections must use `rediss://`.

Apply migrations using the one-shot migration process before starting a new
release. Normal application startup never performs migrations. The current
schema identity is `010_data_protection_enforcement`. Run
`scripts/backfill_data_protection.py` as the one-shot migration from schema
`008`; it applies `009`, locks all affected tables, converts the data, and
applies `010` in one transaction. Do not apply `009` as an independently
committed production step. The explicitly confirmed
`scripts/restore_plaintext_for_rollback.py` reverses `010`, restores all
plaintext required by the prior release, and reverses `009` atomically.

Recipient identifiers, consent/preference subjects, template content, and
delivery commands are encrypted with AES-256-GCM before persistence. Set
`COMMUNICATION_DATA_KEY_DIR` to an absolute, non-symlinked directory containing
mode-0600 `<key-id>.key` files and set `COMMUNICATION_ACTIVE_DATA_KEY_ID` to the
write key. Set `COMMUNICATION_BLIND_INDEX_KEY_ID` to a separately governed,
stable key identity; changing it requires an explicit blind-index rebuild. The
service keeps older encryption key files only for rotation-time reads and
reports not-ready when the active key cannot be loaded. Legacy plaintext reads
are disabled by default and exist only as a bounded migration compatibility
mode.

Business writes and external delivery fail closed by default. Event payloads
contain tenant and aggregate identifiers, lifecycle state, and correlation
identity only; recipients, rendered content, provider payloads, and credentials
are excluded.
