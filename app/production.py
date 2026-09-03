from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import main as core
from .activation import get_activation_state


app = core.app
_original_capabilities = core.capabilities


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


# sitecustomize has already reduced every requested effect to its verified
# effective value. Bind the legacy module globals to that same process-wide
# result so every existing route remains fail closed.
core.BUSINESS_WRITES_ENABLED = _env_enabled("BUSINESS_WRITES_ENABLED")
core.EXTERNAL_DELIVERY_ENABLED = _env_enabled("EXTERNAL_DELIVERY_ENABLED")


# Replace the legacy capability handlers, which intentionally hard-coded all
# live channels false, with exact effective-state readback. Removing only these
# routes preserves the rest of the canonical API and its OpenAPI contract.
_CAPABILITY_PATHS = {"/capabilities", "/v1/communications/capabilities"}
app.router.routes[:] = [
    route
    for route in app.router.routes
    if getattr(route, "path", None) not in _CAPABILITY_PATHS
]


def _activation_public_state() -> dict[str, object]:
    state = get_activation_state().public_dict()
    bootstrap_verdict = os.getenv("COMMUNICATION_ACTIVATION_VERDICT")
    if bootstrap_verdict == "BLOCKED" and state["verdict"] == "DISABLED":
        state.update(
            {
                "verdict": "BLOCKED",
                "reason": os.getenv(
                    "COMMUNICATION_ACTIVATION_REASON",
                    "activation_bootstrap_blocked",
                ),
                "canary_percent": 0.0,
                "business_writes_enabled": False,
                "external_delivery_enabled": False,
                "live_email_enabled": False,
                "live_sms_enabled": False,
            }
        )
    return state


def production_capabilities(request: Request | None = None) -> dict[str, object]:
    value = dict(_original_capabilities(request))
    activation = _activation_public_state()
    value.update(
        {
            "business_writes_enabled": activation["business_writes_enabled"],
            "external_delivery_enabled": activation["external_delivery_enabled"],
            "live_email_enabled": activation["live_email_enabled"],
            "live_sms_enabled": activation["live_sms_enabled"],
            "live_pstn_enabled": False,
            "read_only_mode": not bool(activation["business_writes_enabled"]),
            "simulation_enabled": not bool(activation["external_delivery_enabled"]),
            "external_delivery": activation["external_delivery_enabled"],
            "activation_verdict": activation["verdict"],
            "activation_reason": activation["reason"],
            "activation_mode": activation["mode"],
            "activation_receipt_id": activation["receipt_id"],
            "activation_canary_percent": activation["canary_percent"],
            "activation_channels": activation["channels"],
            "callback_dispatch_enabled": False,
            "n8n_activation_enabled": False,
            "odoo_write_enabled": False,
        }
    )
    return value


app.add_api_route(
    "/capabilities",
    production_capabilities,
    methods=["GET"],
    name="production_capabilities",
)
app.add_api_route(
    "/v1/communications/capabilities",
    production_capabilities,
    methods=["GET"],
    name="production_communications_capabilities",
)


@app.get("/activation/status", include_in_schema=False)
def activation_status() -> dict[str, object]:
    return _activation_public_state()


@app.middleware("http")
async def activation_boundary(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    activation = _activation_public_state()

    # The current release intentionally activates email and SMS as one bounded
    # canary pair because the worker has one shared outbox. Unsupported channels
    # cannot enter the queue after the global delivery capability opens.
    if (
        request.method == "POST"
        and request.url.path == "/v1/communications/messages"
        and bool(activation["external_delivery_enabled"])
    ):
        body = await request.body()
        try:
            document = json.loads(body or b"{}")
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"detail": "request_body_invalid_json"},
            )
        channel = document.get("channel") if isinstance(document, dict) else None
        if channel not in set(activation["channels"]):
            return JSONResponse(
                status_code=423,
                content={
                    "detail": "channel_not_approved_for_activation",
                    "activation_receipt_id": activation["receipt_id"],
                },
            )

        sent = False

        async def receive() -> dict[str, object]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(request.scope, receive)

    response = await call_next(request)
    response.headers["X-Codestra-Activation-Verdict"] = str(
        activation["verdict"]
    )
    response.headers["X-Codestra-Activation-Mode"] = str(activation["mode"])
    response.headers["X-Codestra-Canary-Percent"] = str(
        activation["canary_percent"]
    )
    if activation["receipt_id"]:
        response.headers["X-Codestra-Activation-Receipt"] = str(
            activation["receipt_id"]
        )
    return response
