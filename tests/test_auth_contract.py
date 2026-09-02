from __future__ import annotations

import httpx
import pytest

import app.auth as auth
from app.main import app


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://identity.example/realms/codestra")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "communication-service")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENT_IDS", "middleware-service")


def _claims(*, tenant: str = "tenant-a", scopes: str = "communications.send") -> dict[str, object]:
    return {
        "sub": "synthetic-service",
        "tenant_id": tenant,
        "azp": "middleware-service",
        "scope": scopes,
        "iss": "https://identity.example/realms/codestra",
        "aud": "communication-service",
        "iat": 1,
        "exp": 4_000_000_000,
    }


def _accept_token(monkeypatch: pytest.MonkeyPatch, claims: dict[str, object]) -> None:
    class Key:
        key = "synthetic-public-key"

    class Client:
        def get_signing_key_from_jwt(self, _token: str) -> Key:
            return Key()

    monkeypatch.setattr(auth, "_jwk_client", lambda _url: Client())
    monkeypatch.setattr(auth.jwt, "decode", lambda *_args, **_kwargs: claims)


@pytest.mark.asyncio
async def test_message_routes_require_bearer_before_business_gate():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/communications/messages",
            headers={"X-Tenant-ID": "tenant-a", "Idempotency-Key": "message-key-1"},
            json={"channel": "email", "recipient": "nobody@example.invalid", "template_key": "test"},
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_tenant_claim_mismatch_is_denied(monkeypatch: pytest.MonkeyPatch):
    _accept_token(monkeypatch, _claims(tenant="tenant-b"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/communications/messages",
            headers={
                "Authorization": "Bearer synthetic",
                "X-Tenant-ID": "tenant-a",
                "Idempotency-Key": "message-key-1",
            },
            json={"channel": "email", "recipient": "nobody@example.invalid", "template_key": "test"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant_mismatch"


@pytest.mark.asyncio
async def test_missing_scope_is_denied(monkeypatch: pytest.MonkeyPatch):
    _accept_token(monkeypatch, _claims(scopes="communications.read"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/communications/messages",
            headers={
                "Authorization": "Bearer synthetic",
                "X-Tenant-ID": "tenant-a",
                "Idempotency-Key": "message-key-1",
            },
            json={"channel": "email", "recipient": "nobody@example.invalid", "template_key": "test"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "required_scope_missing"


@pytest.mark.asyncio
async def test_authorized_mutation_reaches_fail_closed_business_gate(monkeypatch: pytest.MonkeyPatch):
    _accept_token(monkeypatch, _claims())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/communications/messages",
            headers={
                "Authorization": "Bearer synthetic",
                "X-Tenant-ID": "tenant-a",
                "Idempotency-Key": "message-key-1",
            },
            json={"channel": "email", "recipient": "nobody@example.invalid", "template_key": "test"},
        )
    assert response.status_code == 423
    assert response.json()["detail"] == "business_writes_disabled"


def test_openapi_declares_scoped_bearer_security():
    operation = app.openapi()["paths"]["/v1/communications/messages"]["post"]
    assert operation["security"]
    assert "HTTPBearer" in operation["security"][0]
