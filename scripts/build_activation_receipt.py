#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.activation import evaluate_activation, sign_receipt


UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and self-verify a signed communication canary receipt."
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt-id", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--deployment-operator", required=True)
    parser.add_argument("--approval-url", required=True)
    parser.add_argument("--canary-percent", required=True, type=float)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--expires-at", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise SystemExit("ERROR=ACTIVATION_EVIDENCE_MUST_BE_OBJECT")

    key_path = args.key_file.resolve()
    output_path = args.output.resolve()
    key = key_path.read_bytes().strip()

    receipt: dict[str, object] = {
        "schema_version": "1.0",
        "receipt_id": args.receipt_id,
        "verdict": "APPROVED",
        "environment": "production",
        "mode": "canary",
        "source_sha": args.source_sha,
        "image_digest": args.image_digest,
        "channels": ["email", "sms"],
        "canary_percent": args.canary_percent,
        "not_before": args.not_before,
        "expires_at": args.expires_at,
        "approver": args.approver,
        "deployment_operator": args.deployment_operator,
        "approval_url": args.approval_url,
        "business_writes_enabled": True,
        "external_delivery_enabled": True,
        "live_email_enabled": True,
        "live_sms_enabled": True,
        "live_pstn_enabled": False,
        "callback_dispatch_enabled": False,
        "n8n_activation_enabled": False,
        "odoo_write_enabled": False,
        "evidence": evidence,
    }
    receipt["signature_hmac_sha256"] = sign_receipt(receipt, key)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(output_path, 0o600)

    verification_env = {
        "CODESTRA_ENVIRONMENT": "production",
        "CODESTRA_GIT_SHA": args.source_sha,
        "CODESTRA_IMAGE_DIGEST": args.image_digest,
        "COMMUNICATION_ACTIVATION_MODE": "canary",
        "COMMUNICATION_CANARY_PERCENT": str(args.canary_percent),
        "COMMUNICATION_DEPLOYMENT_OPERATOR": args.deployment_operator,
        "COMMUNICATION_ACTIVATION_RECEIPT_FILE": str(output_path),
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
    state = evaluate_activation(
        verification_env,
        now=datetime.now(UTC),
    )
    if state.verdict != "APPROVED_CANARY":
        output_path.unlink(missing_ok=True)
        raise SystemExit(f"ERROR=ACTIVATION_RECEIPT_SELF_CHECK_FAILED:{state.reason}")

    print("COMMUNICATION_ACTIVATION_RECEIPT=PASS")
    print(f"RECEIPT_ID={state.receipt_id}")
    print(f"SOURCE_SHA={state.source_sha}")
    print(f"IMAGE_DIGEST={state.image_digest}")
    print(f"CANARY_PERCENT={state.canary_percent}")
    print("LIVE_EMAIL_DELIVERY=true")
    print("LIVE_SMS_DELIVERY=true")
    print("LIVE_PSTN_DIALING=false")
    print("CALLBACK_DISPATCH=false")
    print("N8N_ACTIVATION=false")
    print("ODOO_WRITE=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
