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
schema identity is `006_event_outbox`.

Business writes and external delivery fail closed by default. Event payloads
contain tenant and aggregate identifiers, lifecycle state, and correlation
identity only; recipients, rendered content, provider payloads, and credentials
are excluded.
