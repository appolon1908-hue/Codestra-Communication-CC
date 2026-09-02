from __future__ import annotations

import os

import pytest

from app.data_protection import DataProtectionError, blind_index, protect, readiness, unprotect


@pytest.fixture
def keys(tmp_path, monkeypatch):
    root = tmp_path / "keys"
    root.mkdir(mode=0o700)
    (root / "key-1.key").write_bytes(b"1" * 32)
    (root / "key-1.key").chmod(0o600)
    (root / "key-2.key").write_bytes(b"2" * 32)
    (root / "key-2.key").chmod(0o600)
    monkeypatch.setenv("COMMUNICATION_DATA_KEY_DIR", str(root))
    monkeypatch.setenv("COMMUNICATION_ACTIVE_DATA_KEY_ID", "key-1")
    monkeypatch.setenv("COMMUNICATION_BLIND_INDEX_KEY_ID", "key-1")
    return root


def test_authenticated_encryption_binds_tenant_and_purpose_and_supports_rotation(keys, monkeypatch):
    envelope = protect("person@example.invalid", tenant_id="tenant-a", purpose="recipient")
    assert "person@example.invalid" not in envelope
    assert unprotect(envelope, tenant_id="tenant-a", purpose="recipient") == "person@example.invalid"
    with pytest.raises(DataProtectionError, match="authentication_failed"):
        unprotect(envelope, tenant_id="tenant-b", purpose="recipient")
    monkeypatch.setenv("COMMUNICATION_ACTIVE_DATA_KEY_ID", "key-2")
    assert unprotect(envelope, tenant_id="tenant-a", purpose="recipient") == "person@example.invalid"
    rotated = protect("person@example.invalid", tenant_id="tenant-a", purpose="recipient")
    assert rotated.startswith("v1:key-2:")


def test_blind_index_remains_stable_when_encryption_write_key_rotates(keys, monkeypatch):
    before = blind_index("person@example.invalid", tenant_id="tenant-a", purpose="recipient")
    monkeypatch.setenv("COMMUNICATION_ACTIVE_DATA_KEY_ID", "key-2")
    assert blind_index("person@example.invalid", tenant_id="tenant-a", purpose="recipient") == before


def test_blind_index_is_context_bound_and_deterministic(keys):
    one = blind_index("person@example.invalid", tenant_id="tenant-a", purpose="recipient")
    assert one == blind_index("person@example.invalid", tenant_id="tenant-a", purpose="recipient")
    assert one != blind_index("person@example.invalid", tenant_id="tenant-b", purpose="recipient")
    assert one != blind_index("person@example.invalid", tenant_id="tenant-a", purpose="consent-subject")


def test_key_source_fails_closed_for_missing_symlink_or_broad_permissions(keys, monkeypatch):
    assert readiness() == (True, "ready")
    (keys / "key-1.key").chmod(0o644)
    assert readiness() == (False, "data_key_permissions_invalid")
    (keys / "key-1.key").unlink()
    os.symlink(keys / "key-2.key", keys / "key-1.key")
    with pytest.raises(DataProtectionError, match="data_key_file_invalid"):
        protect("value", tenant_id="tenant-a", purpose="recipient")
