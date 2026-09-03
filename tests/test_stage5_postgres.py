from __future__ import annotations

import os
import asyncio
import uuid
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import (
    CancelMessage,
    Channel,
    MessageCreate,
    Purpose,
    ReconcileOperation,
    PreferenceWrite,
    DomainWrite,
    SenderIdentityWrite,
    ConsentChange,
    SuppressionCreate,
    TemplateCreate,
    TemplateUpdate,
    TemplateRenderRequest,
    cancel_message,
    archive_template,
    create_message,
    get_message,
    get_message_events,
    list_messages,
    list_operations,
    list_preferences,
    get_operation,
    create_suppression,
    create_template,
    delete_suppression,
    get_template,
    grant_consent,
    list_consents,
    list_suppressions,
    list_templates,
    render_template,
    revoke_consent,
    reconcile_operation,
    upsert_preference,
    create_domain,
    create_sender_identity,
    get_domain,
    get_sender_identity,
    list_domains,
    list_sender_identities,
    update_sender_identity,
    verify_domain,
    update_template,
    app,
    metrics,
    communication_usage,
)
from app.models import (
    CommunicationAuditModel,
    CommunicationEventOutboxModel,
    CommunicationOperationModel,
    ConsentModel,
    MessageEventModel,
    MessageMutationModel,
    MessageModel,
    DeliveryOutboxModel,
    ProviderInboxModel,
    PreferenceModel,
    SenderIdentityModel,
    SendingDomainModel,
    SuppressionModel,
    TemplateModel,
)
from app.delivery_worker import run_once as run_delivery_once
from app.data_protection import blind_index, protect, reveal
from app.event_worker import acknowledge, claim, reject
from app.middleware_client import MiddlewareDeliveryError, MiddlewareResult
from sqlalchemy import func, select

pytestmark = pytest.mark.postgres


def protected_fields(value: str, tenant_id: str, purpose: str, prefix: str) -> dict[str, str | None]:
    return {
        prefix: None,
        f"{prefix}_ciphertext": protect(value, tenant_id=tenant_id, purpose=purpose),
        f"{prefix}_hash": blind_index(value, tenant_id=tenant_id, purpose=purpose),
    }


def protected_consent(value: str, tenant_id: str) -> dict[str, str | None]:
    return {
        "subject_key": None,
        "subject_ciphertext": protect(value, tenant_id=tenant_id, purpose="consent-subject"),
        "subject_hash": blind_index(value, tenant_id=tenant_id, purpose="consent-subject"),
    }


