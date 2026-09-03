from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, MutableMapping


UTC = timezone.utc
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
RECEIPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}$")

REQUEST_FLAGS = (
    "BUSINESS_WRITES_ENABLED",
    "EXTERNAL_DELIVERY_ENABLED",
    "LIVE_EMAIL_DELIVERY",
    "LIVE_SMS_DELIVERY",
)

FOREIGN_OR_UNSUPPORTED_EFFECT_FLAGS = (
    "LIVE_PSTN_DIALING",
    "PSTN_DIALING",
    "CALLBACK_DISPATCH",
    "N8N_ACTIVATION",
    "ODOO_WRITE",
    "LIVE_ODOO_WRITE",
    "VICIDIAL_LIVE_CONTROL",
    "LIVE_WHATSAPP_DELIVERY",
    "LIVE_PUSH_DELIVERY",
)

REQUIRED_EVIDENCE_HASHES = (
    "source_readback_sha256",
    "image_readback_sha256",
    "staging_no_effect_certification_sha256",
    "provider_health_sha256",
    "backup_restore_sha256",
    "rollback_rehearsal_sha256",
    "observability_sha256",
    "canary_plan_sha256",
)


@dataclass(frozen=True)
class ActivationState:
    verdict: str
    reason: str
    environment: str
    mode: str
    receipt_id: str | None
    source_sha: str | None
    image_digest: str | None
    canary_percent: float
    business_writes_enabled: bool
    external_delivery_enabled: bool
    live_email_enabled: bool
    live_sms_enabled: bool
    live_pstn_enabled: bool
    callback_dispatch_enabled: bool
    n8n_activation_enabled: bool
    odoo_write_enabled: bool

    @property
    def channels(self) -> tuple[str, ...]:
        result: list[str] = []
        if self.live_email_enabled:
            result.append("email")
        if self.live_sms_enabled:
            result.append("sms")
        return tuple(result)

    def public_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["channels"] = list(self.channels)
        return result


def _enabled(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "false")).strip().lower() in TRUE_VALUES


def _blocked(
    reason: str,
    *,
    env: Mapping[str, str],
    receipt_id: str | None = None,
    source_sha: str | None = None,
    image_digest: str | None = None,
    canary_percent: float = 0.0,
) -> ActivationState:
    return ActivationState(
        verdict="BLOCKED",
        reason=reason,
        environment=str(env.get("CODESTRA_ENVIRONMENT", "unknown")),
        mode=str(env.get("COMMUNICATION_ACTIVATION_MODE", "disabled")),
        receipt_id=receipt_id,
        source_sha=source_sha,
        image_digest=image_digest,
        canary_percent=canary_percent,
        business_writes_enabled=False,
        external_delivery_enabled=False,
        live_email_enabled=False,
        live_sms_enabled=False,
        live_pstn_enabled=False,
        callback_dispatch_enabled=False,
        n8n_activation_enabled=False,
        odoo_write_enabled=False,
    )


def _disabled(env: Mapping[str, str]) -> ActivationState:
    return ActivationState(
        verdict="DISABLED",
        reason="no_live_effect_requested",
        environment=str(env.get("CODESTRA_ENVIRONMENT", "unknown")),
        mode="disabled",
        receipt_id=None,
        source_sha=str(env.get("CODESTRA_GIT_SHA") or "") or None,
        image_digest=str(env.get("CODESTRA_IMAGE_DIGEST") or "") or None,
        canary_percent=0.0,
        business_writes_enabled=False,
        external_delivery_enabled=False,
        live_email_enabled=False,
        live_sms_enabled=False,
        live_pstn_enabled=False,
        callback_dispatch_enabled=False,
        n8n_activation_enabled=False,
        odoo_write_enabled=False,
    )


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp_missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_missing")
    return parsed.astimezone(UTC)


def canonical_receipt_payload(receipt: Mapping[str, object]) -> bytes:
    unsigned = {
        key: value
        for key, value in receipt.items()
        if key != "signature_hmac_sha256"
    }
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_receipt(receipt: Mapping[str, object], key: bytes) -> str:
    if len(key) < 32:
        raise ValueError("activation_key_too_short")
    return hmac.new(key, canonical_receipt_payload(receipt), hashlib.sha256).hexdigest()


def _load_json(path_value: str) -> dict[str, object]:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("activation_receipt_path_must_be_absolute")
    if path.is_symlink():
        raise ValueError("activation_receipt_symlink_forbidden")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("activation_receipt_must_be_object")
    return document


def _load_key(path_value: str) -> bytes:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError("activation_key_path_must_be_absolute")
    if path.is_symlink():
        raise ValueError("activation_key_symlink_forbidden")
    key = path.read_bytes().strip()
    if len(key) < 32:
        raise ValueError("activation_key_too_short")
    return key


