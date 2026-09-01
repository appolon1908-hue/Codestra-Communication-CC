#!/usr/bin/env bash
set -euo pipefail

root=${CODESTRA_RECOVERY_ROOT:?recovery root is required}
max_age=${CODESTRA_RECOVERY_MAX_AGE_SECONDS:?maximum age is required}
[[ "$max_age" =~ ^[1-9][0-9]*$ ]] || { echo "maximum age must be a positive integer" >&2; exit 2; }
[[ -f "$root/LAST_SUCCESS" ]] || { echo "recovery success marker is missing" >&2; exit 1; }
stamp=$(tr -d '\r\n' <"$root/LAST_SUCCESS")
[[ "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || { echo "invalid recovery success marker" >&2; exit 1; }
artifact="$root/$stamp"
[[ -d "$artifact" ]] || artifact="$root/RESTORE-RESULT-$stamp"
[[ -e "$artifact" ]] || { echo "recovery evidence target is missing" >&2; exit 1; }
stamp_iso="${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T${stamp:9:2}:${stamp:11:2}:${stamp:13:2}Z"
stamp_epoch=$(date -u -d "$stamp_iso" +%s)
now_epoch=$(date -u +%s)
age=$((now_epoch - stamp_epoch))
(( age >= -300 )) || { echo "recovery marker is unreasonably in the future" >&2; exit 1; }
(( age <= max_age )) || { echo "recovery evidence is stale age_seconds=$age" >&2; exit 1; }
echo "recovery_freshness=PASS age_seconds=$age max_age_seconds=$max_age"
