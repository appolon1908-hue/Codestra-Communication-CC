#!/usr/bin/env bash
set -euo pipefail
umask 077

required=(POSTGRES_DSN CODESTRA_COMMUNICATION_BACKUP_ROOT CODESTRA_RELEASE_SHA CODESTRA_IMAGE_DIGEST CODESTRA_BACKUP_GPG_RECIPIENT)
for name in "${required[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "required recovery setting is missing: $name" >&2; exit 2; }
done
[[ "$CODESTRA_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "release SHA is not immutable" >&2; exit 2; }
[[ "$CODESTRA_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "image digest is not immutable" >&2; exit 2; }

backup_root=$CODESTRA_COMMUNICATION_BACKUP_ROOT
install -d -m 0700 "$backup_root"
exec 9>"$backup_root/.backup.lock"
flock -n 9 || { echo "another backup is active" >&2; exit 3; }
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d "$backup_root/.${stamp}.XXXXXX")
cleanup() { find "$work" -mindepth 1 -delete 2>/dev/null || true; rmdir "$work" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

database_name=$(psql "$POSTGRES_DSN" -XAtq -v ON_ERROR_STOP=1 -c 'select current_database()')
[[ "$database_name" == "codestra_communication" || "$database_name" == "codestra_communication_stage5" ]] || {
  echo "refusing unexpected database identity" >&2
  exit 2
}
pg_dump "$POSTGRES_DSN" --format=custom --no-owner --no-acl --file="$work/database.dump"
pg_restore --list "$work/database.dump" >/dev/null
gpg --batch --yes --trust-model always --recipient "$CODESTRA_BACKUP_GPG_RECIPIENT" \
  --encrypt --output "$work/database.dump.gpg" "$work/database.dump"
shred -u "$work/database.dump"
cat >"$work/METADATA" <<EOF
SCHEMA=codestra-communication-backup.v1
STAMP=$stamp
DATABASE=$database_name
RELEASE_SHA=$CODESTRA_RELEASE_SHA
IMAGE_DIGEST=$CODESTRA_IMAGE_DIGEST
ENCRYPTION=OPENPGP
EOF
(cd "$work" && sha256sum database.dump.gpg METADATA >SHA256SUMS)
chmod 0600 "$work"/*
destination="$backup_root/$stamp"
[[ ! -e "$destination" ]] || { echo "backup stamp collision" >&2; exit 3; }
mv "$work" "$destination"
trap - EXIT INT TERM
sync -d "$destination"
marker="$backup_root/.LAST_SUCCESS-$stamp"
printf '%s\n' "$stamp" >"$marker"
chmod 0600 "$marker"
mv "$marker" "$backup_root/LAST_SUCCESS"
sync -d "$backup_root"
echo "backup=PASS stamp=$stamp release_sha=$CODESTRA_RELEASE_SHA image_digest=$CODESTRA_IMAGE_DIGEST"
