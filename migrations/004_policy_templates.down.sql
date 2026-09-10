DROP TABLE IF EXISTS communication_domain_mutations;
DROP TABLE IF EXISTS communication_templates;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS resource_version;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS request_fingerprint;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS idempotency_key;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS active;
ALTER TABLE IF EXISTS communication_consents DROP COLUMN IF EXISTS resource_version;
ALTER TABLE IF EXISTS communication_consents DROP COLUMN IF EXISTS request_fingerprint;
ALTER TABLE IF EXISTS communication_consents DROP COLUMN IF EXISTS idempotency_key;
