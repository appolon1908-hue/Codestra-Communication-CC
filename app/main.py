from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import asyncio
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .auth import require_scope
from .models import (
    CommunicationAuditModel,
    CommunicationOperationModel,
    ConsentModel,
    DomainMutationModel,
    DeliveryOutboxModel,
    MessageEventModel,
    MessageModel,
    MessageMutationModel,
    ProviderInboxModel,
    SuppressionModel,
    TemplateModel,
)
from .metrics import DELIVERY_OUTBOX, HTTP_DURATION, HTTP_REQUESTS, OPERATIONS, PROVIDER_INBOX, render

app = FastAPI(title="Codestra Communication API", version="0.3.0")
router = APIRouter(prefix="/v1/communications")
EXTERNAL_DELIVERY_ENABLED = os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").lower() == "true"
BUSINESS_WRITES_ENABLED = os.getenv("BUSINESS_WRITES_ENABLED", "false").lower() == "true"
SERVICE = "codestra-communication"


@app.middleware("http")
async def operational_headers(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
    request.state.correlation_id = correlation_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(status_code=500, content={"detail": "internal_error", "correlation_id": correlation_id})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Correlation-ID"] = correlation_id
    HTTP_REQUESTS.labels(
        method=request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER",
        status_class=f"{response.status_code // 100}xx",
    ).inc()
    HTTP_DURATION.labels(
        method=request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER"
    ).observe(time.perf_counter() - started)
    return response

TenantHeader = Annotated[str, Header(alias="X-Tenant-ID", min_length=1, max_length=128)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"


class Purpose(StrEnum):
    MARKETING = "marketing"
    TRANSACTIONAL = "transactional"
    SERVICE = "service"


class MessageCreate(BaseModel):
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    channel: Channel
    recipient: str = Field(min_length=1, max_length=512)
    template_key: str = Field(min_length=1, max_length=160)
    purpose: Purpose = Purpose.MARKETING
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class Message(BaseModel):
    id: UUID
    tenant_id: str
    status: str
    channel: Channel
    template_key: str
    purpose: Purpose
    resource_version: int
    operation_id: UUID | None
    created_at: datetime
    model_config = {"from_attributes": True}


class MessageEvent(BaseModel):
    id: int
    event_type: str
    previous_status: str | None
    new_status: str
    actor_id: str
    correlation_id: str
    safe_detail: str | None
    occurred_at: datetime
    model_config = {"from_attributes": True}


class CancelMessage(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=240)


class TemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    channel: Channel
    locale: str = Field(default="en", min_length=2, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    subject_template: str | None = Field(default=None, max_length=2000)
    body_template: str = Field(min_length=1, max_length=100_000)


class TemplateUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    subject_template: str | None = Field(default=None, max_length=2000)
    body_template: str | None = Field(default=None, min_length=1, max_length=100_000)
    active: bool | None = None


class Template(BaseModel):
    id: UUID
    key: str
    channel: Channel
    locale: str
    subject_template: str | None
    body_template: str
    active: bool
    resource_version: int
    model_config = {"from_attributes": True}


class ConsentChange(BaseModel):
    subject_key: str = Field(min_length=1, max_length=256)
    channel: Channel
    source: str = Field(min_length=1, max_length=128)
    evidence: str | None = Field(default=None, max_length=4000)
    expected_version: int | None = Field(default=None, ge=1)


class Consent(BaseModel):
    subject_key: str
    channel: Channel
    status: str
    source: str
    resource_version: int
    model_config = {"from_attributes": True}


class SuppressionCreate(BaseModel):
    recipient: str = Field(min_length=1, max_length=512)
    channel: Channel
    reason: str = Field(min_length=1, max_length=128)


class Suppression(BaseModel):
    id: UUID
    channel: Channel
    recipient: str
    reason: str
    active: bool
    resource_version: int
    model_config = {"from_attributes": True}


class ProviderResult(BaseModel):
    message_id: UUID
    event_type: str = Field(pattern=r"^(sent|delivered|failed|bounced|complained|cancelled)$")
    provider_message_id: str | None = Field(default=None, min_length=1, max_length=160)


class ProviderStatus(BaseModel):
    channel: Channel
    route: str
    state: str
    health: str
    reputation: str
    direct_provider_credentials: bool = False


class Operation(BaseModel):
    id: UUID
    message_id: UUID
    kind: str
    state: str
    attempts: int
    middleware_operation_id: str | None
    error_code: str | None
    correlation_id: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ReconcileOperation(BaseModel):
    expected_message_version: int = Field(ge=1)


def _require_business_writes() -> None:
    if not BUSINESS_WRITES_ENABLED:
        raise HTTPException(status_code=423, detail="business_writes_disabled")


def _actor(request: Request | None) -> str:
    principal = getattr(getattr(request, "state", None), "principal", None)
    return getattr(principal, "subject", "codestra-communication-internal")[:160]


def _correlation(request: Request | None) -> str:
    return getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))[:128]


async def _message_event(
    session: AsyncSession,
    row: MessageModel,
    *,
    event_type: str,
    previous_status: str | None,
    request: Request | None,
    safe_detail: str | None = None,
) -> None:
    session.add(
        MessageEventModel(
            tenant_id=row.tenant_id,
            message_id=row.id,
            event_type=event_type,
            previous_status=previous_status,
            new_status=row.status,
            actor_id=_actor(request),
            correlation_id=_correlation(request),
            safe_detail=safe_detail,
        )
    )
    session.add(
        CommunicationAuditModel(
            tenant_id=row.tenant_id,
            aggregate_type="message",
            aggregate_id=row.id,
            action=event_type,
            outcome="accepted",
            actor_id=_actor(request),
            correlation_id=_correlation(request),
        )
    )


def _domain_fingerprint(kind: str, aggregate_key: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "aggregate_key": aggregate_key, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def _record_domain_mutation(
    session: AsyncSession,
    *,
    tenant_id: str,
    aggregate_type: str,
    aggregate_key: str,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    result_version: int,
) -> bool:
    fingerprint = _domain_fingerprint(kind, aggregate_key, payload)
    prior = await session.scalar(
        select(DomainMutationModel).where(
            DomainMutationModel.tenant_id == tenant_id,
            DomainMutationModel.mutation_type == kind,
            DomainMutationModel.idempotency_key == idempotency_key,
        )
    )
    if prior is not None:
        if prior.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return False
    session.add(
        DomainMutationModel(
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_key=aggregate_key,
            mutation_type=kind,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            result_version=result_version,
        )
    )
    return True


def _audit_domain(
    session: AsyncSession,
    *,
    tenant_id: str,
    aggregate_type: str,
    aggregate_id: UUID,
    action: str,
    request: Request | None,
) -> None:
    session.add(
        CommunicationAuditModel(
            tenant_id=tenant_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            action=action,
            outcome="completed",
            actor_id=_actor(request),
            correlation_id=_correlation(request),
        )
    )


def _tenant(header_tenant: str, body_tenant: str | None) -> str:
    if body_tenant is not None and body_tenant != header_tenant:
        raise HTTPException(status_code=403, detail="tenant_mismatch")
    return header_tenant


def _recipient(value: str, channel: Channel) -> str:
    normalized = value.strip()
    if channel == Channel.EMAIL:
        return normalized.lower()
    if channel in {Channel.SMS, Channel.WHATSAPP}:
        prefix = "+" if normalized.startswith("+") else ""
        return prefix + re.sub(r"\D", "", normalized)
    return normalized


def _fingerprint(tenant_id: str, body: MessageCreate, recipient: str) -> str:
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "channel": body.channel.value,
        "recipient": recipient,
        "template_key": body.template_key,
        "purpose": body.purpose.value,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@app.get("/health/live")
@app.get("/health")
def health(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {"status": "ok", "service": SERVICE, "timestamp": datetime.now(timezone.utc).isoformat(), "correlation_id": correlation_id}


@app.get("/health/ready")
@app.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2.0)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE, "dependencies": {"database": "unavailable"}, "correlation_id": request.state.correlation_id})
    return {"status": "ready", "service": SERVICE, "dependencies": {"database": "ready", "configuration": "ready"}, "correlation_id": request.state.correlation_id}


