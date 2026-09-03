from __future__ import annotations

import httpx
import pytest

from app.middleware_client import MiddlewareCommunicationClient, MiddlewareDeliveryError


@pytest.mark.asyncio
async def test_middleware_circuit_opens_and_recovers_without_dispatching(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-token", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("MIDDLEWARE_TOKEN_FILE", str(token))
    monkeypatch.setenv("MIDDLEWARE_CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("MIDDLEWARE_CIRCUIT_RECOVERY_SECONDS", "10")
    clock = [100.0]
    monkeypatch.setattr("app.middleware_client.time.monotonic", lambda: clock[0])

    class Transport:
        calls = 0
        last_headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            self.calls += 1
            self.last_headers = _kwargs["headers"]
            if self.calls <= 2:
                raise httpx.ConnectError("synthetic unavailable")
            return httpx.Response(202, json={"operation_id": "middleware-op", "state": "accepted"})

    transport = Transport()
    monkeypatch.setattr("app.middleware_client.httpx.AsyncClient", lambda **_kwargs: transport)
    client = MiddlewareCommunicationClient()
    payload = {
        "operation_id": "local-op", "tenant_id": "tenant-test",
        "correlation_id": "correlation-test", "channel": "email",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    }
    for _ in range(2):
        with pytest.raises(MiddlewareDeliveryError, match="middleware_outcome_unknown"):
            await client.dispatch(payload)
    with pytest.raises(MiddlewareDeliveryError, match="middleware_circuit_open") as opened:
        await client.dispatch(payload)
    assert opened.value.outcome_unknown is False
    assert transport.calls == 2
    clock[0] += 11
    result = await client.dispatch(payload)
    assert result.operation_id == "middleware-op"
    assert transport.calls == 3
    assert transport.last_headers["traceparent"].split("-")[1] == "0123456789abcdef0123456789abcdef"


def test_enabled_telemetry_rejects_unsafe_collector_endpoints(monkeypatch):
    from app.telemetry import _endpoint

    for value in ("", "http://collector.internal/v1/traces", "https://user:pass@collector/v1/traces", "https://collector/v1/traces?token=secret"):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", value)
        with pytest.raises(ValueError, match="telemetry_endpoint_invalid"):
            _endpoint()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.internal/v1/traces")
    assert _endpoint() == "https://collector.internal/v1/traces"


def test_middleware_token_rejects_relative_symlink_and_broad_permissions(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-token", encoding="utf-8")
    token.chmod(0o644)
    monkeypatch.setenv("MIDDLEWARE_TOKEN_FILE", str(token))
    client = MiddlewareCommunicationClient()
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()
    token.chmod(0o600)
    link = tmp_path / "token-link"
    link.symlink_to(token)
    monkeypatch.setenv("MIDDLEWARE_TOKEN_FILE", str(link))
    client = MiddlewareCommunicationClient()
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()
    monkeypatch.setenv("MIDDLEWARE_TOKEN_FILE", "relative-token")
    client = MiddlewareCommunicationClient()
    with pytest.raises(MiddlewareDeliveryError, match="middleware_token_file_invalid"):
        client._token()
