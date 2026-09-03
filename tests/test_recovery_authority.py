import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BACKUP = (ROOT / "operations/recovery/backup-postgres.sh").read_text()
RESTORE = (ROOT / "operations/recovery/verify-isolated-restore.sh").read_text()
FRESHNESS = ROOT / "operations/recovery/check-recovery-freshness.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o700)


def _mock_tools(root: Path) -> Path:
    tools = root / "bin"
    tools.mkdir()
    _executable(tools / "psql", 'echo "${MOCK_PSQL_VALUE:-codestra_communication}"\n')
    _executable(tools / "pg_dump", 'for arg in "$@"; do case "$arg" in --file=*) out=${arg#--file=};; esac; done\n: "${out:?}"\nprintf synthetic-dump >"$out"\n')
    _executable(tools / "pg_restore", "exit 0\n")
    _executable(tools / "gpg", 'case " $* " in *" --list-secret-keys "*) exit 0;; *" --verify "*) printf "[GNUPG:] VALIDSIG %s 2026-09-01 0 4 0 1 10 00\n" "${MOCK_SIGNER:?}"; exit 0;; esac\nout=\ninput=\nwhile [ "$#" -gt 0 ]; do case "$1" in --output) out=$2; shift 2;; --recipient|--local-user) shift 2;; --detach-sign) shift;; --*) shift;; *) input=$1; shift;; esac; done\n: "${out:?}"\n: "${input:?}"\ncase "$out" in *.sig) printf synthetic-signature >"$out";; *) cp "$input" "$out";; esac\n')
    _executable(tools / "shred", 'rm -f "${2:?}"\n')
    _executable(tools / "sync", "exit 0\n")
    _executable(tools / "findmnt", "echo tmpfs\n")
    return tools


def test_source_contract_is_encrypted_isolated_and_schema_aware():
    assert "pg_dump" in BACKUP and "--format=custom" in BACKUP
    assert "gpg --batch" in BACKUP and "shred -u" in BACKUP
    assert "--detach-sign" in BACKUP and "SIGNED-MANIFEST.sig" in BACKUP
    assert "POSTGRES_DSN" not in BACKUP + RESTORE
    assert 'sync "$publish/database.dump.gpg"' in BACKUP
    assert 'ALLOW_ISOLATED_RESTORE:-false' in RESTORE
    assert '[[ "$target_database" != "$source_database" ]]' in RESTORE
    for table in ("messages", "communication_consents", "communication_suppressions"):
        assert table in RESTORE
    for boundary in ("idempotency_key", "subject_key", "recipient"):
        assert boundary in RESTORE
    assert "i.indisunique and i.indisvalid and i.indisready" in RESTORE
    assert "cardinality(e.columns)" in RESTORE
    assert "flock -n 8" in RESTORE
    assert "restore evidence stamp collision" in RESTORE
    assert "isolated restore database contains user objects" in RESTORE
    assert "CODESTRA_EXPECTED_RELEASE_SHA" in RESTORE
    assert "plaintext recovery work requires tmpfs" in BACKUP + RESTORE
    assert ".publishing.XXXXXX" in BACKUP
    assert RESTORE.index("backup release SHA mismatch") < RESTORE.index("gpg --batch --quiet --decrypt")
    assert RESTORE.index("isolated restore database contains user objects") < RESTORE.index("gpg --batch --quiet --decrypt")
    assert RESTORE.index("gpg --batch --quiet --decrypt") < RESTORE.index("pg_restore --exit-on-error")
    assert "drop database" not in (BACKUP + RESTORE).lower()


def test_backup_publishes_verified_relocatable_artifacts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = _mock_tools(root)
        backup_root = root / "backups"
        work_root = root / "work"
        work_root.mkdir()
        passfile = root / "pgpass"
        passfile.write_text("synthetic")
        passfile.chmod(0o600)
        result = subprocess.run(
            [str(ROOT / "operations/recovery/backup-postgres.sh")],
            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PGHOST": "synthetic.invalid", "PGPORT": "5432", "PGDATABASE": "codestra_communication", "PGUSER": "synthetic", "PGPASSFILE": str(passfile), "CODESTRA_COMMUNICATION_BACKUP_ROOT": str(backup_root), "CODESTRA_COMMUNICATION_RECOVERY_WORK_ROOT": str(work_root), "CODESTRA_RELEASE_SHA": "1" * 40, "CODESTRA_IMAGE_DIGEST": "sha256:" + "2" * 64, "CODESTRA_BACKUP_GPG_RECIPIENT": "synthetic-test-recipient", "CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT": "A" * 40, "MOCK_SIGNER": "A" * 40},
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        stamp = (backup_root / "LAST_SUCCESS").read_text().strip()
        published = backup_root / stamp
        assert not (published / "database.dump").exists()
        assert (published / "SIGNED-MANIFEST.sig").is_file()
        checked = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=published, text=True, capture_output=True, check=False)
        assert checked.returncode == 0, checked.stderr
        assert str(root) not in (published / "SHA256SUMS").read_text()


