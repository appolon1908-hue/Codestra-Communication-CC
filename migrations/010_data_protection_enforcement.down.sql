ALTER TABLE IF EXISTS communication_delivery_outbox
  DROP CONSTRAINT IF EXISTS ck_communication_delivery_payload_protected;

ALTER TABLE IF EXISTS communication_templates DROP CONSTRAINT IF EXISTS ck_communication_template_body_protected;
ALTER TABLE IF EXISTS communication_templates ADD CONSTRAINT ck_communication_template_body_protected
  CHECK (body_template IS NOT NULL OR body_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE IF EXISTS communication_templates VALIDATE CONSTRAINT ck_communication_template_body_protected;

ALTER TABLE IF EXISTS communication_preferences DROP CONSTRAINT IF EXISTS ck_communication_preference_subject_protected;
ALTER TABLE IF EXISTS communication_preferences ADD CONSTRAINT ck_communication_preference_subject_protected
  CHECK (subject IS NOT NULL OR subject_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE IF EXISTS communication_preferences VALIDATE CONSTRAINT ck_communication_preference_subject_protected;

ALTER TABLE IF EXISTS communication_suppressions DROP CONSTRAINT IF EXISTS ck_communication_suppression_recipient_protected;
ALTER TABLE IF EXISTS communication_suppressions ADD CONSTRAINT ck_communication_suppression_recipient_protected
  CHECK (recipient IS NOT NULL OR recipient_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE IF EXISTS communication_suppressions VALIDATE CONSTRAINT ck_communication_suppression_recipient_protected;

ALTER TABLE IF EXISTS communication_consents DROP CONSTRAINT IF EXISTS ck_communication_consent_subject_protected;
ALTER TABLE IF EXISTS communication_consents ADD CONSTRAINT ck_communication_consent_subject_protected
  CHECK (subject_key IS NOT NULL OR subject_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE IF EXISTS communication_consents VALIDATE CONSTRAINT ck_communication_consent_subject_protected;

ALTER TABLE IF EXISTS messages DROP CONSTRAINT IF EXISTS ck_messages_recipient_protected;
ALTER TABLE IF EXISTS messages ADD CONSTRAINT ck_messages_recipient_protected
  CHECK (recipient IS NOT NULL OR recipient_ciphertext IS NOT NULL) NOT VALID;
ALTER TABLE IF EXISTS messages VALIDATE CONSTRAINT ck_messages_recipient_protected;
