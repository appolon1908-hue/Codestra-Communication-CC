DO $$ DECLARE unsafe boolean; BEGIN
  IF to_regclass('public.messages') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM messages WHERE recipient IS NULL)' INTO unsafe;
    IF unsafe THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
  END IF;
  IF to_regclass('public.communication_consents') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM communication_consents WHERE subject_key IS NULL)' INTO unsafe;
    IF unsafe THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
  END IF;
  IF to_regclass('public.communication_suppressions') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM communication_suppressions WHERE recipient IS NULL)' INTO unsafe;
    IF unsafe THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
  END IF;
  IF to_regclass('public.communication_preferences') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM communication_preferences WHERE subject IS NULL)' INTO unsafe;
    IF unsafe THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
  END IF;
  IF to_regclass('public.communication_templates') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM communication_templates WHERE body_template IS NULL OR (subject_ciphertext IS NOT NULL AND subject_template IS NULL))' INTO unsafe;
    IF unsafe THEN RAISE EXCEPTION 'plaintext restoration is required before schema rollback'; END IF;
  END IF;
END $$;
DROP INDEX IF EXISTS ix_messages_recipient_hash;
ALTER TABLE IF EXISTS messages DROP CONSTRAINT IF EXISTS ck_messages_recipient_protected;
ALTER TABLE IF EXISTS messages DROP COLUMN IF EXISTS recipient_ciphertext;
ALTER TABLE IF EXISTS messages DROP COLUMN IF EXISTS recipient_hash;
ALTER TABLE IF EXISTS messages ALTER COLUMN recipient SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_consent_subject_hash;
ALTER TABLE IF EXISTS communication_consents DROP CONSTRAINT IF EXISTS ck_communication_consent_subject_protected;
ALTER TABLE IF EXISTS communication_consents DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE IF EXISTS communication_consents DROP COLUMN IF EXISTS subject_hash;
ALTER TABLE IF EXISTS communication_consents ALTER COLUMN subject_key SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_suppression_recipient_hash;
ALTER TABLE IF EXISTS communication_suppressions DROP CONSTRAINT IF EXISTS ck_communication_suppression_recipient_protected;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS recipient_ciphertext;
ALTER TABLE IF EXISTS communication_suppressions DROP COLUMN IF EXISTS recipient_hash;
ALTER TABLE IF EXISTS communication_suppressions ALTER COLUMN recipient SET NOT NULL;
DROP INDEX IF EXISTS uq_communication_preference_subject_hash;
ALTER TABLE IF EXISTS communication_preferences DROP CONSTRAINT IF EXISTS ck_communication_preference_subject_protected;
ALTER TABLE IF EXISTS communication_preferences DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE IF EXISTS communication_preferences DROP COLUMN IF EXISTS subject_hash;
ALTER TABLE IF EXISTS communication_preferences ALTER COLUMN subject SET NOT NULL;
ALTER TABLE IF EXISTS communication_templates DROP CONSTRAINT IF EXISTS ck_communication_template_body_protected;
ALTER TABLE IF EXISTS communication_templates DROP COLUMN IF EXISTS subject_ciphertext;
ALTER TABLE IF EXISTS communication_templates DROP COLUMN IF EXISTS body_ciphertext;
ALTER TABLE IF EXISTS communication_templates ALTER COLUMN body_template SET NOT NULL;
