ALTER TABLE messages ADD COLUMN IF NOT EXISTS operation_id uuid;
CREATE INDEX IF NOT EXISTS ix_messages_operation_id ON messages(operation_id);

CREATE TABLE IF NOT EXISTS communication_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  message_id uuid NOT NULL, kind varchar(48) NOT NULL DEFAULT 'deliver',
  state varchar(32) NOT NULL, idempotency_key varchar(200) NOT NULL,
  correlation_id varchar(128) NOT NULL, attempts integer NOT NULL DEFAULT 0,
  middleware_operation_id varchar(128), error_code varchar(80),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_communication_operation_tenant_id UNIQUE (tenant_id, id),
  CONSTRAINT uq_communication_operation_idempotency UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT fk_communication_operation_message FOREIGN KEY (tenant_id, message_id)
    REFERENCES messages(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_communication_operations_tenant_state
  ON communication_operations(tenant_id, state);

CREATE TABLE IF NOT EXISTS communication_delivery_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  operation_id uuid NOT NULL UNIQUE, payload_json text NOT NULL,
  state varchar(32) NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz,
  last_error_code varchar(80), completed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_communication_outbox_operation FOREIGN KEY (tenant_id, operation_id)
    REFERENCES communication_operations(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_communication_delivery_outbox_claim
  ON communication_delivery_outbox(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS communication_provider_inbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id varchar(128) NOT NULL,
  provider varchar(64) NOT NULL, provider_event_id varchar(160) NOT NULL,
  payload_hash varchar(64) NOT NULL, message_id uuid NOT NULL,
  event_type varchar(64) NOT NULL, state varchar(32) NOT NULL DEFAULT 'processed',
  received_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_communication_provider_inbox_message FOREIGN KEY (tenant_id, message_id)
    REFERENCES messages(tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT uq_communication_provider_event UNIQUE (tenant_id, provider, provider_event_id)
);
CREATE INDEX IF NOT EXISTS ix_communication_provider_inbox_received
  ON communication_provider_inbox(tenant_id, received_at);
