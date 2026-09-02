DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM messages WHERE recipient IS NULL)
    OR EXISTS (SELECT 1 FROM communication_consents WHERE subject_key IS NULL)
    OR EXISTS (SELECT 1 FROM communication_suppressions WHERE recipient IS NULL)
    OR EXISTS (SELECT 1 FROM communication_preferences WHERE subject IS NULL)
    OR EXISTS (SELECT 1 FROM communication_templates WHERE body_template IS NULL)
  THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
END $$;
DROP INDEX IF EXISTS ix_messages_recipient_hash;
ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_recipient_protected;
ALTER TABLE messages DROP COLUMN IF EXISTS recipient_ciphertext;
ALTER TABLE messages DROP COLUMN IF EXISTS recipient_hash;
ALTER TABLE messages ALTER COLUMN recipient SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_consent_subject_hash;
ALTER TABLE communication_consents DROP CONSTRAINT IF EXISTS ck_communication_consent_subject_protected;
ALTER TABLE communication_consents DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE communication_consents DROP COLUMN IF EXISTS subject_hash;
ALTER TABLE communication_consents ALTER COLUMN subject_key SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_suppression_recipient_hash;
ALTER TABLE communication_suppressions DROP CONSTRAINT IF EXISTS ck_communication_suppression_recipient_protected;
ALTER TABLE communication_suppressions DROP COLUMN IF EXISTS recipient_ciphertext;
ALTER TABLE communication_suppressions DROP COLUMN IF EXISTS recipient_hash;
ALTER TABLE communication_suppressions ALTER COLUMN recipient SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_preference_subject_hash;
ALTER TABLE communication_preferences DROP CONSTRAINT IF EXISTS ck_communication_preference_subject_protected;
ALTER TABLE communication_preferences DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE communication_preferences DROP COLUMN IF EXISTS subject_hash;
ALTER TABLE communication_preferences ALTER COLUMN subject SET NOT NULL;
ALTER TABLE communication_templates DROP CONSTRAINT IF EXISTS ck_communication_template_body_protected;
ALTER TABLE communication_templates DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE communication_templates DROP COLUMN IF EXISTS body_ciphertext;
ALTER TABLE communication_templates ALTER COLUMN body_template SET NOT NULL;
