#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${ALLOW_ISOLATED_RESTORE:-false}" == "true" ]] || { echo "isolated restore requires explicit authorization" >&2; exit 2; }
required=(POSTGRES_DSN CODESTRA_COMMUNICATION_BACKUP_DIR CODESTRA_COMMUNICATION_RESTORE_EVIDENCE_DIR)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "required recovery setting is missing: $name" >&2; exit 2; }
done
backup_dir=$CODESTRA_COMMUNICATION_BACKUP_DIR
evidence_root=$CODESTRA_COMMUNICATION_RESTORE_EVIDENCE_DIR
for file in database.dump.gpg METADATA SHA256SUMS; do
  [[ -f "$backup_dir/$file" ]] || { echo "backup artifact is missing: $file" >&2; exit 2; }
done
(cd "$backup_dir" && sha256sum -c SHA256SUMS)
metadata_value() { sed -n "s/^$1=//p" "$backup_dir/METADATA"; }
[[ "$(metadata_value SCHEMA)" == "codestra-communication-backup.v1" ]] || { echo "unsupported backup schema" >&2; exit 2; }
source_database=$(metadata_value DATABASE)
release_sha=$(metadata_value RELEASE_SHA)
image_digest=$(metadata_value IMAGE_DIGEST)
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid backup release SHA" >&2; exit 2; }
[[ "$image_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid backup image digest" >&2; exit 2; }
target_database=$(psql "$POSTGRES_DSN" -XAtq -v ON_ERROR_STOP=1 -c 'select current_database()')
[[ "$target_database" != "$source_database" ]] || { echo "refusing restore into source database identity" >&2; exit 2; }
[[ "$target_database" =~ (^|_)restore(_|$) ]] || { echo "restore target is not explicitly isolated" >&2; exit 2; }

work=$(mktemp -d)
cleanup() { find "$work" -mindepth 1 -delete 2>/dev/null || true; rmdir "$work" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
gpg --batch --quiet --decrypt --output "$work/database.dump" "$backup_dir/database.dump.gpg"
pg_restore --list "$work/database.dump" >/dev/null
pg_restore "$POSTGRES_DSN" --exit-on-error --clean --if-exists --no-owner --no-acl "$work/database.dump"
table_count=$(psql "$POSTGRES_DSN" -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from information_schema.tables where table_schema='public'
and table_name in ('messages','communication_consents','communication_suppressions');
SQL
)
[[ "$table_count" == "3" ]] || { echo "required table verification failed" >&2; exit 1; }
column_count=$(psql "$POSTGRES_DSN" -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from information_schema.columns where table_schema='public'
and table_name='messages' and column_name in ('tenant_id','purpose','idempotency_key','request_fingerprint','status');
SQL
)
[[ "$column_count" == "5" ]] || { echo "required column verification failed" >&2; exit 1; }
unique_index_count=$(psql "$POSTGRES_DSN" -XAtq -v ON_ERROR_STOP=1 <<'SQL'
select count(*) from pg_indexes where schemaname='public' and
((tablename='messages' and indexdef ilike '%unique%' and indexdef like '%tenant_id%' and indexdef like '%idempotency_key%') or
 (tablename='communication_consents' and indexdef ilike '%unique%' and indexdef like '%tenant_id%' and indexdef like '%subject_key%' and indexdef like '%channel%') or
 (tablename='communication_suppressions' and indexdef ilike '%unique%' and indexdef like '%tenant_id%' and indexdef like '%channel%' and indexdef like '%recipient%'));
SQL
)
[[ "$unique_index_count" == "3" ]] || { echo "tenant safety constraint verification failed" >&2; exit 1; }

install -d -m 0700 "$evidence_root"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
result_name="RESTORE-RESULT-$stamp"
result="$evidence_root/.$result_name"
cat >"$result" <<EOF
SCHEMA=codestra-communication-restore-result.v1
STAMP=$stamp
BACKUP_STAMP=$(metadata_value STAMP)
RELEASE_SHA=$release_sha
IMAGE_DIGEST=$image_digest
TARGET_CLASS=ISOLATED
TABLE_VERIFICATION=PASS
COLUMN_VERIFICATION=PASS
TENANT_CONSTRAINTS=PASS
RESTORE=PASS
EOF
chmod 0600 "$result"
mv "$result" "$evidence_root/$result_name"
(cd "$evidence_root" && sha256sum "$result_name" >".$result_name.sha256")
chmod 0600 "$evidence_root/.$result_name.sha256"
mv "$evidence_root/.$result_name.sha256" "$evidence_root/$result_name.sha256"
printf '%s\n' "$stamp" >"$evidence_root/.LAST_SUCCESS-$stamp"
chmod 0600 "$evidence_root/.LAST_SUCCESS-$stamp"
mv "$evidence_root/.LAST_SUCCESS-$stamp" "$evidence_root/LAST_SUCCESS"
sync -d "$evidence_root"
echo "restore=PASS target_class=ISOLATED release_sha=$release_sha image_digest=$image_digest"
