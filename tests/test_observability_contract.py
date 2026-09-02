import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import EXTERNAL_DELIVERY_ENABLED, TELEMETRY_EXPORT_ENABLED, app, capabilities
from app.telemetry import (
    audit_event,
    correlation_id_context,
    install_correlation_middleware,
    private_otlp_endpoint,
)


def test_telemetry_is_default_off_and_does_not_enable_delivery():
    assert TELEMETRY_EXPORT_ENABLED is False
    assert EXTERNAL_DELIVERY_ENABLED is False
    assert capabilities()["telemetry_export"] is False
    assert capabilities()["correlation_ids"] is True


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://alloy:4318",
        "http://alloy.monitoring.svc:4318",
        "http://127.0.0.1:4318",
        "https://10.20.30.40:4318/v1/traces",
    ),
)
def test_private_otlp_authorities_are_accepted(endpoint):
    assert private_otlp_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://telemetry.example.com:4318",
        "https://user:secret@alloy:4318",
        "file:///tmp/traces",
        "alloy:4318",
        "https://alloy:4318?token=secret",
    ),
)
def test_external_or_credential_bearing_otlp_authorities_are_rejected(endpoint):
    with pytest.raises(RuntimeError):
        private_otlp_endpoint(endpoint)


def test_correlation_id_is_preserved_or_generated_and_invalid_values_fail_closed():
    test_app = FastAPI()
    install_correlation_middleware(test_app)

    @test_app.get("/")
    def root():
        return {"correlation_id": correlation_id_context.get()}

    client = TestClient(test_app)
    supplied = "delivery:018f4f7a-1234"
    response = client.get("/", headers={"X-Correlation-ID": supplied})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == supplied
    assert response.json() == {"correlation_id": supplied}

    generated = client.get("/")
    assert generated.status_code == 200
    assert generated.headers["X-Correlation-ID"] == generated.json()["correlation_id"]

    rejected = client.get("/", headers={"X-Correlation-ID": "secret value invalid"})
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "invalid_correlation_id"}


def test_audit_log_excludes_recipient_template_and_credentials(caplog):
    token = correlation_id_context.set("corr-communication-123")
    try:
        with caplog.at_level(logging.INFO, logger="codestra.communication.audit"):
            audit_event(
                "message_recorded",
                message_id="message-123",
                status="accepted_delivery_disabled",
                channel="email",
            )
    finally:
        correlation_id_context.reset(token)
    record = json.loads(caplog.records[-1].message)
    assert record == {
        "channel": "email",
        "correlation_id": "corr-communication-123",
        "event": "message_recorded",
        "message_id": "message-123",
        "service": "codestra-communication",
        "status": "accepted_delivery_disabled",
    }
    serialized = caplog.records[-1].message.lower()
    assert "authorization" not in serialized
    assert "recipient" not in serialized
    assert "template" not in serialized


def test_existing_application_exposes_correlation_header_without_delivery():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"]
    assert response.json()["external_delivery_enabled"] is False
