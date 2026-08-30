CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL, channel varchar(32) NOT NULL,
  recipient varchar(512) NOT NULL, template_key varchar(160) NOT NULL, idempotency_key varchar(200) NOT NULL,
  status varchar(48) NOT NULL, provider_message_id varchar(160), created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_messages_tenant_status ON messages(tenant_id, status);
CREATE TABLE IF NOT EXISTS communication_consents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL, subject_key varchar(256) NOT NULL,
  channel varchar(32) NOT NULL, status varchar(24) NOT NULL, source varchar(128) NOT NULL, evidence text,
  updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE (tenant_id, subject_key, channel)
);
CREATE TABLE IF NOT EXISTS communication_suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL, channel varchar(32) NOT NULL,
  recipient varchar(512) NOT NULL, reason varchar(128) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, channel, recipient)
);
