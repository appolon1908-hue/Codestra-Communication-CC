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
)
from app.models import (
    CommunicationAuditModel,
    ConsentModel,
    MessageEventModel,
    MessageMutationModel,
    SuppressionModel,
    TemplateModel,
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
