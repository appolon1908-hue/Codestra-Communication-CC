import httpx
import pytest

from app.main import app, capabilities, health, version


def test_operational_endpoints_are_attributable_and_fail_closed():
    assert {"/health", "/ready", "/version", "/capabilities"}.issubset(app.openapi()["paths"])
    assert health()["service"] == "codestra-communication"
    assert version()["service"] == "codestra-communication"
    value = capabilities()
    assert value["business_writes_enabled"] is False
    assert value["external_delivery_enabled"] is False
    assert value["live_email_enabled"] is False
    assert value["live_sms_enabled"] is False


def test_version_does_not_invent_runtime_attribution():
    value = version()
    assert value["git_sha"] == "unknown"
    assert value["image_digest"] == "unknown"


@pytest.mark.asyncio
async def test_operational_headers_and_content_type():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Correlation-ID": "contract-id"})
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "contract-id"
    assert response.headers["content-type"].startswith("application/json")
