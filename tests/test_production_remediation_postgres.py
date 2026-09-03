from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.data_protection import blind_index
from app.delivery_worker import run_once as run_delivery_once
from app.main import (
    Channel, MessageCreate, PreferenceWrite, Purpose, SuppressionCreate,
    create_message, create_suppression, upsert_preference,
)
from app.models import (
    CommunicationAuditModel, CommunicationOperationModel, DeliveryOutboxModel,
    MessageEventModel, MessageModel,
)

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_suppression_activation_serializes_message_admission(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-policy-{uuid.uuid4()}"
    recipient = f"policy-{uuid.uuid4()}@example.invalid"
    entered = asyncio.Event()
    release = asyncio.Event()
    original = main._record_domain_mutation

    async def paused(*args, **kwargs):
        if kwargs.get("kind") == "suppression.create":
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(main, "_record_domain_mutation", paused)

    async def activate():
        async with sessions() as session:
            return await create_suppression(
                SuppressionCreate(
                    recipient=recipient, channel=Channel.EMAIL, reason="policy-test"
                ),
                tenant, f"suppression-{uuid.uuid4()}", session,
            )

    async def admit():
        async with sessions() as session:
            return await create_message(
                MessageCreate(
                    channel=Channel.EMAIL, recipient=recipient,
                    template_key="policy.test", purpose=Purpose.TRANSACTIONAL,
                ),
                tenant, f"message-{uuid.uuid4()}", session,
            )

    suppression_task = asyncio.create_task(activate())
    await asyncio.wait_for(entered.wait(), timeout=2)
    message_task = asyncio.create_task(admit())
    await asyncio.sleep(0.1)
    assert not message_task.done()
    release.set()
    suppression = await asyncio.wait_for(suppression_task, timeout=3)
    assert suppression.active is True
    with pytest.raises(HTTPException) as blocked:
        await asyncio.wait_for(message_task, timeout=3)
    assert blocked.value.detail == "recipient_suppressed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_preference_replay_rejects_superseded_result(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-replay-{uuid.uuid4()}"
    subject = f"replay-{uuid.uuid4()}@example.invalid"
    original_key = f"preference-{uuid.uuid4()}"
    original_body = PreferenceWrite(
        subject=subject, channel=Channel.EMAIL, consent="granted", source="test"
    )
    async with sessions() as session:
        created = await upsert_preference(original_body, tenant, original_key, session)
    async with sessions() as session:
        updated = await upsert_preference(
            PreferenceWrite(
                subject=subject, channel=Channel.EMAIL, consent="denied",
                source="test", expected_version=created.resource_version,
            ),
            tenant, f"preference-{uuid.uuid4()}", session,
        )
        assert updated.resource_version == created.resource_version + 1
    async with sessions() as session:
        with pytest.raises(HTTPException) as superseded:
            await upsert_preference(original_body, tenant, original_key, session)
        assert superseded.value.detail == "idempotency_result_superseded"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pre_dispatch_data_protection_failure_updates_public_message(monkeypatch) -> None:
    monkeypatch.setattr(main, "BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr(main, "EXTERNAL_DELIVERY_ENABLED", True)
    monkeypatch.setenv("EXTERNAL_DELIVERY_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-payload-{uuid.uuid4()}"
    async with sessions() as session:
        message = await create_message(
            MessageCreate(
                channel=Channel.EMAIL,
                recipient=f"payload-{uuid.uuid4()}@example.invalid",
                template_key="payload.test", purpose=Purpose.TRANSACTIONAL,
            ),
            tenant, f"message-{uuid.uuid4()}", session,
        )
        message_id = message.id
        operation_id = message.operation_id
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(
                DeliveryOutboxModel.operation_id == operation_id
            )
        )
        outbox.payload_json = "v1:invalid-protected-payload"
        await session.commit()

    class NeverCalled:
        async def dispatch(self, _payload):
            raise AssertionError("dispatch must not run after payload decryption failure")

    assert not await run_delivery_once(
        NeverCalled(), lease_seconds=30, max_attempts=3, session_factory=sessions
    )
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        operation = await session.get(CommunicationOperationModel, operation_id)
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(
                DeliveryOutboxModel.operation_id == operation_id
            )
        )
        assert message.status == "delivery_failed"
        assert operation.state == "failed"
        assert outbox.state == "dead_letter"
        assert await session.scalar(select(MessageEventModel.id).where(
            MessageEventModel.message_id == message_id,
            MessageEventModel.event_type == "communication.message.delivery_failed",
        )) is not None
        assert await session.scalar(select(CommunicationAuditModel.id).where(
            CommunicationAuditModel.aggregate_id == message_id,
            CommunicationAuditModel.action == "communication.message.delivery_failed",
        )) is not None
    await engine.dispose()