def test_restore_refuses_source_identity_before_mutation():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = _mock_tools(root)
        passfile = root / "pgpass"
        passfile.write_text("synthetic")
        passfile.chmod(0o600)
        backup = root / "backup"
        backup.mkdir()
        work_root = root / "work"
        work_root.mkdir()
        (backup / "database.dump.gpg").write_text("synthetic")
        (backup / "METADATA").write_text("SCHEMA=codestra-communication-backup.v1\nSTAMP=20260901T000000Z\nDATABASE=codestra_communication\nRELEASE_SHA=" + "1" * 40 + "\nIMAGE_DIGEST=sha256:" + "2" * 64 + "\nSIGNING_FINGERPRINT=" + "A" * 40 + "\nENCRYPTION=OPENPGP\n")
        with (backup / "SIGNED-MANIFEST").open("w") as manifest:
            subprocess.run(["sha256sum", "database.dump.gpg", "METADATA"], cwd=backup, text=True, stdout=manifest, check=True)
        (backup / "SIGNED-MANIFEST.sig").write_text("synthetic-signature")
        with (backup / "SHA256SUMS").open("w") as manifest:
            subprocess.run(["sha256sum", "database.dump.gpg", "METADATA", "SIGNED-MANIFEST", "SIGNED-MANIFEST.sig"], cwd=backup, text=True, stdout=manifest, check=True)
        result = subprocess.run(
            [str(ROOT / "operations/recovery/verify-isolated-restore.sh")],
            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PGHOST": "synthetic.invalid", "PGPORT": "5432", "PGDATABASE": "codestra_communication", "PGUSER": "synthetic", "PGPASSFILE": str(passfile), "CODESTRA_COMMUNICATION_BACKUP_DIR": str(backup), "CODESTRA_COMMUNICATION_RESTORE_EVIDENCE_DIR": str(root / "evidence"), "CODESTRA_COMMUNICATION_RECOVERY_WORK_ROOT": str(work_root), "CODESTRA_EXPECTED_RELEASE_SHA": "1" * 40, "CODESTRA_EXPECTED_IMAGE_DIGEST": "sha256:" + "2" * 64, "CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT": "A" * 40, "ALLOW_ISOLATED_RESTORE": "true", "MOCK_PSQL_VALUE": "codestra_communication", "MOCK_SIGNER": "A" * 40},
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 2
        assert "refusing restore into source database identity" in result.stderr


def test_freshness_passes_current_marker_and_fails_stale_marker():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = _mock_tools(root)
        current = subprocess.run(["date", "-u", "+%Y%m%dT%H%M%SZ"], text=True, capture_output=True, check=True).stdout.strip()
        artifact = root / current
        artifact.mkdir()
        (artifact / "database.dump.gpg").write_text("synthetic")
        (artifact / "METADATA").write_text("SCHEMA=codestra-communication-backup.v1\nSTAMP=" + current + "\nSIGNING_FINGERPRINT=" + "A" * 40 + "\n")
        with (artifact / "SIGNED-MANIFEST").open("w") as manifest:
            subprocess.run(["sha256sum", "database.dump.gpg", "METADATA"], cwd=artifact, text=True, stdout=manifest, check=True)
        (artifact / "SIGNED-MANIFEST.sig").write_text("synthetic-signature")
        with (artifact / "SHA256SUMS").open("w") as manifest:
            subprocess.run(["sha256sum", "database.dump.gpg", "METADATA", "SIGNED-MANIFEST", "SIGNED-MANIFEST.sig"], cwd=artifact, text=True, stdout=manifest, check=True)
        (root / "LAST_SUCCESS").write_text(current + "\n")
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "CODESTRA_RECOVERY_ROOT": str(root), "CODESTRA_RECOVERY_MAX_AGE_SECONDS": "120", "CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT": "A" * 40, "MOCK_SIGNER": "A" * 40}
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 0
        wrong_signer = {**env, "CODESTRA_BACKUP_GPG_SIGNING_FINGERPRINT": "B" * 40}
        assert subprocess.run([str(FRESHNESS)], env=wrong_signer, capture_output=True).returncode == 1
        (artifact / "database.dump.gpg").write_text("corrupt")
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 1
        (artifact / "database.dump.gpg").write_text("synthetic")
        renamed="20990101T000000Z"
        (root / renamed).mkdir()
        for name in ("database.dump.gpg", "METADATA", "SIGNED-MANIFEST", "SIGNED-MANIFEST.sig", "SHA256SUMS"):
            (root / renamed / name).write_bytes((artifact / name).read_bytes())
        (root / "LAST_SUCCESS").write_text(renamed + "\n")
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 1
        stale="20200101T000000Z"
        (root / stale).mkdir()
        (root / "LAST_SUCCESS").write_text(stale + "\n")
        assert subprocess.run([str(FRESHNESS)], env=env, capture_output=True).returncode == 1


def test_freshness_parser_uses_explicit_utc_timestamp_shape():
    source = FRESHNESS.read_text()
    assert '${stamp:0:4}-${stamp:4:2}-${stamp:6:2}T' in source
    assert 'date -u -d "$stamp_iso"' in source
    assert 'date -u -d "$stamp"' not in source