@pytest.mark.asyncio
async def test_domains_and_sender_identities_are_tenant_bound_and_never_self_verify(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-sender-{uuid.uuid4()}"
    other = f"tenant-other-{uuid.uuid4()}"
    domain_key = f"domain-{uuid.uuid4()}"
    async with sessions() as session:
        domain = await create_domain(DomainWrite(domain="Example.Invalid"), tenant, domain_key, session)
        domain_id = domain.domain_id
        assert domain.domain == "example.invalid"
        assert domain.status == "dns_required"
        assert set(domain.checks.values()) <= {"pending", "not_configured"}
    async with sessions() as session:
        replay = await create_domain(DomainWrite(domain="example.invalid"), tenant, domain_key, session)
        assert replay.domain_id == domain_id
        with pytest.raises(HTTPException) as hidden:
            await get_domain(domain_id, other, session)
        assert hidden.value.status_code == 404
    async def verified_dns(_domain, _metadata):
        return {"spf": "valid", "dkim": "valid", "dmarc": "valid", "bimi": "not_configured",
                "reverse_dns": "not_configured", "tls": "not_configured"}
    monkeypatch.setattr("app.main.verify_domain_dns", verified_dns)
    verify_key = f"domain-verify-{uuid.uuid4()}"
    async with sessions() as session:
        verified = await verify_domain(domain_id, tenant, verify_key, session)
        assert verified.status == "verified"
    async with sessions() as session:
        replay = await verify_domain(domain_id, tenant, verify_key, session)
        assert replay.resource_version == verified.resource_version
    async with sessions() as session:
        sender = await create_sender_identity(
            SenderIdentityWrite(channel=Channel.EMAIL, address="Sender@Example.Invalid", domain_id=domain_id),
            tenant, f"sender-{uuid.uuid4()}", session,
        )
        sender_id = sender.sender_identity_id
        assert sender.address == "sender@example.invalid"
        assert sender.status == "pending"
    async with sessions() as session:
        assert (await get_sender_identity(sender_id, tenant, session)).sender_identity_id == sender_id
        assert len((await list_domains(tenant, None, 50, session)).items) == 1
        assert len((await list_sender_identities(tenant, None, 50, session)).items) == 1
        assert (await list_sender_identities(other, None, 50, session)).items == []
        with pytest.raises(HTTPException) as mismatch:
            await create_sender_identity(
                SenderIdentityWrite(channel=Channel.EMAIL, address="sender@other.invalid", domain_id=domain_id),
                tenant, f"sender-{uuid.uuid4()}", session,
            )
        assert mismatch.value.status_code == 409
    update_key = f"sender-update-{uuid.uuid4()}"
    update = SenderIdentityWrite(
        channel=Channel.EMAIL, address="sender@example.invalid", domain_id=domain_id,
        display_name="Synthetic Sender", expected_version=1,
    )
    async with sessions() as session:
        updated = await update_sender_identity(sender_id, update, tenant, update_key, session)
        assert updated.resource_version == 2
    async with sessions() as session:
        replay = await update_sender_identity(sender_id, update, tenant, update_key, session)
        assert replay.resource_version == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_preferences_are_tenant_scoped_versioned_and_idempotent(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-preference-{uuid.uuid4()}"
    other = f"tenant-other-{uuid.uuid4()}"
    idem = f"preference-{uuid.uuid4()}"
    body = PreferenceWrite(subject="Person@Example.invalid", channel=Channel.EMAIL,
                           consent="granted", source="synthetic")
    async with sessions() as session:
        created = await upsert_preference(body, tenant, idem, session)
        assert created.subject == "person@example.invalid"
        assert created.resource_version == 1
        stored = await session.get(PreferenceModel, created.preference_id)
        assert stored.subject is None
        assert stored.subject_hash
        assert "person@example.invalid" not in stored.subject_ciphertext
    async with sessions() as session:
        replay = await upsert_preference(body, tenant, idem, session)
        assert replay.preference_id == created.preference_id
    async with sessions() as session:
        own = await list_preferences(tenant, "person@example.invalid", None, 50, session)
        foreign = await list_preferences(other, None, None, 50, session)
        assert [item.preference_id for item in own.items] == [created.preference_id]
        assert foreign.items == []
    async with sessions() as session:
        updated = await upsert_preference(
            PreferenceWrite(subject="person@example.invalid", channel=Channel.EMAIL,
                            consent="denied", source="synthetic", expected_version=1),
            tenant, f"preference-{uuid.uuid4()}", session,
        )
        assert updated.consent == "denied"
        assert updated.resource_version == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_message_event_and_publish_intent_are_atomic_sanitized_and_attempt_fenced(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-event-{uuid.uuid4()}"
    recipient = f"private-{uuid.uuid4()}@example.invalid"
    async with sessions() as session:
        session.add(ConsentModel(
            tenant_id=tenant, channel="email", status="granted", source="event-test",
            **protected_consent(recipient, tenant),
        ))
        await session.commit()
    async with sessions() as session:
        created = await create_message(MessageCreate(channel=Channel.EMAIL, recipient=recipient,
            template_key="event.test", purpose=Purpose.MARKETING), tenant, str(uuid.uuid4()), session)
    async with sessions() as session:
        row = await session.scalar(select(CommunicationEventOutboxModel).where(
            CommunicationEventOutboxModel.payload_json.contains(str(created.id))))
        assert row is not None
        assert recipient not in row.payload_json
        assert "event.test" not in row.payload_json
        assert row.state == "pending"
    claimed = await claim(1, 30, session_factory=sessions)
    assert claimed
    event = next(item for item in claimed if str(created.id) in item.payload_json)
    assert await acknowledge(event.id, event.attempts + 1, session_factory=sessions) is False
    assert await reject(event.id, event.attempts, 1, session_factory=sessions) is True
    async with sessions() as session:
        row = await session.get(CommunicationEventOutboxModel, event.id)
        assert row.state == "dead_letter"
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_metrics_are_backed_by_current_database_state():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        response = await metrics(session)
    assert b"codestra_communication_delivery_outbox_records" in response.body
    assert b"codestra_communication_operations" in response.body
    assert b"codestra_communication_provider_inbox_records" in response.body
    await engine.dispose()


@pytest.mark.asyncio
async def test_usage_is_derived_from_tenant_rows_and_time_window(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"tenant-usage-{uuid.uuid4()}"
    other = f"tenant-other-{uuid.uuid4()}"
    async with sessions() as session:
        created = await create_message(MessageCreate(
            channel=Channel.EMAIL, recipient="usage@example.invalid",
            template_key="transactional.test", purpose=Purpose.TRANSACTIONAL,
        ), tenant, f"message-{uuid.uuid4()}", session)
        created.status = "delivered"
        await session.commit()
        await create_message(MessageCreate(
            channel=Channel.EMAIL, recipient="other@example.invalid",
            template_key="transactional.test", purpose=Purpose.TRANSACTIONAL,
        ), other, f"message-{uuid.uuid4()}", session)
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        report = await communication_usage(
            tenant, now - timedelta(hours=1), now + timedelta(hours=1), Channel.EMAIL, session,
        )
        assert report.totals[0].accepted == 1
        assert report.totals[0].delivered == 1
    await engine.dispose()


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
                channel="email",
                status="granted",
                source="stage5-test",
                **protected_consent(recipient, tenant_a),
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
                reason="stage5-test",
                **protected_fields(recipient, tenant_a, "suppression-recipient", "recipient"),
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
        stored = await session.get(MessageModel, message_id)
        outbox = await session.scalar(select(DeliveryOutboxModel).where(
            DeliveryOutboxModel.operation_id == message.operation_id
        ))
        assert stored.recipient is None and stored.recipient_hash
        assert "@example.invalid" not in stored.recipient_ciphertext
        assert "@example.invalid" not in outbox.payload_json
        assert outbox.payload_json.startswith("v1:")

    class Client:
        async def dispatch(self, payload):
            return MiddlewareResult("middleware-delivery-operation", "accepted")

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
        current_version = message.resource_version
        operations = await list_operations(tenant_id, None, 50, session)
        assert len(operations) == 1
        assert (await get_operation(operations[0].id, tenant_id, session)).id == operations[0].id
        with pytest.raises(HTTPException) as hidden:
            await get_operation(operations[0].id, "other-tenant", session)
        assert hidden.value.status_code == 404
    async with sessions() as session:
        pending = await cancel_message(
            message_id,
            CancelMessage(expected_version=current_version, reason="synthetic cancellation"),
            tenant_id,
            f"cancel-{uuid.uuid4()}",
            session,
        )
        assert pending.status == "cancellation_pending"
        cancel_operation = await session.scalar(
            select(CommunicationOperationModel).where(
                CommunicationOperationModel.message_id == message_id,
                CommunicationOperationModel.kind == "cancel",
            )
        )
        cancel_outbox = await session.scalar(
            select(DeliveryOutboxModel).where(
                DeliveryOutboxModel.operation_id == cancel_operation.id
            )
        )
        cancel_payload = json.loads(reveal(
            ciphertext=cancel_outbox.payload_json, legacy_plaintext=None,
            tenant_id=tenant_id, purpose="delivery-payload",
        ))
        assert cancel_payload["delivery_operation_id"] == (
            "middleware-delivery-operation"
        )

    class CancelClient:
        async def dispatch(self, _payload):
            return MiddlewareResult("middleware-cancel-operation", "cancelled")

    assert await run_delivery_once(
        CancelClient(), lease_seconds=30, max_attempts=3, session_factory=sessions
    )
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        assert message.status == "cancelled"
        event_types = set(
            await session.scalars(
                select(MessageEventModel.event_type).where(
                    MessageEventModel.message_id == message_id
                )
            )
        )
        assert "communication.message.cancellation_requested" in event_types
        assert "communication.message.cancelled" in event_types
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_delivery_outcome_stops_redispatch_and_updates_public_state(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setattr("app.main.EXTERNAL_DELIVERY_ENABLED", True)
    monkeypatch.setenv("EXTERNAL_DELIVERY_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-unknown-{uuid.uuid4()}"
    async with sessions() as session:
        message = await create_message(
            MessageCreate(
                channel=Channel.EMAIL,
                recipient=f"unknown-{uuid.uuid4()}@example.invalid",
                template_key="transactional.test",
                purpose=Purpose.TRANSACTIONAL,
            ),
            tenant_id,
            f"message-{uuid.uuid4()}",
            session,
        )
        message_id = message.id

    class Client:
        async def dispatch(self, _payload):
            raise MiddlewareDeliveryError(
                "middleware_outcome_unknown", retryable=True, outcome_unknown=True
            )

    assert await run_delivery_once(Client(), lease_seconds=30, max_attempts=8, session_factory=sessions)
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        operation = await session.scalar(
            select(CommunicationOperationModel).where(
                CommunicationOperationModel.message_id == message_id
            )
        )
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(DeliveryOutboxModel.operation_id == operation.id)
        )
        assert message.status == "reconciliation_required"
        assert operation.state == "reconciliation_required"
        assert outbox.state == "reconciliation_required"
        assert await session.scalar(
            select(func.count()).select_from(CommunicationAuditModel).where(
                CommunicationAuditModel.aggregate_id == message_id,
                CommunicationAuditModel.action == "communication.message.reconciliation_required",
            )
        ) == 1
    assert not await run_delivery_once(Client(), lease_seconds=30, max_attempts=8, session_factory=sessions)
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_is_durable_and_reads_middleware_status(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    monkeypatch.setenv("EXTERNAL_DELIVERY_ENABLED", "true")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-reconcile-{uuid.uuid4()}"
    message_id = uuid.uuid4()
    target_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            MessageModel(
                id=message_id,
                tenant_id=tenant_id,
                channel="email",
                **protected_fields(
                    "reconcile@example.invalid", tenant_id, "message-recipient", "recipient"
                ),
                template_key="transactional.test",
                purpose="transactional",
                idempotency_key=f"message-{uuid.uuid4()}",
                request_fingerprint="0" * 64,
                status="reconciliation_required",
                resource_version=2,
                operation_id=target_id,
            )
        )
        await session.flush()
        session.add(
            CommunicationOperationModel(
                id=target_id,
                tenant_id=tenant_id,
                message_id=message_id,
                kind="deliver",
                state="reconciliation_required",
                idempotency_key=f"delivery-{uuid.uuid4()}",
                correlation_id=f"corr-{uuid.uuid4()}",
                middleware_operation_id=None,
            )
        )
        session.add(
            DeliveryOutboxModel(
                tenant_id=tenant_id,
                operation_id=target_id,
                state="reconciliation_required",
                payload_json=protect(json.dumps({
                    "operation_id": str(target_id), "message_id": str(message_id),
                    "channel": "email", "recipient": "reconcile@example.invalid",
                    "template_key": "transactional.test", "purpose": "transactional",
                    "tenant_id": tenant_id, "correlation_id": f"corr-{uuid.uuid4()}",
                }, sort_keys=True, separators=(",", ":")), tenant_id=tenant_id, purpose="delivery-payload"),
            )
        )
        await session.commit()
    async with sessions() as session:
        operation = await reconcile_operation(
            target_id,
            ReconcileOperation(expected_message_version=2),
            tenant_id,
            "reconcile-idempotency-key",
            session,
        )
        reconciliation_id = operation.id
        assert operation.state == "pending"

    class Client:
        async def dispatch(self, payload):
            assert "action" not in payload
            assert payload["operation_id"] == str(target_id)
            return MiddlewareResult("middleware-operation-reconcile", "completed")

    assert await run_delivery_once(Client(), lease_seconds=30, max_attempts=3, session_factory=sessions)
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        target = await session.get(CommunicationOperationModel, target_id)
        reconciliation = await session.get(CommunicationOperationModel, reconciliation_id)
        assert message.status == "completed"
        assert target.state == "accepted"
        assert reconciliation.state == "accepted"
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
                template_key="transactional.test",
                purpose="transactional", idempotency_key=f"message-{uuid.uuid4()}",
                request_fingerprint="0" * 64, status="middleware_accepted",
                provider_message_id="provider-message-1",
                **protected_fields(
                    "webhook@example.invalid", tenant_id, "message-recipient", "recipient"
                ),
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
    event_id = "provider-event-1"
    signature = hmac.new(
        secret,
        b".".join(
            (
                b"synthetic",
                tenant_id.encode(),
                event_id.encode(),
                timestamp.encode(),
                body,
            )
        ),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-Correlation-ID": f"corr-{uuid.uuid4()}",
        "X-Provider-Timestamp": timestamp,
        "X-Provider-Event-ID": event_id,
        "X-Provider-Signature": f"sha256={signature}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/webhooks/communications/synthetic/results", headers=headers, content=body)
        replay = await client.post("/v1/webhooks/communications/synthetic/results", headers=headers, content=body)
        stale_body = json.dumps(
            {
                "message_id": str(message_id), "event_type": "sent",
                "provider_message_id": "provider-message-1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        stale_headers = dict(headers)
        stale_headers["X-Provider-Event-ID"] = "provider-event-2"
        stale_signature = hmac.new(
            secret,
            b".".join(
                (
                    b"synthetic", tenant_id.encode(), b"provider-event-2",
                    timestamp.encode(), stale_body,
                )
            ),
            hashlib.sha256,
        ).hexdigest()
        stale_headers["X-Provider-Signature"] = f"sha256={stale_signature}"
        stale = await client.post(
            "/v1/webhooks/communications/synthetic/results",
            headers=stale_headers,
            content=stale_body,
        )
    assert first.status_code == 202 and first.json()["status"] == "processed"
    assert replay.status_code == 202 and replay.json()["status"] == "already_processed"
    assert stale.status_code == 202 and stale.json()["status"] == "processed"
    async with sessions() as session:
        message = await session.get(MessageModel, message_id)
        assert message.status == "delivered"
        assert await session.scalar(select(func.count()).select_from(ProviderInboxModel)) == 2
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
async def test_templates_consents_and_suppressions_are_versioned_and_replay_safe(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
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
        rendered = await render_template(
            template_id, TemplateRenderRequest(variables={"name": "Synthetic"}),
            tenant_id, session,
        )
        assert rendered.body == "Hello Synthetic"
        assert rendered.missing_variables == []
        missing = await render_template(
            template_id, TemplateRenderRequest(variables={}), tenant_id, session,
        )
        assert missing.body == "Hello {{name}}"
        assert missing.missing_variables == ["name"]
    async with sessions() as session:
        updated = await update_template(
            template_id,
            TemplateUpdate(
                expected_version=template_version,
                subject_template=None,
                body_template="Hello",
            ),
            tenant_id, "template-update-key", session,
        )
        assert updated.resource_version == template_version + 1
        assert updated.subject_template is None
        assert [row.id for row in await list_templates(tenant_id, session)] == [template_id]
        with pytest.raises(HTTPException) as hidden:
            await get_template(template_id, other_tenant, session)
        assert hidden.value.status_code == 404
    async with sessions() as session:
        response = await archive_template(
            template_id, tenant_id, "template-archive-key", session
        )
        assert response.status_code == 204
        stored = await session.get(TemplateModel, template_id)
        assert stored.active is False
    async with sessions() as session:
        replay = await archive_template(
            template_id, tenant_id, "template-archive-key", session
        )
        assert replay.status_code == 204
    async with sessions() as session:
        changed = await update_template(
            template_id,
            TemplateUpdate(expected_version=template_version + 2, body_template="Later"),
            tenant_id, "template-later-update-key", session,
        )
        assert changed.resource_version == template_version + 3
    async with sessions() as session:
        with pytest.raises(HTTPException) as superseded:
            await update_template(
                template_id,
                TemplateUpdate(
                    expected_version=template_version,
                    subject_template=None,
                    body_template="Hello",
                ),
                tenant_id, "template-update-key", session,
            )
        assert superseded.value.status_code == 409
        assert superseded.value.detail == "idempotency_result_superseded"

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


@pytest.mark.asyncio
async def test_concurrent_policy_inserts_return_the_committed_winner(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-policy-race-{uuid.uuid4()}"
    recipient = f"race-{uuid.uuid4()}@example.invalid"
    consent_body = ConsentChange(
        subject_key=recipient, channel=Channel.EMAIL, source="synthetic-race"
    )

    async def consent_attempt():
        async with sessions() as session:
            return await grant_consent(
                consent_body, tenant_id, "concurrent-consent-key", session
            )

    consents = await asyncio.gather(consent_attempt(), consent_attempt())
    assert consents[0].id == consents[1].id

    suppression_body = SuppressionCreate(
        recipient=recipient, channel=Channel.EMAIL, reason="synthetic-race"
    )

    async def suppression_attempt():
        async with sessions() as session:
            return await create_suppression(
                suppression_body, tenant_id, "concurrent-suppression-key", session
            )

    suppressions = await asyncio.gather(suppression_attempt(), suppression_attempt())
    assert suppressions[0].id == suppressions[1].id
    await engine.dispose()


@pytest.mark.asyncio
async def test_message_acceptance_rechecks_concurrent_consent_revocation(monkeypatch):
    monkeypatch.setattr("app.main.BUSINESS_WRITES_ENABLED", True)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-consent-race-{uuid.uuid4()}"
    recipient = f"consent-race-{uuid.uuid4()}@example.invalid"
    async with sessions() as seed:
        seed.add(
            ConsentModel(
                tenant_id=tenant_id,
                channel="email",
                status="granted",
                source="synthetic-race",
                **protected_consent(recipient, tenant_id),
            )
        )
        await seed.commit()

    revoker = sessions()
    consent = await revoker.scalar(
        select(ConsentModel)
        .where(
            ConsentModel.tenant_id == tenant_id,
            ConsentModel.subject_hash == blind_index(
                recipient, tenant_id=tenant_id, purpose="consent-subject"
            ),
            ConsentModel.channel == "email",
        )
        .with_for_update()
    )
    consent.status = "revoked"

    async def accept_message():
        async with sessions() as sender:
            return await create_message(
                MessageCreate(
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    template_key="marketing.test",
                    purpose=Purpose.MARKETING,
                ),
                tenant_id,
                f"message-{uuid.uuid4()}",
                sender,
            )

    pending = asyncio.create_task(accept_message())
    await asyncio.sleep(0.05)
    assert not pending.done()
    await revoker.commit()
    await revoker.close()
    with pytest.raises(HTTPException) as denied:
        await pending
    assert denied.value.detail == "marketing_consent_missing"
    await engine.dispose()
