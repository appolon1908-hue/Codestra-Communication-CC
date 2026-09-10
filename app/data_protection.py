from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import stat
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


class DataProtectionError(RuntimeError):
    pass


_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise DataProtectionError("protected_value_invalid") from exc


def _root() -> Path:
    configured = os.getenv("COMMUNICATION_DATA_KEY_DIR", "").strip()
    if not configured:
        raise DataProtectionError("data_key_directory_missing")
    root = Path(configured)
    if (
        not root.is_absolute() or root.is_symlink() or not root.is_dir()
        or root.resolve() != root
    ):
        raise DataProtectionError("data_key_directory_invalid")
    details = root.stat()
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise DataProtectionError("data_key_directory_permissions_invalid")
    return root


def _active_key_id() -> str:
    value = os.getenv("COMMUNICATION_ACTIVE_DATA_KEY_ID", "").strip()
    if not _KEY_ID.fullmatch(value):
        raise DataProtectionError("active_data_key_id_invalid")
    return value


def _blind_index_key_id() -> str:
    value = os.getenv("COMMUNICATION_BLIND_INDEX_KEY_ID", "").strip()
    if not _KEY_ID.fullmatch(value):
        raise DataProtectionError("blind_index_key_id_invalid")
    return value


def _key(key_id: str) -> bytes:
    if not _KEY_ID.fullmatch(key_id):
        raise DataProtectionError("data_key_id_invalid")
    root = _root()
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(
                f"{key_id}.key", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise DataProtectionError("data_key_file_invalid") from exc
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
                raise DataProtectionError("data_key_file_invalid")
            if stat.S_IMODE(details.st_mode) & 0o077:
                raise DataProtectionError("data_key_permissions_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(4097)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    if len(raw) != 32:
        try:
            encoded = raw.decode("ascii", "strict").strip()
        except UnicodeDecodeError as exc:
            raise DataProtectionError("data_key_length_invalid") from exc
        raw = _b64decode(encoded)
    if len(raw) != 32:
        raise DataProtectionError("data_key_length_invalid")
    return raw


def _aad(*, tenant_id: str, purpose: str) -> bytes:
    if not tenant_id or len(tenant_id) > 128 or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", purpose):
        raise DataProtectionError("data_protection_context_invalid")
    return f"codestra-communication\0v1\0{tenant_id}\0{purpose}".encode()


def protect(value: str, *, tenant_id: str, purpose: str) -> str:
    if not isinstance(value, str):
        raise DataProtectionError("protected_value_invalid")
    key_id = _active_key_id()
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(key_id)).encrypt(nonce, value.encode("utf-8"), _aad(tenant_id=tenant_id, purpose=purpose))
    return f"v1:{key_id}:{_b64encode(nonce + ciphertext)}"


def unprotect(envelope: str, *, tenant_id: str, purpose: str) -> str:
    try:
        version, key_id, encoded = envelope.split(":", 2)
    except (AttributeError, ValueError) as exc:
        raise DataProtectionError("protected_value_invalid") from exc
    if version != "v1":
        raise DataProtectionError("protected_value_version_unsupported")
    packed = _b64decode(encoded)
    if len(packed) < 29:
        raise DataProtectionError("protected_value_invalid")
    try:
        cleartext = AESGCM(_key(key_id)).decrypt(
            packed[:12], packed[12:], _aad(tenant_id=tenant_id, purpose=purpose)
        )
        return cleartext.decode("utf-8")
    except Exception as exc:
        raise DataProtectionError("protected_value_authentication_failed") from exc


def blind_index(value: str, *, tenant_id: str, purpose: str) -> str:
    # This key identity is deliberately independent from the rotating AEAD write
    # key. Changing it requires an explicit index-rebuild migration.
    active = _key(_blind_index_key_id())
    index_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"codestra-communication-blind-index-v1",
    ).derive(active)
    material = _aad(tenant_id=tenant_id, purpose=purpose) + b"\0" + value.encode("utf-8")
    return hmac.new(index_key, material, hashlib.sha256).hexdigest()


def readiness() -> tuple[bool, str]:
    try:
        _key(_active_key_id())
        _key(_blind_index_key_id())
    except DataProtectionError as exc:
        return False, str(exc)
    return True, "ready"


def reveal(
    *, ciphertext: str | None, legacy_plaintext: str | None,
    tenant_id: str, purpose: str,
) -> str:
    if ciphertext:
        return unprotect(ciphertext, tenant_id=tenant_id, purpose=purpose)
    allow_legacy = os.getenv("COMMUNICATION_ALLOW_LEGACY_PLAINTEXT_READS", "false").lower() == "true"
    if allow_legacy and legacy_plaintext is not None:
        return legacy_plaintext
    raise DataProtectionError("protected_value_missing")
