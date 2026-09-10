from __future__ import annotations

from app import production


def test_guarded_wrapper_preserves_application_and_routes() -> None:
    assert production.app.title == "Codestra Communication API"
    capability_paths = [
        route.path
        for route in production.app.router.routes
        if getattr(route, "path", None)
        in {"/capabilities", "/v1/communications/capabilities"}
    ]
    assert sorted(capability_paths) == [
        "/capabilities",
        "/v1/communications/capabilities",
    ]
    assert any(
        getattr(route, "path", None) == "/activation/status"
        for route in production.app.router.routes
    )


def test_default_capability_readback_is_fail_closed() -> None:
    value = production.production_capabilities()
    assert value["business_writes_enabled"] is False
    assert value["external_delivery_enabled"] is False
    assert value["live_email_enabled"] is False
    assert value["live_sms_enabled"] is False
    assert value["live_pstn_enabled"] is False
    assert value["callback_dispatch_enabled"] is False
    assert value["n8n_activation_enabled"] is False
    assert value["odoo_write_enabled"] is False
    assert value["activation_verdict"] in {"DISABLED", "BLOCKED"}
    assert value["activation_channels"] == []


def test_production_capabilities_retain_correlation_contract():
    from fastapi.testclient import TestClient

    client = TestClient(production.app)
    missing = client.get("/v1/communications/capabilities")
    assert missing.status_code == 422
    accepted = client.get(
        "/v1/communications/capabilities",
        headers={"X-Correlation-ID": "activation-contract-check"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["external_delivery_enabled"] is False
    assert accepted.json()["activation_verdict"] in {"DISABLED", "BLOCKED"}
    operation = production.app.openapi()["paths"]["/v1/communications/capabilities"]["get"]
    assert operation["operationId"] == "capabilities_v1_communications_capabilities_get"