@app.get("/metrics", dependencies=[Depends(require_scope("metrics.read"))])
async def metrics(session: AsyncSession = Depends(get_session)) -> Response:
    bounded = {
        DeliveryOutboxModel: (DELIVERY_OUTBOX, ("pending", "processing", "completed", "dead_letter")),
        CommunicationOperationModel: (
            OPERATIONS,
            ("pending", "processing", "accepted", "failed", "reconciliation_required"),
        ),
        ProviderInboxModel: (PROVIDER_INBOX, ("processed", "dead_letter")),
    }
    for model, (gauge, states) in bounded.items():
        rows = await session.execute(select(model.state, func.count()).group_by(model.state))
        counts = {str(state): int(count) for state, count in rows.all()}
        for state in states:
            gauge.labels(state=state).set(counts.get(state, 0))
    body, media_type = render()
    return Response(content=body, media_type=media_type)


@app.get("/version")
def version(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {"service": SERVICE, "application_version": app.version, "release_id": os.getenv("CODESTRA_RELEASE_VERSION", "unknown"), "api_versions": ["v1"], "git_sha": os.getenv("CODESTRA_GIT_SHA", "unknown"), "image_digest": os.getenv("CODESTRA_IMAGE_DIGEST", "unknown"), "build_timestamp": os.getenv("CODESTRA_BUILD_TIMESTAMP", "unknown"), "migration_revision": os.getenv("CODESTRA_MIGRATION_REVISION", "unknown"), "schema_version": os.getenv("CODESTRA_MIGRATION_REVISION", "unknown"), "configuration_checksum": os.getenv("CODESTRA_CONFIGURATION_CHECKSUM", "unknown"), "environment": os.getenv("CODESTRA_ENVIRONMENT", "unknown"), "correlation_id": correlation_id}


@app.get("/capabilities")
@router.get("/capabilities")
def capabilities(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {
        "service": SERVICE,
        "maintenance_mode": os.getenv("MAINTENANCE_MODE", "false").lower() == "true",
        "degraded_mode": False,
        "business_writes_enabled": BUSINESS_WRITES_ENABLED,
        "external_delivery_enabled": EXTERNAL_DELIVERY_ENABLED,
        "live_email_enabled": False,
        "live_sms_enabled": False,
        "live_pstn_enabled": False,
        "read_only_mode": not BUSINESS_WRITES_ENABLED,
        "simulation_enabled": not EXTERNAL_DELIVERY_ENABLED,
        "supported_api_versions": ["v1"],
        "email": True,
        "sms": True,
        "whatsapp": True,
        "push": True,
        "consent_enforcement": True,
        "suppression_enforcement": True,
        "external_delivery": EXTERNAL_DELIVERY_ENABLED,
        "correlation_id": correlation_id,
    }


@router.post(
    "/messages",
    response_model=Message,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scope("communications.send"))],
)
async def create_message(
    body: MessageCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> MessageModel:
    _require_business_writes()
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    if body.idempotency_key is not None and body.idempotency_key != idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_header_body_mismatch")
    recipient = _recipient(body.recipient, body.channel)
    if not recipient:
        raise HTTPException(status_code=400, detail="recipient_invalid")
    fingerprint = _fingerprint(tenant_id, body, recipient)

    existing = await session.execute(
        select(MessageModel).where(
            MessageModel.tenant_id == tenant_id,
            MessageModel.idempotency_key == idempotency_key,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return row

    suppressed = await session.execute(
        select(SuppressionModel).where(
            SuppressionModel.tenant_id == tenant_id,
            SuppressionModel.channel == body.channel.value,
            SuppressionModel.recipient == recipient,
            SuppressionModel.active.is_(True),
        )
    )
    if suppressed.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="recipient_suppressed")

    if body.purpose == Purpose.MARKETING:
        consent = await session.execute(
            select(ConsentModel).where(
                ConsentModel.tenant_id == tenant_id,
                ConsentModel.subject_key == recipient,
                ConsentModel.channel == body.channel.value,
                ConsentModel.status == "granted",
            ).with_for_update()
        )
        if consent.scalar_one_or_none() is None:
            raise HTTPException(status_code=409, detail="marketing_consent_missing")

    row = MessageModel(
        tenant_id=tenant_id,
        channel=body.channel.value,
        recipient=recipient,
        template_key=body.template_key,
        purpose=body.purpose.value,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        status="accepted_delivery_disabled" if not EXTERNAL_DELIVERY_ENABLED else "queued",
    )
    session.add(row)
    try:
        await session.flush()
        if EXTERNAL_DELIVERY_ENABLED:
            operation = CommunicationOperationModel(
                tenant_id=tenant_id,
                message_id=row.id,
                kind="deliver",
                state="pending",
                idempotency_key=idempotency_key,
                correlation_id=_correlation(request),
            )
            session.add(operation)
            await session.flush()
            row.operation_id = operation.id
            session.add(
                DeliveryOutboxModel(
                    tenant_id=tenant_id,
                    operation_id=operation.id,
                    payload_json=json.dumps(
                        {
                            "operation_id": str(operation.id),
                            "message_id": str(row.id),
                            "channel": row.channel,
                            "recipient": row.recipient,
                            "template_key": row.template_key,
                            "purpose": row.purpose,
                            "tenant_id": tenant_id,
                            "correlation_id": _correlation(request),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        await _message_event(
            session,
            row,
            event_type="communication.message.accepted",
            previous_status=None,
            request=request,
            safe_detail="external_delivery_disabled" if not EXTERNAL_DELIVERY_ENABLED else "queued",
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(MessageModel).where(
                MessageModel.tenant_id == tenant_id,
                MessageModel.idempotency_key == idempotency_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None or row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return row
    await session.refresh(row)

    return row


@router.get(
    "/messages",
    response_model=list[Message],
    dependencies=[Depends(require_scope("communications.read"))],
)
async def list_messages(
    x_tenant_id: TenantHeader,
    status_filter: str | None = Query(default=None, alias="status", max_length=48),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[MessageModel]:
    statement = select(MessageModel).where(MessageModel.tenant_id == x_tenant_id)
    if status_filter:
        statement = statement.where(MessageModel.status == status_filter)
    rows = await session.scalars(statement.order_by(MessageModel.created_at.desc()).limit(limit))
    return list(rows.all())


@router.get(
    "/messages/{message_id}",
    response_model=Message,
    dependencies=[Depends(require_scope("communications.read"))],
)
async def get_message(
    message_id: UUID,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> MessageModel:
    result = await session.execute(
        select(MessageModel).where(
            MessageModel.id == message_id,
            MessageModel.tenant_id == x_tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    return row


@router.get(
    "/messages/{message_id}/events",
    response_model=list[MessageEvent],
    dependencies=[Depends(require_scope("communications.read"))],
)
async def get_message_events(
    message_id: UUID,
    x_tenant_id: TenantHeader,
    after: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[MessageEventModel]:
    exists = await session.scalar(
        select(MessageModel.id).where(
            MessageModel.id == message_id, MessageModel.tenant_id == x_tenant_id
        )
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    statement = select(MessageEventModel).where(
        MessageEventModel.tenant_id == x_tenant_id,
        MessageEventModel.message_id == message_id,
    )
    if after is not None:
        statement = statement.where(MessageEventModel.id > after)
    rows = await session.scalars(statement.order_by(MessageEventModel.id).limit(limit))
    return list(rows.all())


@router.get(
    "/operations",
    response_model=list[Operation],
    dependencies=[Depends(require_scope("communications.operations.read"))],
)
async def list_operations(
    x_tenant_id: TenantHeader,
    state: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[CommunicationOperationModel]:
    statement = select(CommunicationOperationModel).where(
        CommunicationOperationModel.tenant_id == x_tenant_id
    )
    if state is not None:
        statement = statement.where(CommunicationOperationModel.state == state)
    rows = await session.scalars(
        statement.order_by(CommunicationOperationModel.created_at.desc()).limit(limit)
    )
    return list(rows.all())


@router.get(
    "/operations/{operation_id}",
    response_model=Operation,
    dependencies=[Depends(require_scope("communications.operations.read"))],
)
async def get_operation(
    operation_id: UUID,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> CommunicationOperationModel:
    row = await session.scalar(
        select(CommunicationOperationModel).where(
            CommunicationOperationModel.id == operation_id,
            CommunicationOperationModel.tenant_id == x_tenant_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="operation_not_found")
    return row


@router.post(
    "/operations/{operation_id}/reconcile",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_scope("communications.operations.reconcile"))],
)
async def reconcile_operation(
    operation_id: UUID,
    body: ReconcileOperation,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> CommunicationOperationModel:
    _require_business_writes()
    target = await session.scalar(
        select(CommunicationOperationModel)
        .where(
            CommunicationOperationModel.id == operation_id,
            CommunicationOperationModel.tenant_id == x_tenant_id,
        )
        .with_for_update()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="operation_not_found")
    message = await session.scalar(
        select(MessageModel)
        .where(
            MessageModel.id == target.message_id,
            MessageModel.tenant_id == x_tenant_id,
        )
        .with_for_update()
    )
    if message is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    payload = {
        "target_operation_id": str(target.id),
        "expected_message_version": body.expected_message_version,
    }
    aggregate_key = str(target.id)
    if not await _record_domain_mutation(
        session,
        tenant_id=x_tenant_id,
        aggregate_type="operation",
        aggregate_key=aggregate_key,
        kind="operation.reconcile",
        idempotency_key=idempotency_key,
        payload=payload,
        result_version=message.resource_version,
    ):
        replay = await session.scalar(
            select(CommunicationOperationModel).where(
                CommunicationOperationModel.tenant_id == x_tenant_id,
                CommunicationOperationModel.idempotency_key == idempotency_key,
                CommunicationOperationModel.kind == "reconcile",
            )
        )
        if replay is None:
            raise HTTPException(status_code=409, detail="reconciliation_replay_missing")
        return replay
    if message.resource_version != body.expected_message_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if target.state != "reconciliation_required":
        raise HTTPException(status_code=409, detail="operation_not_reconcilable")
    if not target.middleware_operation_id:
        raise HTTPException(status_code=409, detail="middleware_operation_identity_unknown")
    operation = CommunicationOperationModel(
        tenant_id=x_tenant_id,
        message_id=message.id,
        kind="reconcile",
        state="pending",
        idempotency_key=idempotency_key,
        correlation_id=_correlation(request),
    )
    session.add(operation)
    await session.flush()
    session.add(
        DeliveryOutboxModel(
            tenant_id=x_tenant_id,
            operation_id=operation.id,
            payload_json=json.dumps(
                {
                    "action": "reconcile",
                    "operation_id": str(operation.id),
                    "target_operation_id": str(target.id),
                    "target_kind": target.kind,
                    "middleware_operation_id": target.middleware_operation_id,
                    "message_id": str(message.id),
                    "tenant_id": x_tenant_id,
                    "correlation_id": operation.correlation_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )
    _audit_domain(
        session,
        tenant_id=x_tenant_id,
        aggregate_type="operation",
        aggregate_id=target.id,
        action="communication.operation.reconciliation_requested",
        request=request,
    )
    await session.commit()
    await session.refresh(operation)
    return operation


@router.post(
    "/messages/{message_id}/cancel",
    response_model=Message,
    dependencies=[Depends(require_scope("communications.cancel"))],
)
async def cancel_message(
    message_id: UUID,
    body: CancelMessage,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> MessageModel:
    _require_business_writes()
    row = await session.scalar(
        select(MessageModel)
        .where(MessageModel.id == message_id, MessageModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prior = await session.scalar(
        select(MessageMutationModel).where(
            MessageMutationModel.tenant_id == x_tenant_id,
            MessageMutationModel.message_id == message_id,
            MessageMutationModel.mutation_type == "cancel",
            MessageMutationModel.idempotency_key == idempotency_key,
        )
    )
    if prior is not None:
        if prior.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.status not in {"accepted_delivery_disabled", "queued", "middleware_accepted"}:
        raise HTTPException(status_code=409, detail="message_not_cancellable")
    previous = row.status
    delivery_operation_id = row.operation_id
    delivery_operation = None
    delivery_outbox = None
    if delivery_operation_id is not None:
        delivery_operation = await session.scalar(
            select(CommunicationOperationModel)
            .where(
                CommunicationOperationModel.id == delivery_operation_id,
                CommunicationOperationModel.tenant_id == x_tenant_id,
                CommunicationOperationModel.kind == "deliver",
            )
            .with_for_update()
        )
        delivery_outbox = await session.scalar(
            select(DeliveryOutboxModel)
            .where(DeliveryOutboxModel.operation_id == delivery_operation_id)
            .with_for_update()
        )
    middleware_operation_id = (
        delivery_operation.middleware_operation_id if delivery_operation is not None else None
    )
    if delivery_outbox is not None and delivery_outbox.state == "pending":
        delivery_outbox.state = "cancelled"
        delivery_operation.state = "cancelled_before_dispatch"
        row.status = "cancelled"
    elif middleware_operation_id:
        row.status = "cancellation_pending"
    elif delivery_operation_id is not None:
        row.status = "reconciliation_required"
    else:
        row.status = "cancelled"
    row.cancelled_at = datetime.now(timezone.utc) if row.status == "cancelled" else None
    row.resource_version += 1
    session.add(
        MessageMutationModel(
            tenant_id=x_tenant_id,
            message_id=message_id,
            mutation_type="cancel",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            result_version=row.resource_version,
        )
    )
    if row.status == "cancellation_pending" and middleware_operation_id is not None:
        cancel_operation = CommunicationOperationModel(
            tenant_id=x_tenant_id,
            message_id=message_id,
            kind="cancel",
            state="pending",
            idempotency_key=idempotency_key,
            correlation_id=_correlation(request),
        )
        session.add(cancel_operation)
        await session.flush()
        row.operation_id = cancel_operation.id
        session.add(
            DeliveryOutboxModel(
                tenant_id=x_tenant_id,
                operation_id=cancel_operation.id,
                payload_json=json.dumps(
                    {
                        "operation_id": str(cancel_operation.id),
                        "delivery_operation_id": middleware_operation_id,
                        "message_id": str(message_id),
                        "action": "cancel",
                        "reason": body.reason,
                        "tenant_id": x_tenant_id,
                        "correlation_id": _correlation(request),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    await _message_event(
        session,
        row,
        event_type=(
            "communication.message.cancelled"
            if row.status == "cancelled"
            else "communication.message.cancellation_requested"
        ),
        previous_status=previous,
        request=request,
        safe_detail=body.reason,
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/templates",
    response_model=Template,
    status_code=201,
    dependencies=[Depends(require_scope("communications.templates.write"))],
)
async def create_template(
    body: TemplateCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> TemplateModel:
    _require_business_writes()
    payload = body.model_dump(mode="json")
    fingerprint = _domain_fingerprint("template.create", f"{body.key}:{body.locale}", payload)
    prior = await session.scalar(
        select(TemplateModel).where(
            TemplateModel.tenant_id == x_tenant_id,
            TemplateModel.idempotency_key == idempotency_key,
        )
    )
    if prior is not None:
        if prior.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return prior
    row = TemplateModel(
        tenant_id=x_tenant_id,
        key=body.key,
        channel=body.channel.value,
        locale=body.locale,
        subject_template=body.subject_template,
        body_template=body.body_template,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        await session.flush()
        _audit_domain(
            session, tenant_id=x_tenant_id, aggregate_type="template", aggregate_id=row.id,
            action="communication.template.created", request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        replay = await session.scalar(
            select(TemplateModel).where(
                TemplateModel.tenant_id == x_tenant_id,
                TemplateModel.idempotency_key == idempotency_key,
            )
        )
        if replay is None or replay.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="template_key_conflict") from exc
        return replay
    await session.refresh(row)
    return row


@router.get(
    "/templates",
    response_model=list[Template],
    dependencies=[Depends(require_scope("communications.templates.read"))],
)
async def list_templates(
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> list[TemplateModel]:
    rows = await session.scalars(
        select(TemplateModel).where(TemplateModel.tenant_id == x_tenant_id).order_by(TemplateModel.key)
    )
    return list(rows.all())


@router.get(
    "/templates/{template_id}",
    response_model=Template,
    dependencies=[Depends(require_scope("communications.templates.read"))],
)
async def get_template(
    template_id: UUID,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> TemplateModel:
    row = await session.scalar(
        select(TemplateModel).where(TemplateModel.id == template_id, TemplateModel.tenant_id == x_tenant_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    return row


@router.patch(
    "/templates/{template_id}",
    response_model=Template,
    dependencies=[Depends(require_scope("communications.templates.write"))],
)
async def update_template(
    template_id: UUID,
    body: TemplateUpdate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> TemplateModel:
    _require_business_writes()
    row = await session.scalar(
        select(TemplateModel)
        .where(TemplateModel.id == template_id, TemplateModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    payload = body.model_dump(mode="json")
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="template", aggregate_key=str(template_id),
        kind="template.update", idempotency_key=idempotency_key, payload=payload,
        result_version=row.resource_version + 1,
    ):
        return row
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if "subject_template" in body.model_fields_set:
        row.subject_template = body.subject_template
    for field in ("body_template", "active"):
        value = getattr(body, field)
        if field in body.model_fields_set and value is not None:
            setattr(row, field, value)
    row.resource_version += 1
    _audit_domain(
        session, tenant_id=x_tenant_id, aggregate_type="template", aggregate_id=row.id,
        action="communication.template.updated", request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/consents",
    response_model=Consent,
    dependencies=[Depends(require_scope("communications.consent.write"))],
)
async def grant_consent(
    body: ConsentChange,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> ConsentModel:
    _require_business_writes()
    subject = _recipient(body.subject_key, body.channel)
    row = await session.scalar(
        select(ConsentModel)
        .where(
            ConsentModel.tenant_id == x_tenant_id,
            ConsentModel.subject_key == subject,
            ConsentModel.channel == body.channel.value,
        )
        .with_for_update()
    )
    version = 1 if row is None else row.resource_version + 1
    payload = body.model_dump(mode="json")
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="consent",
        aggregate_key=f"{body.channel.value}:{subject}", kind="consent.grant",
        idempotency_key=idempotency_key, payload=payload, result_version=version,
    ):
        assert row is not None
        return row
    if row is None:
        row = ConsentModel(
            tenant_id=x_tenant_id, subject_key=subject, channel=body.channel.value,
            status="granted", source=body.source, evidence=body.evidence,
            idempotency_key=idempotency_key,
            request_fingerprint=_domain_fingerprint("consent.grant", subject, payload),
            resource_version=1,
        )
        session.add(row)
    else:
        if body.expected_version is None or row.resource_version != body.expected_version:
            raise HTTPException(status_code=409, detail="stale_resource_version")
        row.status = "granted"
        row.source = body.source
        row.evidence = body.evidence
        row.resource_version += 1
    try:
        await session.flush()
        _audit_domain(
            session, tenant_id=x_tenant_id, aggregate_type="consent", aggregate_id=row.id,
            action="communication.consent.granted", request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        mutation = await session.scalar(
            select(DomainMutationModel).where(
                DomainMutationModel.tenant_id == x_tenant_id,
                DomainMutationModel.mutation_type == "consent.grant",
                DomainMutationModel.idempotency_key == idempotency_key,
            )
        )
        winner = await session.scalar(
            select(ConsentModel).where(
                ConsentModel.tenant_id == x_tenant_id,
                ConsentModel.subject_key == subject,
                ConsentModel.channel == body.channel.value,
            )
        )
        expected = _domain_fingerprint(
            "consent.grant", f"{body.channel.value}:{subject}", payload
        )
        if mutation is None or mutation.request_fingerprint != expected or winner is None:
            raise HTTPException(status_code=409, detail="consent_conflict") from exc
        return winner
    await session.refresh(row)
    return row


@router.get(
    "/consents",
    response_model=list[Consent],
    dependencies=[Depends(require_scope("communications.consent.read"))],
)
async def list_consents(
    x_tenant_id: TenantHeader,
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    session: AsyncSession = Depends(get_session),
) -> list[ConsentModel]:
    statement = select(ConsentModel).where(ConsentModel.tenant_id == x_tenant_id)
    if status_filter:
        statement = statement.where(ConsentModel.status == status_filter)
    rows = await session.scalars(statement.order_by(ConsentModel.updated_at.desc()).limit(100))
    return list(rows.all())


@router.post(
    "/consents/revoke",
    response_model=Consent,
    dependencies=[Depends(require_scope("communications.consent.write"))],
)
async def revoke_consent(
    body: ConsentChange,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> ConsentModel:
    _require_business_writes()
    subject = _recipient(body.subject_key, body.channel)
    row = await session.scalar(
        select(ConsentModel)
        .where(
            ConsentModel.tenant_id == x_tenant_id,
            ConsentModel.subject_key == subject,
            ConsentModel.channel == body.channel.value,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="consent_not_found")
    payload = body.model_dump(mode="json")
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="consent",
        aggregate_key=f"{body.channel.value}:{subject}", kind="consent.revoke",
        idempotency_key=idempotency_key, payload=payload, result_version=row.resource_version + 1,
    ):
        return row
    if body.expected_version is None or row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    row.status = "revoked"
    row.source = body.source
    row.evidence = body.evidence
    row.resource_version += 1
    _audit_domain(
        session, tenant_id=x_tenant_id, aggregate_type="consent", aggregate_id=row.id,
        action="communication.consent.revoked", request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


@router.post(
    "/suppressions",
    response_model=Suppression,
    dependencies=[Depends(require_scope("communications.suppression.write"))],
)
async def create_suppression(
    body: SuppressionCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> SuppressionModel:
    _require_business_writes()
    recipient = _recipient(body.recipient, body.channel)
    row = await session.scalar(
        select(SuppressionModel)
        .where(
            SuppressionModel.tenant_id == x_tenant_id,
            SuppressionModel.channel == body.channel.value,
            SuppressionModel.recipient == recipient,
        )
        .with_for_update()
    )
    payload = body.model_dump(mode="json")
    version = 1 if row is None else row.resource_version + 1
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="suppression",
        aggregate_key=f"{body.channel.value}:{recipient}", kind="suppression.create",
        idempotency_key=idempotency_key, payload=payload, result_version=version,
    ):
        assert row is not None
        return row
    if row is None:
        row = SuppressionModel(
            tenant_id=x_tenant_id, channel=body.channel.value, recipient=recipient,
            reason=body.reason, active=True, idempotency_key=idempotency_key,
            request_fingerprint=_domain_fingerprint("suppression.create", recipient, payload),
        )
        session.add(row)
    else:
        row.active = True
        row.reason = body.reason
        row.resource_version += 1
    try:
        await session.flush()
        _audit_domain(
            session, tenant_id=x_tenant_id, aggregate_type="suppression", aggregate_id=row.id,
            action="communication.suppression.created", request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        mutation = await session.scalar(
            select(DomainMutationModel).where(
                DomainMutationModel.tenant_id == x_tenant_id,
                DomainMutationModel.mutation_type == "suppression.create",
                DomainMutationModel.idempotency_key == idempotency_key,
            )
        )
        winner = await session.scalar(
            select(SuppressionModel).where(
                SuppressionModel.tenant_id == x_tenant_id,
                SuppressionModel.channel == body.channel.value,
                SuppressionModel.recipient == recipient,
            )
        )
        expected = _domain_fingerprint(
            "suppression.create", f"{body.channel.value}:{recipient}", payload
        )
        if mutation is None or mutation.request_fingerprint != expected or winner is None:
            raise HTTPException(status_code=409, detail="suppression_conflict") from exc
        return winner
    await session.refresh(row)
    return row


@router.get(
    "/suppressions",
    response_model=list[Suppression],
    dependencies=[Depends(require_scope("communications.suppression.read"))],
)
async def list_suppressions(
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> list[SuppressionModel]:
    rows = await session.scalars(
        select(SuppressionModel).where(
            SuppressionModel.tenant_id == x_tenant_id, SuppressionModel.active.is_(True)
        ).order_by(SuppressionModel.created_at.desc()).limit(100)
    )
    return list(rows.all())


@router.delete(
    "/suppressions/{suppression_id}",
    response_model=Suppression,
    dependencies=[Depends(require_scope("communications.suppression.write"))],
)
async def delete_suppression(
    suppression_id: UUID,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    expected_version: int = Query(ge=1),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> SuppressionModel:
    _require_business_writes()
    row = await session.scalar(
        select(SuppressionModel)
        .where(SuppressionModel.id == suppression_id, SuppressionModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="suppression_not_found")
    payload = {"expected_version": expected_version}
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="suppression", aggregate_key=str(suppression_id),
        kind="suppression.delete", idempotency_key=idempotency_key, payload=payload,
        result_version=row.resource_version + 1,
    ):
        return row
    if row.resource_version != expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    row.active = False
    row.resource_version += 1
    _audit_domain(
        session, tenant_id=x_tenant_id, aggregate_type="suppression", aggregate_id=row.id,
        action="communication.suppression.deleted", request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


def _webhook_secret(provider: str) -> bytes:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", provider):
        raise HTTPException(status_code=404, detail="provider_not_found")
    root_value = os.getenv("PROVIDER_WEBHOOK_SECRET_DIR", "").strip()
    if not root_value:
        raise HTTPException(status_code=503, detail="webhook_identity_unavailable")
    root = Path(root_value).resolve()
    path = root / f"{provider}.secret"
    if path.is_symlink() or path.parent.resolve() != root or not path.is_file():
        raise HTTPException(status_code=503, detail="webhook_identity_unavailable")
    value = path.read_bytes().strip()
    if len(value) < 32:
        raise HTTPException(status_code=503, detail="webhook_identity_unavailable")
    return value


@router.get(
    "/providers",
    response_model=list[ProviderStatus],
    dependencies=[Depends(require_scope("communications.providers.read"))],
)
def provider_statuses() -> list[ProviderStatus]:
    state = "middleware_routed" if EXTERNAL_DELIVERY_ENABLED else "delivery_disabled"
    health = "not_probed" if EXTERNAL_DELIVERY_ENABLED else "disabled"
    return [
        ProviderStatus(
            channel=channel,
            route="middleware",
            state=state,
            health=health,
            reputation="not_applicable" if not EXTERNAL_DELIVERY_ENABLED else "not_probed",
        )
        for channel in Channel
    ]


@app.post("/v1/webhooks/communications/{provider}/results", status_code=202)
async def provider_result(
    provider: str,
    request: Request,
    x_tenant_id: TenantHeader,
    x_provider_timestamp: Annotated[str, Header(alias="X-Provider-Timestamp", min_length=1, max_length=20)],
    x_provider_event_id: Annotated[str, Header(alias="X-Provider-Event-ID", min_length=1, max_length=160)],
    x_provider_signature: Annotated[str, Header(alias="X-Provider-Signature", min_length=64, max_length=80)],
    x_correlation_id: Annotated[str, Header(alias="X-Correlation-ID", min_length=8, max_length=128)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    body = await request.body()
    if len(body) > 1_048_576:
        raise HTTPException(status_code=413, detail="webhook_body_too_large")
    try:
        timestamp = int(x_provider_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="webhook_timestamp_invalid") from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=401, detail="webhook_timestamp_expired")
    supplied = x_provider_signature.removeprefix("sha256=").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise HTTPException(status_code=401, detail="webhook_signature_invalid")
    signed = b".".join(
        (
            provider.encode(),
            x_tenant_id.encode(),
            x_provider_event_id.encode(),
            x_provider_timestamp.encode(),
            body,
        )
    )
    expected = hmac.new(_webhook_secret(provider), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="webhook_signature_invalid")
    try:
        event = ProviderResult.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="webhook_payload_invalid") from exc
    payload_hash = hashlib.sha256(body).hexdigest()

    message = await session.scalar(
        select(MessageModel)
        .where(MessageModel.id == event.message_id, MessageModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    if message is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    prior = await session.scalar(
        select(ProviderInboxModel).where(
            ProviderInboxModel.tenant_id == x_tenant_id,
            ProviderInboxModel.provider == provider,
            ProviderInboxModel.provider_event_id == x_provider_event_id,
        )
    )
    if prior is not None:
        if prior.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="webhook_replay_conflict")
        return {"status": "already_processed", "event_id": x_provider_event_id}
    if (
        message.provider_message_id is not None
        and event.provider_message_id is not None
        and message.provider_message_id != event.provider_message_id
    ):
        raise HTTPException(status_code=409, detail="provider_message_mismatch")
    previous = message.status
    allowed_transitions = {
        "middleware_accepted": {"sent", "delivered", "failed", "bounced", "complained", "cancelled"},
        "sent": {"delivered", "failed", "bounced", "complained", "cancelled"},
        "delivered": {"complained"},
        "failed": set(),
        "bounced": {"complained"},
        "complained": set(),
        "cancelled": set(),
    }
    transition_allowed = (
        event.event_type == previous
        or event.event_type in allowed_transitions.get(previous, set())
    )
    if transition_allowed:
        message.status = event.event_type
    if event.provider_message_id is not None:
        message.provider_message_id = event.provider_message_id
    if message.status != previous:
        message.resource_version += 1
    session.add(
        ProviderInboxModel(
            tenant_id=x_tenant_id,
            provider=provider,
            provider_event_id=x_provider_event_id,
            payload_hash=payload_hash,
            message_id=message.id,
            event_type=event.event_type,
        )
    )
    await _message_event(
        session,
        message,
        event_type=(
            f"communication.message.{event.event_type}"
            if transition_allowed
            else "communication.message.provider_event_ignored"
        ),
        previous_status=previous,
        request=request,
        safe_detail=provider if transition_allowed else f"{provider}:stale_transition",
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        replay = await session.scalar(
            select(ProviderInboxModel).where(
                ProviderInboxModel.tenant_id == x_tenant_id,
                ProviderInboxModel.provider == provider,
                ProviderInboxModel.provider_event_id == x_provider_event_id,
            )
        )
        if replay is None or replay.payload_hash != payload_hash:
            raise HTTPException(status_code=409, detail="webhook_replay_conflict") from exc
        return {"status": "already_processed", "event_id": x_provider_event_id}
    return {"status": "processed", "event_id": x_provider_event_id}


app.include_router(router)
