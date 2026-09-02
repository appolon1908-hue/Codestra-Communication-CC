ALTER TABLE messages ALTER COLUMN recipient DROP NOT NULL;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_ciphertext text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_hash varchar(64);
CREATE INDEX IF NOT EXISTS ix_messages_recipient_hash ON messages(tenant_id, recipient_hash);
ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_recipient_protected;
ALTER TABLE messages ADD CONSTRAINT ck_messages_recipient_protected
  CHECK (recipient IS NOT NULL OR recipient_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE messages VALIDATE CONSTRAINT ck_messages_recipient_protected;

ALTER TABLE communication_consents ALTER COLUMN subject_key DROP NOT NULL;
ALTER TABLE communication_consents ADD COLUMN IF NOT EXISTS subject_ciphertext text;
ALTER TABLE communication_consents ADD COLUMN IF NOT EXISTS subject_hash varchar(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_communication_consent_subject_hash
  ON communication_consents(tenant_id, subject_hash, channel) WHERE subject_hash IS NOT NULL;
ALTER TABLE communication_consents DROP CONSTRAINT IF EXISTS ck_communication_consent_subject_protected;
ALTER TABLE communication_consents ADD CONSTRAINT ck_communication_consent_subject_protected
  CHECK (subject_key IS NOT NULL OR subject_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE communication_consents VALIDATE CONSTRAINT ck_communication_consent_subject_protected;

ALTER TABLE communication_suppressions ALTER COLUMN recipient DROP NOT NULL;
ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS recipient_ciphertext text;
ALTER TABLE communication_suppressions ADD COLUMN IF NOT EXISTS recipient_hash varchar(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_communication_suppression_recipient_hash
  ON communication_suppressions(tenant_id, channel, recipient_hash) WHERE recipient_hash IS NOT NULL;
ALTER TABLE communication_suppressions DROP CONSTRAINT IF EXISTS ck_communication_suppression_recipient_protected;
ALTER TABLE communication_suppressions ADD CONSTRAINT ck_communication_suppression_recipient_protected
  CHECK (recipient IS NOT NULL OR recipient_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE communication_suppressions VALIDATE CONSTRAINT ck_communication_suppression_recipient_protected;

ALTER TABLE communication_preferences ALTER COLUMN subject DROP NOT NULL;
ALTER TABLE communication_preferences ADD COLUMN IF NOT EXISTS subject_ciphertext text;
ALTER TABLE communication_preferences ADD COLUMN IF NOT EXISTS subject_hash varchar(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_communication_preference_subject_hash
  ON communication_preferences(tenant_id, subject_hash, channel, topic) WHERE subject_hash IS NOT NULL;
ALTER TABLE communication_preferences DROP CONSTRAINT IF EXISTS ck_communication_preference_subject_protected;
ALTER TABLE communication_preferences ADD CONSTRAINT ck_communication_preference_subject_protected
  CHECK (subject IS NOT NULL OR subject_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE communication_preferences VALIDATE CONSTRAINT ck_communication_preference_subject_protected;

ALTER TABLE communication_templates ALTER COLUMN body_template DROP NOT NULL;
ALTER TABLE communication_templates ADD COLUMN IF NOT EXISTS subject_ciphertext text;
ALTER TABLE communication_templates ADD COLUMN IF NOT EXISTS body_ciphertext text;
ALTER TABLE communication_templates DROP CONSTRAINT IF EXISTS ck_communication_template_body_protected;
ALTER TABLE communication_templates ADD CONSTRAINT ck_communication_template_body_protected
  CHECK (body_template IS NOT NULL OR body_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE communication_templates VALIDATE CONSTRAINT ck_communication_template_body_protected;
