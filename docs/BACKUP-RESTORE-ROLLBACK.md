# Backup, restore, and rollback authority

These source controls do not authorize deployment or production delivery.

Before a migration or rollout, an authorized operator runs the recovery backup
with libpq `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and an owner-protected
`PGPASSFILE`, a root-owned mode-0700 directory, exact release SHA, exact
image digest, approved OpenPGP recovery recipient, and pinned backup-signing
fingerprint. It validates a custom-format dump in a verified tmpfs work root,
encrypts it, destroys the temporary plaintext, and
atomically publishes a signed manifest, relocatable checksums, and
`LAST_SUCCESS`. Freshness binds the marker to authenticated metadata. Database
credentials are never passed as process arguments.

Restore verification requires `ALLOW_ISOLATED_RESTORE=true`, a disposable
empty database whose name contains `restore`, a verified tmpfs restore work
root, and an identity different from the
source backup. The verifier checks artifact hashes, restores with
`--exit-on-error`, requires the exact expected release SHA/image digest, and
proves all three tables, Stage 5 message columns, and the
three tenant/idempotency/consent/suppression uniqueness boundaries before
publishing checksum-bearing evidence.

`check-recovery-freshness.sh` evaluates either backup or restore evidence against
an explicitly supplied RPO/RTO age. Scheduling, off-host replication, approved
RPO/RTO values, current/previous immutable runtime tuples, and a live rehearsal
remain deployment-owner responsibilities.

Application rollback should first redeploy the reviewed previous immutable image
after confirming schema compatibility. Never automatically run down migrations
or restore into production. Database restore requires a separate recovery
decision and must first succeed against an isolated database.
