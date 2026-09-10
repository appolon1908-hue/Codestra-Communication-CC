CREATE TABLE IF NOT EXISTS communication_event_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id bigint NOT NULL UNIQUE,
  topic varchar(160) NOT NULL,
  payload_json text NOT NULL,
  state varchar(24) NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  last_error_code varchar(80),
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_communication_event_outbox_event FOREIGN KEY (event_id)
    REFERENCES communication_message_events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_communication_event_outbox_claim
  ON communication_event_outbox(state, available_at, lease_until);
