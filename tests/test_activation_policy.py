from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.activation import (
    apply_fail_closed_environment,
    evaluate_activation,
    sign_receipt,
)


UTC = timezone.utc
SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + ("b" * 64)
NOW = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)


def _receipt(*, approver: str = "independent-reviewer") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "receipt_id": "COMM-CANARY-20260903-001",
        "verdict": "APPROVED",
        "environment": "production",
        "mode": "canary",
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE_DIGEST,
        "channels": ["email", "sms"],
        "canary_percent": 1.0,
        "not_before": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "approver": approver,
        "deployment_operator": "release-operator",
        "approval_url": "https://github.com/appolon1908-hue/Codestra-Communication-CC/issues/10",
        "business_writes_enabled": True,
        "external_delivery_enabled": True,
        "live_email_enabled": True,
        "live_sms_enabled": True,
        "live_pstn_enabled": False,
        "callback_dispatch_enabled": False,
        "n8n_activation_enabled": False,
        "odoo_write_enabled": False,
        "evidence": {
            "source_readback_sha256": "1" * 64,
            "image_readback_sha256": "2" * 64,
            "staging_no_effect_certification_sha256": "3" * 64,
            "provider_health_sha256": "4" * 64,
            "backup_restore_sha256": "5" * 64,
            "rollback_rehearsal_sha256": "6" * 64,
            "observability_sha256": "7" * 64,
            "canary_plan_sha256": "8" * 64,
            "emails_sent_baseline": 0,
            "sms_sent_baseline": 0,
            "callbacks_dispatched_baseline": 0,
            "pstn_calls_placed_baseline": 0,
            "pending_delivery_outbox": 0,
            "reconciliation_required_count": 0,
            "email_provider_status": "ready",
            "sms_provider_status": "ready",
            "canary_scope_id": "tenant-synthetic-canary-001",
        },
    }


def _environment(tmp_path: Path, receipt: dict[str, object]) -> dict[str, str]:
    key = b"k" * 32
    receipt["signature_hmac_sha256"] = sign_receipt(receipt, key)
    receipt_path = tmp_path / "activation-receipt.json"
    key_path = tmp_path / "activation.key"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    key_path.write_bytes(key)
    return {
        "CODESTRA_ENVIRONMENT": "production",
        "CODESTRA_GIT_SHA": SOURCE_SHA,
        "CODESTRA_IMAGE_DIGEST": IMAGE_DIGEST,
        "COMMUNICATION_ACTIVATION_MODE": "canary",
        "COMMUNICATION_CANARY_PERCENT": "1",
        "COMMUNICATION_DEPLOYMENT_OPERATOR": "release-operator",
        "COMMUNICATION_ACTIVATION_RECEIPT_FILE": str(receipt_path),
        "COMMUNICATION_ACTIVATION_HMAC_KEY_FILE": str(key_path),
        "BUSINESS_WRITES_ENABLED": "true",
        "EXTERNAL_DELIVERY_ENABLED": "true",
        "LIVE_EMAIL_DELIVERY": "true",
        "LIVE_SMS_DELIVERY": "true",
        "LIVE_PSTN_DIALING": "false",
        "CALLBACK_DISPATCH": "false",
        "N8N_ACTIVATION": "false",
        "ODOO_WRITE": "false",
    }


def _write_receipt(env: dict[str, str], receipt: dict[str, object]) -> None:
    key = Path(env["COMMUNICATION_ACTIVATION_HMAC_KEY_FILE"]).read_bytes()
    receipt["signature_hmac_sha256"] = sign_receipt(receipt, key)
    Path(env["COMMUNICATION_ACTIVATION_RECEIPT_FILE"]).write_text(
        json.dumps(receipt), encoding="utf-8"
    )


def test_no_requested_effects_default_closed() -> None:
    state = evaluate_activation({"CODESTRA_ENVIRONMENT": "production"}, now=NOW)
    assert state.verdict == "DISABLED"
    assert state.business_writes_enabled is False
    assert state.external_delivery_enabled is False
    assert state.channels == ()


def test_complete_signed_canary_receipt_opens_email_and_sms_only(tmp_path: Path) -> None:
    env = _environment(tmp_path, _receipt())
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "APPROVED_CANARY"
    assert state.business_writes_enabled is True
    assert state.external_delivery_enabled is True
    assert state.channels == ("email", "sms")
    assert state.live_pstn_enabled is False
    assert state.callback_dispatch_enabled is False
    assert state.n8n_activation_enabled is False
    assert state.odoo_write_enabled is False


