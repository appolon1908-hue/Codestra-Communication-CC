# Backup, restore, and rollback authority

These source controls do not authorize deployment or production delivery.

Before a migration or rollout, an authorized operator runs the recovery backup
with a protected DSN, root-owned mode-0700 directory, exact release SHA, exact
image digest, and approved OpenPGP recovery recipient. It validates a
custom-format dump, encrypts it, destroys the temporary plaintext, and
atomically publishes relocatable checksums and `LAST_SUCCESS`.

Restore verification requires `ALLOW_ISOLATED_RESTORE=true`, a disposable
database whose name contains `restore`, and an identity different from the
source backup. The verifier checks artifact hashes, restores with
`--exit-on-error`, and proves all three tables, Stage 5 message columns, and the
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
