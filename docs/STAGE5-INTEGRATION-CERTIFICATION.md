# Stage 5 integration certification

The canonical provider-neutral API prefix is `/v1/communications`. Message creation is tenant-bound through `X-Tenant-ID`, requires `Idempotency-Key`, returns the original durable result for semantic retries, and rejects key reuse with changed content.

Recipient normalization, suppression, and purpose-aware consent are enforced before a message record is accepted. Marketing messages require a tenant/channel/recipient consent row with status `granted`; transactional and service messages remain suppression-controlled. Every status read queries by message ID and tenant.

The disposable PostgreSQL gate applies and rolls back both migrations, then proves consent, suppression, duplicate reuse, conflicting reuse, and cross-tenant denial against a real database.

`EXTERNAL_DELIVERY_ENABLED=false` remains the default and the certification environment never changes it. No provider adapter sends email, SMS, WhatsApp, or push traffic during Stage 5.
