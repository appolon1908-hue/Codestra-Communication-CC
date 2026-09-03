ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_recipient_protected;
ALTER TABLE messages ADD CONSTRAINT ck_messages_recipient_protected
  CHECK (recipient IS NULL AND recipient_ciphertext IS NOT NULL AND recipient_hash IS NOT NULL);

ALTER TABLE communication_consents DROP CONSTRAINT IF EXISTS ck_communication_consent_subject_protected;
ALTER TABLE communication_consents ADD CONSTRAINT ck_communication_consent_subject_protected
  CHECK (subject_key IS NULL AND subject_ciphertext IS NOT NULL AND subject_hash IS NOT NULL);

ALTER TABLE communication_suppressions DROP CONSTRAINT IF EXISTS ck_communication_suppression_recipient_protected;
ALTER TABLE communication_suppressions ADD CONSTRAINT ck_communication_suppression_recipient_protected
  CHECK (recipient IS NULL AND recipient_ciphertext IS NOT NULL AND recipient_hash IS NOT NULL);

ALTER TABLE communication_preferences DROP CONSTRAINT IF EXISTS ck_communication_preference_subject_protected;
ALTER TABLE communication_preferences ADD CONSTRAINT ck_communication_preference_subject_protected
  CHECK (subject IS NULL AND subject_ciphertext IS NOT NULL AND subject_hash IS NOT NULL);

ALTER TABLE communication_templates DROP CONSTRAINT IF EXISTS ck_communication_template_body_protected;
ALTER TABLE communication_templates ADD CONSTRAINT ck_communication_template_body_protected
  CHECK (subject_template IS NULL AND body_template IS NULL AND body_ciphertext IS NOT NULL);

ALTER TABLE communication_delivery_outbox DROP CONSTRAINT IF EXISTS ck_communication_delivery_payload_protected;
ALTER TABLE communication_delivery_outbox ADD CONSTRAINT ck_communication_delivery_payload_protected
  CHECK (payload_json LIKE 'v1:%');

ALTER TABLE communication_domain_mutations
  DROP CONSTRAINT IF EXISTS ck_communication_mutation_key_protected;
ALTER TABLE communication_domain_mutations ADD CONSTRAINT ck_communication_mutation_key_protected
  CHECK (aggregate_key ~ '^[0-9a-f]{64}$' AND aggregate_key_ciphertext LIKE 'v1:%');
