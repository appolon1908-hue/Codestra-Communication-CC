from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import main
from app.auth import service_bearer
from app.main import Channel, MessageCreate, Purpose, create_message, provider_statuses
from app.telemetry import _optional_file


def test_all_enforced_oauth_scopes_are_declared() -> None:
    scopes = service_bearer.model.flows.clientCredentials.scopes
    required = {
        "communications.preferences.read", "communications.preferences.write",
        "communications.domains.read", "communications.domains.write",
        "communications.senders.read", "communications.senders.write",
        "communications.usage.read",
    }
    assert required.issubset(scopes)


def test_provider_routing_is_channel_specific_when_delivery_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    by_channel = {item.channel: item for item in provider_statuses()}
    assert by_channel[Channel.EMAIL].state == "middleware_routed"
    assert by_channel[Channel.SMS].state == "middleware_routed"
    assert by_channel[Channel.WHATSAPP].state == "unsupported"
    assert by_channel[Channel.PUSH].state == "unsupported"
    assert by_channel[Channel.WHATSAPP].route == "none"


@pytest.mark.asyncio
async def test_unsupported_delivery_channel_is_rejected_before_database_work(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    session = AsyncMock()
    with pytest.raises(HTTPException) as denied:
        await create_message(
            MessageCreate(
                channel=Channel.WHATSAPP, recipient="+1 555 010 1000",
                template_key="unsupported.test", purpose=Purpose.TRANSACTIONAL,
            ),
            "tenant-test", "unsupported-channel-key", session,
        )
    assert denied.value.status_code == 422
    assert denied.value.detail == "channel_delivery_not_supported"
    session.execute.assert_not_awaited()


def test_private_telemetry_file_uses_same_descriptor_and_trusted_parent(monkeypatch) -> None:
    directory = Path.cwd() / f".telemetry-test-{uuid.uuid4()}"
    directory.mkdir(mode=0o700)
    path = directory / "client.key"
    path.write_bytes(b"synthetic-private-key")
    path.chmod(0o600)
    try:
        monkeypatch.setenv("TEST_TELEMETRY_KEY", str(path))
        assert _optional_file("TEST_TELEMETRY_KEY", private=True) == str(path)
    finally:
        shutil.rmtree(directory)


def test_world_writable_telemetry_parent_is_rejected(monkeypatch, tmp_path) -> None:
    path = tmp_path / "client.key"
    path.write_bytes(b"synthetic-private-key")
    path.chmod(0o600)
    monkeypatch.setenv("TEST_TELEMETRY_KEY", str(path))
    with pytest.raises(ValueError, match="telemetry_file_invalid"):
        _optional_file("TEST_TELEMETRY_KEY", private=True)
