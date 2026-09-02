from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import (
    CancelMessage,
    Channel,
    MessageCreate,
    Purpose,
    cancel_message,
    create_message,
    get_message,
    get_message_events,
    list_messages,
)
from app.models import (
    CommunicationAuditModel,
    ConsentModel,
    MessageEventModel,
    MessageMutationModel,
    SuppressionModel,
)
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
