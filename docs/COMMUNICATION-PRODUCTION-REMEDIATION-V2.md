# Codestra Communication Production Remediation V2

## Authority and objective

This branch starts from `development@ab666e24a7a338535130a458eff7faf197e162d2`, the consolidated Communication API candidate. Its sole purpose is to close every known correctness, privacy, idempotency, recovery and contract gap before any promotion from development.

Merging the candidate into development did not certify it for staging or production. This branch must implement the fixes below in code, migrations, tests and runbooks.

## 1. Serialize suppression and consent policy with message admission

Message creation, consent revocation, suppression creation/reactivation and preference changes must serialize on a shared tenant/channel/normalized-recipient policy identity even when no aggregate row yet exists.

Use a PostgreSQL transaction advisory lock or an equivalent durable lock keyed from tenant, channel and a privacy-safe blind index. Re-evaluate consent, suppression, sender and preference policy while holding the same lock immediately before inserting the message and operation.

Required concurrency tests:

- message admission racing consent revocation cannot commit after revocation wins;
- message admission racing suppression creation/reactivation cannot bypass suppression;
- no-row races are serialized;
- unrelated tenant/recipient identities do not block one another;
- retries are deterministic and tenant scoped.

## 2. Channel-specific routing truth

The capability and provider-health surfaces must not advertise unsupported channels as Middleware-routed. Email and SMS may be described as Middleware-routed only when their explicit capability is enabled and configured. WhatsApp, push and any other unimplemented channel must remain disabled and message creation must reject them before queuing external-delivery work.

Required tests prove unsupported channels create no operation, no outbox row and no provider request.

## 3. Deterministic mutation replay

Every consent, suppression, preference, template, sender and domain mutation must preserve the original result associated with its tenant-scoped idempotency key. When later mutations advance the resource, replaying an older key must either return the stored original response snapshot or return a deterministic `409 idempotency_result_superseded`; it must never attribute current state to the old operation.

Concurrent reuse of one key for different semantic requests must return a deterministic conflict, not a generic 500.

Persist the minimum privacy-safe response/version snapshot needed for replay and cover every mutation family with sequential and concurrent tests.

## 4. Public lifecycle propagation for pre-dispatch failures

When a delivery payload cannot be decrypted, is corrupt, lacks a required key, fails validation or otherwise becomes terminal before Middleware dispatch, update the locked message, operation, outbox and immutable lifecycle/audit event in the same transaction.

Callers must never observe a message permanently stuck in `queued` or `cancellation_pending` when the command has terminally failed. The public state must distinguish terminal failure from reconciliation-required uncertainty.

## 5. Complete OAuth scope contract

The committed OpenAPI client-credentials flow must declare every scope enforced by runtime operations, including message, template, consent, suppression, preference, sender, domain, usage, provider-health and reputation operations.

CI must compare runtime scope dependencies, OpenAPI security declarations and the Keycloak platform scope authority. Unknown, wildcard, undocumented and unused scopes fail validation.

## 6. Atomic telemetry credential-file validation

Do not validate a telemetry certificate/key pathname and later let the exporter reopen an unbound path. Require a trusted, non-group/world-writable parent directory owned by the runtime identity and open files with no-follow semantics. Validate type, owner, mode and inode on the opened descriptor. Either pass descriptor-backed data to the exporter through a protected runtime file or retain a verified atomic identity until exporter initialization.

Symlink replacement, writable-directory replacement and wrong-owner/wrong-mode negative tests are mandatory. No key material may be logged.

## 7. Backup integrity across a storage boundary

Backup certification must:

1. create a logical dump and checksum;
2. copy the dump and checksum to an independent directory or artifact boundary;
3. verify the copied checksum against the copied bytes;
4. restore only from the copied artifact into a separately named disposable database;
5. compare every populated table, required row identity and expected schema object;
6. verify columns, constraints, indexes, migration ledger and protected-data representation;
7. record RPO/RTO and delete the disposable database.

Checking a checksum against the same local dump immediately after generation is insufficient.

The restore comparison must cover messages, lifecycle events, templates, consent, suppression, preferences, senders/domains, operations, outboxes, provider inbox, idempotency/mutation records and every other populated table.

## 8. Existing resolved review protections remain mandatory

Preserve the existing fixes for:

- global read-only kill switch on every business mutation;
- unknown-outcome reconciliation before retry;
- signed provider event identity and replay protection;
- monotonic lifecycle transitions, including reconciliation resolution;
- terminal failure propagation;
- stable blind indexes across AEAD key rotation;
- atomic data-key loading;
- protected mutation aggregate keys;
- safe data-protection migration/rollback certification;
- correlation-header OpenAPI declaration;
- cancellation using the accepted Middleware operation identity.

Regression tests must prove none are lost.

## 9. Required validation

- format, lint and type checks;
- complete unit and contract suite;
- disposable PostgreSQL certification;
- migration apply, apply twice, upgrade and rollback/restore;
- tenant-isolation and authorization negative tests;
- idempotency concurrency tests for every mutation family;
- message-policy race tests;
- provider unknown-outcome and callback reconciliation tests;
- OpenAPI/AsyncAPI/runtime parity;
- OAuth scope parity;
- data-protection and telemetry-file security tests;
- independent-storage backup/restore certification;
- deterministic locked dependency install;
- non-root immutable container build;
- secret scan, SBOM, provenance and HIGH/CRITICAL vulnerability policy;
- `git diff --check`.

## 10. Fail-closed baseline

```text
BUSINESS_WRITES_ENABLED=false
EXTERNAL_DELIVERY_ENABLED=false
TELEMETRY_EXPORT_ENABLED=false
LIVE_EMAIL_DELIVERY=false
LIVE_SMS_DELIVERY=false
EMAILS_SENT=0
SMS_SENT=0
RUNTIME_DEPLOYED=false
PRODUCTION_CHANGED=false
```

No real provider, SMTP, SMS, voice, Odoo, n8n or production call is allowed in tests. External delivery remains a separately reviewed activation program.

## Completion gate

This PR may leave draft only after all findings are implemented, every current review thread has a fixing commit or is superseded by equivalent evidence, exact-head and synthetic merge-result checks pass, the protected PostgreSQL/container/recovery gates pass, and an independent reviewer with write access approves the unchanged head.

Promotion to test/staging/main and runtime deployment remain separate operations.