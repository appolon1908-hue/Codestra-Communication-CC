from __future__ import annotations

import os
import uuid
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import (
    CancelMessage,
    Channel,
    MessageCreate,
    Purpose,
    ConsentChange,
    SuppressionCreate,
    TemplateCreate,
    TemplateUpdate,
    cancel_message,
    create_message,
    get_message,
    get_message_events,
    list_messages,
    create_suppression,
    create_template,
    delete_suppression,
    get_template,
    grant_consent,
    list_consents,
    list_suppressions,
    list_templates,
    revoke_consent,
    update_template,
    app,
)
from app.models import (
    CommunicationAuditModel,
    CommunicationOperationModel,
    ConsentModel,
    MessageEventModel,
    MessageMutationModel,
    MessageModel,
    DeliveryOutboxModel,
    ProviderInboxModel,
    SuppressionModel,
    TemplateModel,
)
from app.delivery_worker import run_once as run_delivery_once
from app.middleware_client import MiddlewareResult
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_consent_suppression_idempotency_and_tenant_isolation(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a = f"tenant-a-{uuid.uuid4()}"
    tenant_b = f"tenant-b-{uuid.uuid4()}"
    recipient = f"stage5-{uuid.uuid4()}@example.invalid"
    key = f"message-{uuid.uuid4()}"
    body = MessageCreate(
        channel=Channel.EMAIL,
        recipient=recipient,
        template_key="stage5.test",
        purpose=Purpose.MARKETING,
    )

    async with sessions() as session:
        session.add(
            ConsentModel(
                tenant_id=tenant_a,
                subject_key=recipient,
                channel="email",
                status="granted",
                source="stage5-test",
            )
        )
        await session.commit()
    async with sessions() as session:
        first = await create_message(body, tenant_a, key, session)
        message_id = first.id
    async with sessions() as session:
        duplicate = await create_message(body, tenant_a, key, session)
        assert duplicate.id == message_id
    async with sessions() as session:
        with pytest.raises(HTTPException) as conflict:
            await create_message(
                MessageCreate(
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    template_key="different.template",
                    purpose=Purpose.MARKETING,
                ),
                tenant_a,
                key,
                session,
            )
        assert conflict.value.status_code == 409
    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await get_message(message_id, tenant_b, session)
        assert denied.value.status_code == 404
    async with sessions() as session:
        session.add(
            SuppressionModel(
                tenant_id=tenant_a,
                channel="email",
                recipient=recipient,
                reason="stage5-test",
            )
        )
        await session.commit()
    async with sessions() as session:
        with pytest.raises(HTTPException) as suppressed:
            await create_message(body, tenant_a, f"suppressed-{uuid.uuid4()}", session)
        assert suppressed.value.detail == "recipient_suppressed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_enabled_delivery_creates_one_middleware_outbox_and_worker_accepts(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr("app.main.EXTERNAL_DELIVERY_ENABLED", True)
    monkeypatch.setenv("EXTERNAL_DELIVERY_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-delivery-{uuid.uuid4()}"
    async with sessions() as session:
        message = await create_message(
            MessageCreate(
                channel=Channel.EMAIL, recipient=f"delivery-{uuid.uuid4()}@example.invalid",
                template_key="transactional.test", purpose=Purpose.TRANSACTIONAL,
            ),
            tenant_id, f"message-{uuid.uuid4()}", session,
        )
        message_id = message.id
        assert message.status == "queued" and message.operation_id is not None

    class Client:
        async def dispatch(self, payload):
            return MiddlewareResult(str(payload["operation_id"]), "accepted")

    assert await run_delivery_once(Client(), lease_seconds=30, max_attempts=3, session_factory=sessions)
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        assert message.status == "middleware_accepted"
        assert await session.scalar(
            select(func.count()).select_from(DeliveryOutboxModel).where(
                DeliveryOutboxModel.state == "completed"
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(CommunicationOperationModel).where(
                CommunicationOperationModel.state == "accepted"
            )
        ) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_webhook_is_signed_replay_safe_and_tenant_bound(monkeypatch, tmp_path):
    secret = b"synthetic-webhook-secret-material-32bytes"
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "synthetic.secret").write_bytes(secret)
    monkeypatch.setenv("PROVIDER_WEBHOOK_SECRET_DIR", str(secret_dir))
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-webhook-{uuid.uuid4()}"
    message_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            MessageModel(
                id=message_id, tenant_id=tenant_id, channel="email",
                recipient="webhook@example.invalid", template_key="transactional.test",
                purpose="transactional", idempotency_key=f"message-{uuid.uuid4()}",
                request_fingerprint="0" * 64, status="middleware_accepted",
                provider_message_id="provider-message-1",
            )
        )
        await session.commit()
    body = json.dumps(
        {
            "message_id": str(message_id), "event_type": "delivered",
            "provider_message_id": "provider-message-1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-Correlation-ID": f"corr-{uuid.uuid4()}",
        "X-Provider-Timestamp": timestamp,
        "X-Provider-Event-ID": "provider-event-1",
        "X-Provider-Signature": f"sha256={signature}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/webhooks/communications/synthetic/results", headers=headers, content=body)
        replay = await client.post("/v1/webhooks/communications/synthetic/results", headers=headers, content=body)
    assert first.status_code == 202 and first.json()["status"] == "processed"
    assert replay.status_code == 202 and replay.json()["status"] == "already_processed"
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        assert message.status == "delivered"
        assert await session.scalar(select(func.count()).select_from(ProviderInboxModel)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_message_history_cancel_and_mutation_ledger_are_durable(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-lifecycle-{uuid.uuid4()}"
    async with sessions() as session:
        created = await create_message(
            MessageCreate(
                channel=Channel.EMAIL,
                recipient=f"lifecycle-{uuid.uuid4()}@example.invalid",
                template_key="transactional.test",
                purpose=Purpose.TRANSACTIONAL,
            ),
            tenant_id,
            f"create-{uuid.uuid4()}",
            session,
        )
        message_id = created.id
        version = created.resource_version
    async with sessions() as session:
        rows = await list_messages(tenant_id, None, 50, session)
        assert [row.id for row in rows] == [message_id]
        events = await get_message_events(message_id, tenant_id, None, 50, session)
        assert [event.event_type for event in events] == ["communication.message.accepted"]
    cancellation = CancelMessage(expected_version=version, reason="synthetic cancellation")
    key = f"cancel-{uuid.uuid4()}"
    async with sessions() as session:
        cancelled = await cancel_message(message_id, cancellation, tenant_id, key, session)
        assert cancelled.status == "cancelled"
    async with sessions() as session:
        replay = await cancel_message(message_id, cancellation, tenant_id, key, session)
        assert replay.status == "cancelled"
        assert await session.scalar(
            select(func.count()).select_from(MessageMutationModel).where(
                MessageMutationModel.tenant_id == tenant_id,
                MessageMutationModel.message_id == message_id,
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(MessageEventModel).where(
                MessageEventModel.tenant_id == tenant_id,
                MessageEventModel.message_id == message_id,
            )
        ) == 2
        assert await session.scalar(
            select(func.count()).select_from(CommunicationAuditModel).where(
                CommunicationAuditModel.tenant_id == tenant_id,
                CommunicationAuditModel.aggregate_id == message_id,
            )
        ) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_templates_consents_and_suppressions_are_versioned_and_replay_safe():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-policy-{uuid.uuid4()}"
    other_tenant = f"tenant-policy-other-{uuid.uuid4()}"
    async with sessions() as session:
        template = await create_template(
            TemplateCreate(
                key="welcome.transactional", channel=Channel.EMAIL, locale="en",
                subject_template="Welcome", body_template="Hello {{name}}",
            ),
            tenant_id, f"template-{uuid.uuid4()}", session,
        )
        template_id = template.id
        template_version = template.resource_version
    async with sessions() as session:
        updated = await update_template(
            template_id, TemplateUpdate(expected_version=template_version, body_template="Hello"),
            tenant_id, "template-update-key", session,
        )
        assert updated.resource_version == template_version + 1
        assert [row.id for row in await list_templates(tenant_id, session)] == [template_id]
        with pytest.raises(HTTPException) as hidden:
            await get_template(template_id, other_tenant, session)
        assert hidden.value.status_code == 404

    recipient = f"policy-{uuid.uuid4()}@example.invalid"
    grant = ConsentChange(
        subject_key=recipient, channel=Channel.EMAIL, source="synthetic-test"
    )
    async with sessions() as session:
        consent = await grant_consent(grant, tenant_id, "consent-grant-key", session)
        consent_version = consent.resource_version
    async with sessions() as session:
        replay = await grant_consent(grant, tenant_id, "consent-grant-key", session)
        assert replay.resource_version == consent_version
        revoked = await revoke_consent(
            grant.model_copy(update={"expected_version": consent_version}),
            tenant_id, "consent-revoke-key", session,
        )
        assert revoked.status == "revoked"
        assert len(await list_consents(tenant_id, None, session)) == 1

    async with sessions() as session:
        suppression = await create_suppression(
            SuppressionCreate(recipient=recipient, channel=Channel.EMAIL, reason="synthetic"),
            tenant_id, "suppression-create-key", session,
        )
        suppression_id = suppression.id
        suppression_version = suppression.resource_version
    async with sessions() as session:
        deleted = await delete_suppression(
            suppression_id, tenant_id, "suppression-delete-key", suppression_version, session,
        )
        assert deleted.active is False
        assert await list_suppressions(tenant_id, session) == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TemplateModel)) == 1
        assert await session.scalar(select(func.count()).select_from(CommunicationAuditModel)) >= 5
    await engine.dispose()
