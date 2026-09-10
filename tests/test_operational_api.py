import httpx
import pytest
from fastapi import HTTPException

from app.main import BUSINESS_WRITES_ENABLED, _require_business_writes, app, capabilities, health, version


def test_operational_endpoints_are_attributable_and_fail_closed():
    assert {
        "/health", "/health/live", "/ready", "/health/ready", "/version", "/capabilities", "/metrics"
    }.issubset(app.openapi()["paths"])
    assert health()["service"] == "codestra-communication"
    assert version()["service"] == "codestra-communication"
    value = capabilities()
    assert value["business_writes_enabled"] is False
    assert value["external_delivery_enabled"] is False
    assert value["live_email_enabled"] is False
    assert value["live_sms_enabled"] is False
    assert BUSINESS_WRITES_ENABLED is False


def test_version_does_not_invent_runtime_attribution():
    value = version()
    assert value["git_sha"] == "unknown"
    assert value["image_digest"] == "unknown"


def test_business_mutation_gate_fails_closed():
    with pytest.raises(HTTPException) as denied:
        _require_business_writes()
    assert denied.value.status_code == 423


@pytest.mark.asyncio
async def test_operational_headers_and_content_type():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Correlation-ID": "contract-id"})
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "contract-id"
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_w3c_trace_context_is_accepted_without_reflecting_arbitrary_headers():
    trace_id = "0123456789abcdef0123456789abcdef"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/health",
            headers={"traceparent": f"00-{trace_id}-0123456789abcdef-01", "tracestate": "vendor=value"},
        )
    assert response.status_code == 200
    assert response.headers["x-trace-id"] == trace_id
    assert "tracestate" not in response.headers


@pytest.mark.asyncio
async def test_mutations_require_a_bounded_correlation_identity_before_auth():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post("/v1/communications/preferences", json={})
        invalid = await client.get("/health", headers={"X-Correlation-ID": "bad correlation value"})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "correlation_id_required"
    assert missing.headers["x-correlation-id"]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "correlation_id_invalid"
    assert invalid.headers["x-correlation-id"] == invalid.json()["correlation_id"]
    assert invalid.headers["cache-control"] == "no-store"
