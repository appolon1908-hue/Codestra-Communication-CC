# Codestra Communication API Completion V1

This branch is the active Communication control-plane completion candidate. It
must not be treated as deployed or production-certified until every item in the
remaining-gaps section is closed and protected exact-head validation passes.

## Canonical surface

- `GET /health/live`, `GET /health/ready`, `GET /version`, private `GET /metrics`
- message create/list/detail/events/cancel
- template create/list/detail/update
- consent grant/list/revoke
- suppression create/list/delete
- provider routing plus honest disabled/unprobed health read-back
- tenant-scoped usage read-back derived from persisted message state
- communication preference list/upsert and recipient preference read-back
- non-mutating, bounded template preview
- operation list/detail/reconciliation and durable integration-event publication
- signed, replay-safe provider-result ingestion

## Security and durability

All tenant mutations require verified Keycloak issuer, audience, client, scope and tenant claims plus `X-Tenant-ID`, `X-Correlation-ID`, and `Idempotency-Key`. Browser-supplied scope or identity headers are rejected. Marketing messages require consent and every purpose honors suppression. Recipient, policy-subject, template-content, and delivery-command values are prohibited from metric/log/event labels and protected at rest using tenant/purpose-bound AEAD.

Enabled email or SMS work is submitted only to the durable Middleware control API. Communication never calls Klyrow, SMTP, Telnexa, Twilio, SIP, WhatsApp, push providers, or any delivery engine directly. Unknown outcomes enter reconciliation before retry.

## Required source evidence

- PostgreSQL migrations and reversible rollback/restore evidence
- exact-client logical backup integrity and isolated restore certification in CI
- semantic idempotency and concurrent duplicate tests
- cross-tenant denial and authorization mutation tests
- committed OpenAPI 3.1 and AsyncAPI 3.0 with runtime parity
- privacy-safe metrics for HTTP, auth, idempotency, consent, suppression, queues, provider results, reconciliation and safety state
- no provider credentials in source or payloads

## Remaining production gaps

- Complete provider-side sender/domain verification. Registration and real
  SPF/DKIM/DMARC/BIMI DNS read-back are implemented; reverse-DNS, transport TLS,
  and provider account approval remain `not_configured`, and senders remain
  `pending` until a separate provider-authorized `sending_enabled` state exists.
- Implement evidence-backed reputation read-back; the canonical contract has no
  `unknown` state, so this service does not currently expose a reputation route.
- Implement the documented conversation record contract after its request and
  response schemas have a canonical authority; only a path is currently documented.
- Reconcile the remaining SDK naming/schema differences and add exact cross-repo
  OpenAPI compatibility validation.
- Deploy and verify the implemented opt-in OTLP traces; add production encrypted/off-host backup and restore proof, staging
  certification, immutable release publication, and exact-digest deployment evidence.

## Safety baseline

```text
BUSINESS_WRITES_ENABLED=false
EXTERNAL_DELIVERY_ENABLED=false
TELEMETRY_EXPORT_ENABLED=false
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://approved-private-collector.example/v1/traces
# Optional private collector trust/client authentication; client key must be 0600 and runtime-owned.
OTEL_EXPORTER_OTLP_CERTIFICATE=/run/secrets/otel-ca.pem
OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE=/run/secrets/otel-client.crt
OTEL_EXPORTER_OTLP_CLIENT_KEY=/run/secrets/otel-client.key
RUNTIME_DEPLOYED=false
PRODUCTION_CHANGED=false
EMAILS_SENT=0
SMS_SENT=0
```
