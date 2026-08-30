ALTER TABLE messages ADD COLUMN IF NOT EXISTS purpose varchar(32);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS request_fingerprint varchar(64);
UPDATE messages
SET purpose = COALESCE(purpose, 'marketing'),
    request_fingerprint = COALESCE(request_fingerprint, repeat('0', 64));
ALTER TABLE messages ALTER COLUMN purpose SET NOT NULL;
ALTER TABLE messages ALTER COLUMN request_fingerprint SET NOT NULL;
