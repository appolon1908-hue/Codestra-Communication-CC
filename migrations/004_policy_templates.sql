ALTER TABLE communication_consents ADD COLUMN IF NOT EXISTS idempotency_key varchar(200);
ALTER TABLE communication_consents ADD COLUMN IF NOT EXISTS request_fingerprint varchar(64);
ALTER TABLE communication_consents ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1;
UPDATE communication_consents SET
  idempotency_key = COALESCE(idempotency_key, 'legacy-' || id::text),
  request_fingerprint = COALESCE(request_fingerprint, repeat('0', 64));
ALTER TABLE communication_consents ALTER COLUMN idempotency_key SET NOT NULL;
ALTER TABLE communication_consents ALTER COLUMN request_fingerprint SET NOT NULL;

ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS idempotency_key varchar(200);
ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS request_fingerprint varchar(64);
ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1;
UPDATE communication_suppressions SET
  idempotency_key = COALESCE(idempotency_key, 'legacy-' || id::text),
  request_fingerprint = COALESCE(request_fingerprint, repeat('0', 64));
ALTER TABLE communication_suppressions ALTER COLUMN idempotency_key SET NOT NULL;
ALTER TABLE communication_suppressions ALTER COLUMN request_fingerprint SET NOT NULL;

CREATE TABLE IF NOT EXISTS communication_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  key varchar(160) NOT NULL, channel varchar(32) NOT NULL, locale varchar(24) NOT NULL DEFAULT 'en',
  subject_template text, body_template text NOT NULL, active boolean NOT NULL DEFAULT true,
  idempotency_key varchar(200) NOT NULL, request_fingerprint varchar(64) NOT NULL,
  resource_version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_template_key_locale UNIQUE (tenant_id, key, locale),
  CONSTRAINT uq_communication_template_idempotency UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_communication_templates_tenant ON communication_templates(tenant_id);

CREATE TABLE IF NOT EXISTS communication_domain_mutations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  aggregate_type varchar(64) NOT NULL, aggregate_key varchar(256) NOT NULL,
  mutation_type varchar(64) NOT NULL, idempotency_key varchar(200) NOT NULL,
  request_fingerprint varchar(64) NOT NULL, result_version integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_domain_mutation UNIQUE (tenant_id, mutation_type, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_communication_domain_mutations_tenant
  ON communication_domain_mutations(tenant_id, aggregate_type, aggregate_key);