def test_partial_channel_or_global_flag_request_fails_closed(tmp_path: Path) -> None:
    env = _environment(tmp_path, _receipt())
    env["LIVE_SMS_DELIVERY"] = "false"
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert "email_sms_pair" in state.reason
    assert state.external_delivery_enabled is False


def test_foreign_effect_request_blocks_communication_activation(tmp_path: Path) -> None:
    env = _environment(tmp_path, _receipt())
    env["LIVE_PSTN_DIALING"] = "true"
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason.endswith("LIVE_PSTN_DIALING")


def test_source_and_image_readback_are_exact(tmp_path: Path) -> None:
    env = _environment(tmp_path, _receipt())
    env["CODESTRA_IMAGE_DIGEST"] = "sha256:" + ("c" * 64)
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason == "activation_image_digest_readback_mismatch"


def test_activation_signature_mismatch_is_rejected(tmp_path: Path) -> None:
    receipt = _receipt()
    env = _environment(tmp_path, receipt)
    receipt["canary_percent"] = 0.5
    Path(env["COMMUNICATION_ACTIVATION_RECEIPT_FILE"]).write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    env["COMMUNICATION_CANARY_PERCENT"] = "0.5"
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason == "activation_signature_mismatch"


def test_zero_or_missing_runtime_evidence_cannot_certify(tmp_path: Path) -> None:
    receipt = _receipt()
    env = _environment(tmp_path, receipt)
    receipt["evidence"]["rollback_rehearsal_sha256"] = "0" * 64  # type: ignore[index]
    _write_receipt(env, receipt)
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason.endswith("rollback_rehearsal_sha256")


def test_pending_outbox_or_reconciliation_blocks_activation(tmp_path: Path) -> None:
    receipt = _receipt()
    env = _environment(tmp_path, receipt)
    receipt["evidence"]["pending_delivery_outbox"] = 1  # type: ignore[index]
    _write_receipt(env, receipt)
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason.endswith("pending_delivery_outbox")


def test_self_approval_is_rejected(tmp_path: Path) -> None:
    receipt = _receipt(approver="release-operator")
    env = _environment(tmp_path, receipt)
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason == "activation_self_approval_forbidden"


def test_expired_or_oversized_activation_window_is_rejected(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["not_before"] = (NOW - timedelta(hours=6)).isoformat()
    receipt["expires_at"] = (NOW + timedelta(hours=1)).isoformat()
    env = _environment(tmp_path, receipt)
    state = evaluate_activation(env, now=NOW)
    assert state.verdict == "BLOCKED"
    assert state.reason == "activation_window_exceeds_four_hours"


def test_apply_environment_never_enables_foreign_effects(tmp_path: Path) -> None:
    env = _environment(tmp_path, _receipt())
    state = evaluate_activation(env, now=NOW)
    target = {
        "LIVE_PSTN_DIALING": "true",
        "CALLBACK_DISPATCH": "true",
        "N8N_ACTIVATION": "true",
        "ODOO_WRITE": "true",
    }
    apply_fail_closed_environment(state, target)
    assert target["BUSINESS_WRITES_ENABLED"] == "true"
    assert target["EXTERNAL_DELIVERY_ENABLED"] == "true"
    assert target["LIVE_EMAIL_DELIVERY"] == "true"
    assert target["LIVE_SMS_DELIVERY"] == "true"
    assert target["LIVE_PSTN_DIALING"] == "false"
    assert target["CALLBACK_DISPATCH"] == "false"
    assert target["N8N_ACTIVATION"] == "false"
    assert target["ODOO_WRITE"] == "false"


def test_production_image_uses_guarded_entrypoint() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY sitecustomize.py ./sitecustomize.py" in dockerfile
    assert 'CMD ["app.production:app"' in dockerfile
    for name in (
        "BUSINESS_WRITES_ENABLED=false",
        "EXTERNAL_DELIVERY_ENABLED=false",
        "LIVE_EMAIL_DELIVERY=false",
        "LIVE_SMS_DELIVERY=false",
        "LIVE_PSTN_DIALING=false",
        "CALLBACK_DISPATCH=false",
        "N8N_ACTIVATION=false",
        "ODOO_WRITE=false",
    ):
        assert name in dockerfile