def _parse_canary_percent(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("canary_percent_invalid")
    number = float(value)
    if not 0.0 < number <= 1.0:
        raise ValueError("canary_percent_out_of_range")
    return number


def evaluate_activation(
    env: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> ActivationState:
    effective_env: Mapping[str, str] = os.environ if env is None else env
    requested = {name: _enabled(effective_env, name) for name in REQUEST_FLAGS}

    if not any(requested.values()):
        return _disabled(effective_env)

    for name in FOREIGN_OR_UNSUPPORTED_EFFECT_FLAGS:
        if _enabled(effective_env, name):
            return _blocked(
                f"foreign_or_unsupported_effect_requested:{name}",
                env=effective_env,
            )

    # The present worker has one delivery queue. Until a later independently
    # reviewed per-channel worker split exists, email and SMS must enter the
    # first canary together so a disabled channel can never be consumed from a
    # pre-existing queue while the global worker capability is open.
    if requested != {
        "BUSINESS_WRITES_ENABLED": True,
        "EXTERNAL_DELIVERY_ENABLED": True,
        "LIVE_EMAIL_DELIVERY": True,
        "LIVE_SMS_DELIVERY": True,
    }:
        return _blocked(
            "activation_requires_business_writes_external_delivery_and_email_sms_pair",
            env=effective_env,
        )

    mode = str(effective_env.get("COMMUNICATION_ACTIVATION_MODE", "")).strip().lower()
    if mode != "canary":
        return _blocked("initial_activation_mode_must_be_canary", env=effective_env)

    try:
        configured_canary = _parse_canary_percent(
            effective_env.get("COMMUNICATION_CANARY_PERCENT", "")
        )
    except (TypeError, ValueError):
        return _blocked("configured_canary_percent_invalid", env=effective_env)

    receipt_path = str(
        effective_env.get("COMMUNICATION_ACTIVATION_RECEIPT_FILE", "")
    ).strip()
    key_path = str(
        effective_env.get("COMMUNICATION_ACTIVATION_HMAC_KEY_FILE", "")
    ).strip()
    if not receipt_path or not key_path:
        return _blocked("activation_receipt_or_key_path_missing", env=effective_env)

    try:
        receipt = _load_json(receipt_path)
        key = _load_key(key_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _blocked(f"activation_material_invalid:{exc}", env=effective_env)

    receipt_id = receipt.get("receipt_id")
    receipt_id_text = receipt_id if isinstance(receipt_id, str) else None
    source_sha = receipt.get("source_sha")
    source_sha_text = source_sha if isinstance(source_sha, str) else None
    image_digest = receipt.get("image_digest")
    image_digest_text = image_digest if isinstance(image_digest, str) else None

    if receipt.get("schema_version") != "1.0":
        return _blocked(
            "activation_schema_version_invalid",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if receipt.get("verdict") != "APPROVED":
        return _blocked(
            "activation_receipt_not_approved",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if not receipt_id_text or not RECEIPT_ID_RE.fullmatch(receipt_id_text):
        return _blocked("activation_receipt_id_invalid", env=effective_env)
    if receipt.get("environment") != "production" or effective_env.get(
        "CODESTRA_ENVIRONMENT"
    ) != "production":
        return _blocked(
            "activation_environment_must_be_production",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if receipt.get("mode") != "canary":
        return _blocked(
            "activation_receipt_mode_must_be_canary",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if receipt.get("channels") != ["email", "sms"]:
        return _blocked(
            "activation_channels_must_be_exact_email_sms_pair",
            env=effective_env,
            receipt_id=receipt_id_text,
        )

    try:
        receipt_canary = _parse_canary_percent(receipt.get("canary_percent"))
    except (TypeError, ValueError):
        return _blocked(
            "activation_receipt_canary_percent_invalid",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if receipt_canary != configured_canary:
        return _blocked(
            "activation_canary_percent_mismatch",
            env=effective_env,
            receipt_id=receipt_id_text,
            canary_percent=configured_canary,
        )

    if not source_sha_text or not SHA_RE.fullmatch(source_sha_text):
        return _blocked(
            "activation_source_sha_invalid",
            env=effective_env,
            receipt_id=receipt_id_text,
        )
    if source_sha_text != effective_env.get("CODESTRA_GIT_SHA"):
        return _blocked(
            "activation_source_sha_readback_mismatch",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
        )
    if not image_digest_text or not DIGEST_RE.fullmatch(image_digest_text):
        return _blocked(
            "activation_image_digest_invalid",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
        )
    if image_digest_text != effective_env.get("CODESTRA_IMAGE_DIGEST"):
        return _blocked(
            "activation_image_digest_readback_mismatch",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
            image_digest=image_digest_text,
        )

    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        not_before = _timestamp(receipt.get("not_before"))
        expires_at = _timestamp(receipt.get("expires_at"))
    except (TypeError, ValueError):
        return _blocked(
            "activation_window_invalid",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
            image_digest=image_digest_text,
        )
    if current < not_before or current >= expires_at or expires_at <= not_before:
        return _blocked(
            "activation_window_not_current",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
            image_digest=image_digest_text,
        )
    if (expires_at - not_before).total_seconds() > 4 * 60 * 60:
        return _blocked(
            "activation_window_exceeds_four_hours",
            env=effective_env,
            receipt_id=receipt_id_text,
            source_sha=source_sha_text,
            image_digest=image_digest_text,
        )

    approver = receipt.get("approver")
    operator = receipt.get("deployment_operator")
    if not isinstance(approver, str) or not approver.strip():
        return _blocked("activation_approver_missing", env=effective_env)
    if not isinstance(operator, str) or not operator.strip():
        return _blocked("activation_operator_missing", env=effective_env)
    if approver.strip().casefold() == operator.strip().casefold():
        return _blocked("activation_self_approval_forbidden", env=effective_env)
    if operator != effective_env.get("COMMUNICATION_DEPLOYMENT_OPERATOR"):
        return _blocked("activation_operator_readback_mismatch", env=effective_env)
    approval_url = receipt.get("approval_url")
    if not isinstance(approval_url, str) or not approval_url.startswith(
        "https://github.com/"
    ):
        return _blocked("activation_approval_url_invalid", env=effective_env)

    for field in (
        "business_writes_enabled",
        "external_delivery_enabled",
        "live_email_enabled",
        "live_sms_enabled",
    ):
        if receipt.get(field) is not True:
            return _blocked(
                f"activation_receipt_requires_{field}",
                env=effective_env,
                receipt_id=receipt_id_text,
            )
    for field in (
        "live_pstn_enabled",
        "callback_dispatch_enabled",
        "n8n_activation_enabled",
        "odoo_write_enabled",
    ):
        if receipt.get(field) is not False:
            return _blocked(
                f"activation_receipt_must_keep_{field}_false",
                env=effective_env,
                receipt_id=receipt_id_text,
            )

    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict):
        return _blocked("activation_evidence_missing", env=effective_env)
    for field in REQUIRED_EVIDENCE_HASHES:
        value = evidence.get(field)
        if not isinstance(value, str) or not HASH_RE.fullmatch(value) or set(value) == {"0"}:
            return _blocked(
                f"activation_evidence_hash_invalid:{field}",
                env=effective_env,
                receipt_id=receipt_id_text,
            )
    for field in (
        "emails_sent_baseline",
        "sms_sent_baseline",
        "callbacks_dispatched_baseline",
        "pstn_calls_placed_baseline",
        "pending_delivery_outbox",
        "reconciliation_required_count",
    ):
        if evidence.get(field) != 0:
            return _blocked(
                f"activation_zero_baseline_required:{field}",
                env=effective_env,
                receipt_id=receipt_id_text,
            )
    if evidence.get("email_provider_status") != "ready":
        return _blocked("activation_email_provider_not_ready", env=effective_env)
    if evidence.get("sms_provider_status") != "ready":
        return _blocked("activation_sms_provider_not_ready", env=effective_env)
    if evidence.get("canary_scope_id") in (None, ""):
        return _blocked("activation_canary_scope_missing", env=effective_env)

    supplied_signature = receipt.get("signature_hmac_sha256")
    if not isinstance(supplied_signature, str) or not HASH_RE.fullmatch(
        supplied_signature
    ):
        return _blocked("activation_signature_invalid", env=effective_env)
    try:
        expected_signature = sign_receipt(receipt, key)
    except ValueError as exc:
        return _blocked(f"activation_signature_material_invalid:{exc}", env=effective_env)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return _blocked("activation_signature_mismatch", env=effective_env)

    return ActivationState(
        verdict="APPROVED_CANARY",
        reason="signed_activation_receipt_verified",
        environment="production",
        mode="canary",
        receipt_id=receipt_id_text,
        source_sha=source_sha_text,
        image_digest=image_digest_text,
        canary_percent=receipt_canary,
        business_writes_enabled=True,
        external_delivery_enabled=True,
        live_email_enabled=True,
        live_sms_enabled=True,
        live_pstn_enabled=False,
        callback_dispatch_enabled=False,
        n8n_activation_enabled=False,
        odoo_write_enabled=False,
    )


def apply_fail_closed_environment(
    state: ActivationState,
    env: MutableMapping[str, str] | None = None,
) -> None:
    target = os.environ if env is None else env
    target["BUSINESS_WRITES_ENABLED"] = str(
        state.business_writes_enabled
    ).lower()
    target["EXTERNAL_DELIVERY_ENABLED"] = str(
        state.external_delivery_enabled
    ).lower()
    target["LIVE_EMAIL_DELIVERY"] = str(state.live_email_enabled).lower()
    target["LIVE_SMS_DELIVERY"] = str(state.live_sms_enabled).lower()
    for name in FOREIGN_OR_UNSUPPORTED_EFFECT_FLAGS:
        target[name] = "false"
    target["COMMUNICATION_ACTIVATION_VERDICT"] = state.verdict
    target["COMMUNICATION_ACTIVATION_REASON"] = state.reason
    target["COMMUNICATION_EFFECTIVE_CANARY_PERCENT"] = str(
        state.canary_percent
    )
    if state.receipt_id:
        target["COMMUNICATION_ACTIVATION_RECEIPT_ID"] = state.receipt_id
    else:
        target.pop("COMMUNICATION_ACTIVATION_RECEIPT_ID", None)


def get_activation_state() -> ActivationState:
    return evaluate_activation(os.environ)
