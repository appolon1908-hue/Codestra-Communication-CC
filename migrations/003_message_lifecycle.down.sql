DROP TABLE IF EXISTS communication_audit_events;
DROP TABLE IF EXISTS communication_message_mutations;
DROP TABLE IF EXISTS communication_message_events;
ALTER TABLE IF EXISTS messages DROP CONSTRAINT IF EXISTS uq_message_tenant_id;
ALTER TABLE IF EXISTS messages DROP COLUMN IF EXISTS cancelled_at;
ALTER TABLE IF EXISTS messages DROP COLUMN IF EXISTS resource_version;
