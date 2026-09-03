DROP TABLE IF EXISTS communication_provider_inbox;
DROP TABLE IF EXISTS communication_delivery_outbox;
DROP TABLE IF EXISTS communication_operations;
DROP INDEX IF EXISTS ix_messages_operation_id;
ALTER TABLE IF EXISTS messages DROP COLUMN IF EXISTS operation_id;
