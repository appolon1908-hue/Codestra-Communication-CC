CREATE TABLE IF NOT EXISTS communication_preferences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(128) NOT NULL,
  subject varchar(300) NOT NULL,
  channel varchar(32) NOT NULL,
  topic varchar(120) NOT NULL DEFAULT '',
  consent varchar(16) NOT NULL,
  source varchar(120) NOT NULL DEFAULT 'unspecified',
  metadata_json text NOT NULL DEFAULT '{}',
  idempotency_key varchar(200) NOT NULL,
  request_fingerprint varchar(64) NOT NULL,
  resource_version integer NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_preference UNIQUE (tenant_id, subject, channel, topic),
  CONSTRAINT uq_communication_preference_idempotency UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT ck_communication_preference_consent CHECK (consent IN ('granted','denied','unknown'))
);
CREATE INDEX IF NOT EXISTS ix_communication_preferences_tenant_subject
  ON communication_preferences(tenant_id, subject, id);
