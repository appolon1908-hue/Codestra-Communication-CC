"""Process-wide fail-closed live-effect bootstrap.

Python imports ``sitecustomize`` during interpreter startup. That makes this
control apply to the API, delivery worker, migration helpers, and any alternate
entry point instead of protecting only one Uvicorn command.
"""

from __future__ import annotations

import os


_EFFECT_FLAGS = (
    "BUSINESS_WRITES_ENABLED",
    "EXTERNAL_DELIVERY_ENABLED",
    "LIVE_EMAIL_DELIVERY",
    "LIVE_SMS_DELIVERY",
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


def _force_closed(reason: str) -> None:
    for name in _EFFECT_FLAGS:
        os.environ[name] = "false"
    os.environ["COMMUNICATION_ACTIVATION_VERDICT"] = "BLOCKED"
    os.environ["COMMUNICATION_ACTIVATION_REASON"] = reason
    os.environ["COMMUNICATION_EFFECTIVE_CANARY_PERCENT"] = "0.0"
    os.environ.pop("COMMUNICATION_ACTIVATION_RECEIPT_ID", None)


try:
    from app.activation import apply_fail_closed_environment, evaluate_activation

    apply_fail_closed_environment(evaluate_activation(os.environ), os.environ)
except Exception as exc:  # pragma: no cover - defensive startup boundary
    _force_closed(f"activation_bootstrap_failure:{type(exc).__name__}")
