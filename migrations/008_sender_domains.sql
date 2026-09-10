CREATE TABLE IF NOT EXISTS communication_sending_domains (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  domain varchar(253) NOT NULL, status varchar(32) NOT NULL DEFAULT 'dns_required',
  spf varchar(24) NOT NULL DEFAULT 'pending', dkim varchar(24) NOT NULL DEFAULT 'pending',
  dmarc varchar(24) NOT NULL DEFAULT 'pending', reverse_dns varchar(24) NOT NULL DEFAULT 'not_configured',
  tls varchar(24) NOT NULL DEFAULT 'not_configured', bimi varchar(24) NOT NULL DEFAULT 'not_configured',
  metadata_json text NOT NULL DEFAULT '{}', idempotency_key varchar(200) NOT NULL,
  request_fingerprint varchar(64) NOT NULL, resource_version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_sending_domain UNIQUE (tenant_id, domain),
  CONSTRAINT uq_communication_domain_idempotency UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT uq_communication_domain_tenant_id UNIQUE (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS ix_communication_domains_tenant_id ON communication_sending_domains(tenant_id, id);

CREATE TABLE IF NOT EXISTS communication_sender_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  channel varchar(32) NOT NULL, address varchar(300) NOT NULL, display_name varchar(160),
  domain_id uuid, metadata_json text NOT NULL DEFAULT '{}', status varchar(24) NOT NULL DEFAULT 'pending',
  idempotency_key varchar(200) NOT NULL, request_fingerprint varchar(64) NOT NULL,
  resource_version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_sender_identity UNIQUE (tenant_id, channel, address),
  CONSTRAINT uq_communication_sender_tenant_id UNIQUE (tenant_id, id),
  CONSTRAINT uq_communication_sender_idempotency UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT fk_communication_sender_domain FOREIGN KEY (tenant_id, domain_id)
    REFERENCES communication_sending_domains(tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_communication_senders_tenant_id ON communication_sender_identities(tenant_id, id);
