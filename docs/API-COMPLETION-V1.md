# Codestra Communication API Completion V1

This branch completes the governed Communication control plane on top of the merged operational API.

## Canonical surface

- `GET /health/live`, `GET /health/ready`, `GET /version`, private `GET /metrics`
- message create/list/detail/events/cancel
- template create/list/detail/update
- consent grant/list/revoke
- suppression create/list/delete
- provider health and reputation
- signed, replay-safe provider-result ingestion

## Security and durability

All tenant mutations require verified Keycloak issuer, audience, client, scope and tenant claims plus `X-Tenant-ID`, `X-Correlation-ID`, and `Idempotency-Key`. Browser-supplied scope or identity headers are rejected. Marketing messages require consent and every purpose honors suppression. Recipient data is protected at rest and prohibited from metric/log labels.

Enabled email or SMS work is submitted only to the durable Middleware control API. Communication never calls Klyrow, SMTP, Telnexa, Twilio, SIP, WhatsApp, push providers, or any delivery engine directly. Unknown outcomes enter reconciliation before retry.

## Required source evidence

- PostgreSQL migrations and reversible rollback/restore evidence
- semantic idempotency and concurrent duplicate tests
- cross-tenant denial and authorization mutation tests
- committed OpenAPI 3.1 and AsyncAPI 3.0 with runtime parity
- privacy-safe metrics for HTTP, auth, idempotency, consent, suppression, queues, provider results, reconciliation and safety state
- no provider credentials in source or payloads

## Safety baseline

```text
BUSINESS_WRITES_ENABLED=false
EXTERNAL_DELIVERY_ENABLED=false
TELEMETRY_EXPORT_ENABLED=false
RUNTIME_DEPLOYED=false
PRODUCTION_CHANGED=false
EMAILS_SENT=0
SMS_SENT=0
```
