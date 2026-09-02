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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .auth import require_scope
from .models import (
    CommunicationAuditModel,
    CommunicationEventOutboxModel,
    CommunicationOperationModel,
    ConsentModel,
    DomainMutationModel,
    DeliveryOutboxModel,
    MessageEventModel,
    MessageModel,
    MessageMutationModel,
    ProviderInboxModel,
    PreferenceModel,
    SenderIdentityModel,
    SendingDomainModel,
    SuppressionModel,
    TemplateModel,
)
from .events import record_message_event
from .metrics import DELIVERY_OUTBOX, HTTP_DURATION, HTTP_REQUESTS, OPERATIONS, PROVIDER_INBOX, render

app = FastAPI(title="Codestra Communication API", version="0.3.0")
router = APIRouter(prefix="/v1/communications")
EXTERNAL_DELIVERY_ENABLED = os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").lower() == "true"
BUSINESS_WRITES_ENABLED = os.getenv("BUSINESS_WRITES_ENABLED", "false").lower() == "true"
SERVICE = "codestra-communication"


@app.middleware("http")
async def operational_headers(request: Request, call_next):
    supplied_correlation = request.headers.get("X-Correlation-ID")
    correlation_id = supplied_correlation or str(uuid4())
    request.state.correlation_id = correlation_id
    if supplied_correlation is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", supplied_correlation
    ):
        return JSONResponse(
            status_code=400,
            content={"detail": "correlation_id_invalid", "correlation_id": str(uuid4())},
            headers={"Cache-Control": "no-store"},
        )
    mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    read_only_post = request.url.path.endswith("/render")
    governed_path = request.url.path.startswith("/v1/communications/") or request.url.path.startswith("/v1/webhooks/")
    if mutation and governed_path and not read_only_post and supplied_correlation is None:
        return JSONResponse(
            status_code=400,
            content={"detail": "correlation_id_required", "correlation_id": correlation_id},
            headers={"Cache-Control": "no-store", "X-Correlation-ID": correlation_id},
        )
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


class TemplateRenderRequest(BaseModel):
    variables: dict[str, str | int | float | bool]
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_variables(self):
        if len(self.variables) > 200:
            raise ValueError("at most 200 template variables are allowed")
        for name, value in self.variables.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", name):
                raise ValueError("template variable name is invalid")
            if len(str(value)) > 4000:
                raise ValueError("template variable value is too long")
        return self


class TemplateRenderResult(BaseModel):
    subject: str | None
    body: str
    missing_variables: list[str]


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


class PreferenceWrite(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    channel: Channel
    topic: str | None = Field(default=None, min_length=1, max_length=120)
    consent: str = Field(pattern=r"^(granted|denied|unknown)$")
    source: str = Field(default="unspecified", min_length=1, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=1)
    model_config = {"extra": "forbid"}


class Preference(BaseModel):
    preference_id: UUID
    subject: str
    channel: Channel
    topic: str | None
    consent: str
    source: str
    metadata: dict[str, Any]
    resource_version: int
    updated_at: datetime


class PreferenceList(BaseModel):
    items: list[Preference]
    next_cursor: str | None = None


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


class ProviderHealthItem(BaseModel):
    provider: str
    channel: Channel
    status: str
    reason: str | None = None


class ProviderHealth(BaseModel):
    status: str
    checked_at: datetime = Field(serialization_alias="checkedAt")
    providers: list[ProviderHealthItem]


class UsageTotal(BaseModel):
    channel: Channel
    accepted: int
    delivered: int
    failed: int
    suppressed: int


class UsageReport(BaseModel):
    from_at: datetime = Field(serialization_alias="from")
    to: datetime
    totals: list[UsageTotal]


class DomainWrite(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class SendingDomain(BaseModel):
    domain_id: UUID
    domain: str
    metadata: dict[str, Any]
    status: str
    checks: dict[str, str]
    resource_version: int
    created_at: datetime
    updated_at: datetime


class SendingDomainList(BaseModel):
    items: list[SendingDomain]
    next_cursor: str | None = None


class SenderIdentityWrite(BaseModel):
    channel: Channel
    address: str = Field(min_length=1, max_length=300)
    display_name: str | None = Field(default=None, max_length=160)
    domain_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=1)
    model_config = {"extra": "forbid"}


class SenderIdentity(BaseModel):
    sender_identity_id: UUID
    channel: Channel
    address: str
    display_name: str | None
    domain_id: UUID | None
    metadata: dict[str, Any]
    status: str
    resource_version: int
    created_at: datetime
    updated_at: datetime


class SenderIdentityList(BaseModel):
    items: list[SenderIdentity]
    next_cursor: str | None = None


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
    provider: str | None = None,
    provider_event_type: str | None = None,
) -> None:
    await record_message_event(
        session, row, event_type=event_type, previous_status=previous_status,
        actor_id=_actor(request), correlation_id=_correlation(request), safe_detail=safe_detail,
        provider=provider, provider_event_type=provider_event_type,
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
    event_depth = await session.scalar(
        select(func.count()).select_from(CommunicationEventOutboxModel).where(
            CommunicationEventOutboxModel.state.in_(("pending", "publishing"))
        )
    )
    from .metrics import EVENT_OUTBOX_DEPTH
    EVENT_OUTBOX_DEPTH.set(int(event_depth or 0))
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


@router.post(
    "/templates/{template_id}/render",
    response_model=TemplateRenderResult,
    dependencies=[Depends(require_scope("communications.templates.read"))],
)
async def render_template(
    template_id: UUID,
    body: TemplateRenderRequest,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> TemplateRenderResult:
    row = await session.scalar(select(TemplateModel).where(
        TemplateModel.id == template_id,
        TemplateModel.tenant_id == x_tenant_id,
        TemplateModel.active.is_(True),
    ))
    if row is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    pattern = re.compile(r"{{\s*([A-Za-z][A-Za-z0-9_.-]{0,79})\s*}}")
    names = set(pattern.findall(row.body_template))
    if row.subject_template:
        names.update(pattern.findall(row.subject_template))
    missing = sorted(names - body.variables.keys())
    def substitute(value: str | None) -> str | None:
        if value is None:
            return None
        return pattern.sub(
            lambda match: str(body.variables[match.group(1)])
            if match.group(1) in body.variables else match.group(0),
            value,
        )
    return TemplateRenderResult(
        subject=substitute(row.subject_template),
        body=substitute(row.body_template) or "",
        missing_variables=missing,
    )


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


def _preference_response(row: PreferenceModel) -> Preference:
    return Preference(
        preference_id=row.id,
        subject=row.subject,
        channel=Channel(row.channel),
        topic=row.topic or None,
        consent=row.consent,
        source=row.source,
        metadata=json.loads(row.metadata_json),
        resource_version=row.resource_version,
        updated_at=row.updated_at,
    )


@router.get(
    "/preferences",
    response_model=PreferenceList,
    dependencies=[Depends(require_scope("communications.preferences.read"))],
)
async def list_preferences(
    x_tenant_id: TenantHeader,
    subject: str | None = Query(default=None, min_length=1, max_length=300),
    cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> PreferenceList:
    statement = select(PreferenceModel).where(PreferenceModel.tenant_id == x_tenant_id)
    if subject is not None:
        statement = statement.where(PreferenceModel.subject == subject.strip())
    if cursor is not None:
        statement = statement.where(PreferenceModel.id > cursor)
    rows = list((await session.scalars(statement.order_by(PreferenceModel.id).limit(limit + 1))).all())
    return PreferenceList(
        items=[_preference_response(row) for row in rows[:limit]],
        next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
    )


@router.get(
    "/recipients/{recipient_id}/preferences",
    response_model=PreferenceList,
    dependencies=[Depends(require_scope("communications.preferences.read"))],
)
async def get_recipient_preferences(
    recipient_id: str,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> PreferenceList:
    if not recipient_id.strip() or len(recipient_id) > 300:
        raise HTTPException(status_code=422, detail="recipient_invalid")
    rows = list((await session.scalars(
        select(PreferenceModel).where(
            PreferenceModel.tenant_id == x_tenant_id,
            PreferenceModel.subject == recipient_id.strip(),
        ).order_by(PreferenceModel.id).limit(100)
    )).all())
    return PreferenceList(items=[_preference_response(row) for row in rows])


@router.put(
    "/preferences",
    response_model=Preference,
    dependencies=[Depends(require_scope("communications.preferences.write"))],
)
@router.post(
    "/preferences",
    response_model=Preference,
    dependencies=[Depends(require_scope("communications.preferences.write"))],
)
async def upsert_preference(
    body: PreferenceWrite,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> Preference:
    _require_business_writes()
    subject = _recipient(body.subject, body.channel)
    topic = body.topic or ""
    encoded_metadata = json.dumps(body.metadata, sort_keys=True, separators=(",", ":"))
    if len(encoded_metadata.encode("utf-8")) > 8192:
        raise HTTPException(status_code=413, detail="preference_metadata_too_large")
    payload = body.model_dump(mode="json") | {"subject": subject}
    fingerprint = _domain_fingerprint("preference.upsert", f"{body.channel.value}:{subject}:{topic}", payload)
    row = await session.scalar(
        select(PreferenceModel).where(
            PreferenceModel.tenant_id == x_tenant_id,
            PreferenceModel.subject == subject,
            PreferenceModel.channel == body.channel.value,
            PreferenceModel.topic == topic,
        ).with_for_update()
    )
    aggregate_key = f"{body.channel.value}:{subject}:{topic}"
    version = 1 if row is None else row.resource_version + 1
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="preference",
        aggregate_key=aggregate_key, kind="preference.upsert",
        idempotency_key=idempotency_key, payload=payload, result_version=version,
    ):
        if row is None:
            row = await session.scalar(select(PreferenceModel).where(
                PreferenceModel.tenant_id == x_tenant_id,
                PreferenceModel.subject == subject,
                PreferenceModel.channel == body.channel.value,
                PreferenceModel.topic == topic,
            ))
        if row is None:
            raise HTTPException(status_code=409, detail="preference_replay_missing")
        return _preference_response(row)
    if row is None:
        if body.expected_version is not None:
            raise HTTPException(status_code=409, detail="preference_not_found_for_expected_version")
        row = PreferenceModel(
            tenant_id=x_tenant_id, subject=subject, channel=body.channel.value,
            topic=topic, consent=body.consent, source=body.source,
            metadata_json=encoded_metadata, idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        session.add(row)
    else:
        if body.expected_version is None or row.resource_version != body.expected_version:
            raise HTTPException(status_code=409, detail="stale_resource_version")
        row.consent = body.consent
        row.source = body.source
        row.metadata_json = encoded_metadata
        row.request_fingerprint = fingerprint
        row.resource_version += 1
    try:
        await session.flush()
        _audit_domain(
            session, tenant_id=x_tenant_id, aggregate_type="preference", aggregate_id=row.id,
            action="communication.preference.upserted", request=request,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        mutation = await session.scalar(select(DomainMutationModel).where(
            DomainMutationModel.tenant_id == x_tenant_id,
            DomainMutationModel.mutation_type == "preference.upsert",
            DomainMutationModel.idempotency_key == idempotency_key,
        ))
        winner = await session.scalar(select(PreferenceModel).where(
            PreferenceModel.tenant_id == x_tenant_id,
            PreferenceModel.subject == subject,
            PreferenceModel.channel == body.channel.value,
            PreferenceModel.topic == topic,
        ))
        if mutation is None or mutation.request_fingerprint != fingerprint or winner is None:
            raise HTTPException(status_code=409, detail="preference_conflict") from exc
        row = winner
    await session.refresh(row)
    return _preference_response(row)


def _bounded_metadata(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 8192:
        raise HTTPException(status_code=413, detail="metadata_too_large")
    return encoded


def _normalize_domain(value: str) -> str:
    candidate = value.strip().rstrip(".").lower()
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise HTTPException(status_code=422, detail="domain_invalid") from exc
    if len(candidate) > 253 or not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
        candidate,
    ):
        raise HTTPException(status_code=422, detail="domain_invalid")
    return candidate


def _domain_response(row: SendingDomainModel) -> SendingDomain:
    return SendingDomain(
        domain_id=row.id, domain=row.domain, metadata=json.loads(row.metadata_json),
        status=row.status,
        checks={"spf": row.spf, "dkim": row.dkim, "dmarc": row.dmarc,
                "reverseDns": row.reverse_dns, "tls": row.tls, "bimi": row.bimi},
        resource_version=row.resource_version, created_at=row.created_at, updated_at=row.updated_at,
    )


@router.post(
    "/domains", response_model=SendingDomain, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("communications.domains.write"))],
)
async def create_domain(
    body: DomainWrite, x_tenant_id: TenantHeader, idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session), request: Request = None,
) -> SendingDomain:
    _require_business_writes()
    domain = _normalize_domain(body.domain)
    metadata_json = _bounded_metadata(body.metadata)
    fingerprint = _domain_fingerprint("domain.create", domain, {"domain": domain, "metadata": body.metadata})
    replay = await session.scalar(select(SendingDomainModel).where(
        SendingDomainModel.tenant_id == x_tenant_id,
        SendingDomainModel.idempotency_key == idempotency_key,
    ))
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return _domain_response(replay)
    row = SendingDomainModel(
        tenant_id=x_tenant_id, domain=domain, status="dns_required",
        metadata_json=metadata_json, idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        await session.flush()
        _audit_domain(session, tenant_id=x_tenant_id, aggregate_type="sending_domain",
                      aggregate_id=row.id, action="communication.domain.registered", request=request)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        winner = await session.scalar(select(SendingDomainModel).where(
            SendingDomainModel.tenant_id == x_tenant_id,
            SendingDomainModel.idempotency_key == idempotency_key,
        ))
        if winner is None or winner.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="domain_conflict") from exc
        row = winner
    await session.refresh(row)
    return _domain_response(row)


@router.get("/domains", response_model=SendingDomainList,
            dependencies=[Depends(require_scope("communications.domains.read"))])
async def list_domains(
    x_tenant_id: TenantHeader, cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100), session: AsyncSession = Depends(get_session),
) -> SendingDomainList:
    statement = select(SendingDomainModel).where(SendingDomainModel.tenant_id == x_tenant_id)
    if cursor is not None:
        statement = statement.where(SendingDomainModel.id > cursor)
    rows = list((await session.scalars(statement.order_by(SendingDomainModel.id).limit(limit + 1))).all())
    return SendingDomainList(items=[_domain_response(row) for row in rows[:limit]],
                             next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None)


@router.get("/domains/{domain_id}", response_model=SendingDomain,
            dependencies=[Depends(require_scope("communications.domains.read"))])
async def get_domain(domain_id: UUID, x_tenant_id: TenantHeader,
                     session: AsyncSession = Depends(get_session)) -> SendingDomain:
    row = await session.scalar(select(SendingDomainModel).where(
        SendingDomainModel.id == domain_id, SendingDomainModel.tenant_id == x_tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="domain_not_found")
    return _domain_response(row)


def _sender_response(row: SenderIdentityModel) -> SenderIdentity:
    return SenderIdentity(
        sender_identity_id=row.id, channel=Channel(row.channel), address=row.address,
        display_name=row.display_name, domain_id=row.domain_id,
        metadata=json.loads(row.metadata_json), status=row.status,
        resource_version=row.resource_version, created_at=row.created_at, updated_at=row.updated_at,
    )


async def _sender_domain(
    session: AsyncSession, tenant_id: str, body: SenderIdentityWrite,
) -> SendingDomainModel | None:
    address = _recipient(body.address, body.channel)
    if body.channel == Channel.EMAIL:
        parts = address.rsplit("@", 1)
        address_domain = parts[-1] if len(parts) == 2 and 0 < len(parts[0]) <= 64 else ""
        if not address_domain or body.domain_id is None:
            raise HTTPException(status_code=422, detail="email_sender_domain_required")
        domain = await session.scalar(select(SendingDomainModel).where(
            SendingDomainModel.id == body.domain_id,
            SendingDomainModel.tenant_id == tenant_id,
        ))
        if domain is None or domain.domain != _normalize_domain(address_domain):
            raise HTTPException(status_code=409, detail="sender_domain_mismatch")
        return domain
    if body.domain_id is not None:
        raise HTTPException(status_code=422, detail="domain_not_applicable_to_channel")
    return None


@router.post(
    "/sender-identities", response_model=SenderIdentity, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("communications.senders.write"))],
)
async def create_sender_identity(
    body: SenderIdentityWrite, x_tenant_id: TenantHeader, idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session), request: Request = None,
) -> SenderIdentity:
    _require_business_writes()
    if body.expected_version is not None:
        raise HTTPException(status_code=409, detail="sender_not_found_for_expected_version")
    domain = await _sender_domain(session, x_tenant_id, body)
    address = _recipient(body.address, body.channel)
    metadata_json = _bounded_metadata(body.metadata)
    payload = body.model_dump(mode="json") | {"address": address}
    fingerprint = _domain_fingerprint("sender.create", f"{body.channel.value}:{address}", payload)
    replay = await session.scalar(select(SenderIdentityModel).where(
        SenderIdentityModel.tenant_id == x_tenant_id,
        SenderIdentityModel.idempotency_key == idempotency_key,
    ))
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return _sender_response(replay)
    row = SenderIdentityModel(
        tenant_id=x_tenant_id, channel=body.channel.value, address=address,
        display_name=body.display_name, domain_id=body.domain_id, metadata_json=metadata_json,
        status="active" if domain is not None and domain.status in {"verified", "sending_enabled"} else "pending",
        idempotency_key=idempotency_key, request_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        await session.flush()
        _audit_domain(session, tenant_id=x_tenant_id, aggregate_type="sender_identity",
                      aggregate_id=row.id, action="communication.sender.registered", request=request)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        winner = await session.scalar(select(SenderIdentityModel).where(
            SenderIdentityModel.tenant_id == x_tenant_id,
            SenderIdentityModel.idempotency_key == idempotency_key,
        ))
        if winner is None or winner.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="sender_conflict") from exc
        row = winner
    await session.refresh(row)
    return _sender_response(row)


@router.get("/sender-identities", response_model=SenderIdentityList,
            dependencies=[Depends(require_scope("communications.senders.read"))])
async def list_sender_identities(
    x_tenant_id: TenantHeader, cursor: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100), session: AsyncSession = Depends(get_session),
) -> SenderIdentityList:
    statement = select(SenderIdentityModel).where(SenderIdentityModel.tenant_id == x_tenant_id)
    if cursor is not None:
        statement = statement.where(SenderIdentityModel.id > cursor)
    rows = list((await session.scalars(statement.order_by(SenderIdentityModel.id).limit(limit + 1))).all())
    return SenderIdentityList(items=[_sender_response(row) for row in rows[:limit]],
                              next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None)


@router.get("/sender-identities/{sender_identity_id}", response_model=SenderIdentity,
            dependencies=[Depends(require_scope("communications.senders.read"))])
async def get_sender_identity(sender_identity_id: UUID, x_tenant_id: TenantHeader,
                              session: AsyncSession = Depends(get_session)) -> SenderIdentity:
    row = await session.scalar(select(SenderIdentityModel).where(
        SenderIdentityModel.id == sender_identity_id,
        SenderIdentityModel.tenant_id == x_tenant_id))
    if row is None:
        raise HTTPException(status_code=404, detail="sender_identity_not_found")
    return _sender_response(row)


@router.put("/sender-identities/{sender_identity_id}", response_model=SenderIdentity,
            dependencies=[Depends(require_scope("communications.senders.write"))])
async def update_sender_identity(
    sender_identity_id: UUID, body: SenderIdentityWrite, x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader, session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> SenderIdentity:
    _require_business_writes()
    row = await session.scalar(select(SenderIdentityModel).where(
        SenderIdentityModel.id == sender_identity_id,
        SenderIdentityModel.tenant_id == x_tenant_id).with_for_update())
    if row is None:
        raise HTTPException(status_code=404, detail="sender_identity_not_found")
    if body.expected_version is None or row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    domain = await _sender_domain(session, x_tenant_id, body)
    address = _recipient(body.address, body.channel)
    payload = body.model_dump(mode="json") | {"address": address}
    if not await _record_domain_mutation(
        session, tenant_id=x_tenant_id, aggregate_type="sender_identity",
        aggregate_key=str(sender_identity_id), kind="sender.update",
        idempotency_key=idempotency_key, payload=payload, result_version=row.resource_version + 1,
    ):
        return _sender_response(row)
    row.channel = body.channel.value
    row.address = address
    row.display_name = body.display_name
    row.domain_id = body.domain_id
    row.metadata_json = _bounded_metadata(body.metadata)
    row.status = "active" if domain is not None and domain.status in {"verified", "sending_enabled"} else "pending"
    row.resource_version += 1
    _audit_domain(session, tenant_id=x_tenant_id, aggregate_type="sender_identity",
                  aggregate_id=row.id, action="communication.sender.updated", request=request)
    await session.commit()
    await session.refresh(row)
    return _sender_response(row)


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


@router.get(
    "/provider-health",
    response_model=ProviderHealth,
    dependencies=[Depends(require_scope("communications.providers.read"))],
)
def provider_health() -> ProviderHealth:
    status = "disabled" if not EXTERNAL_DELIVERY_ENABLED else "degraded"
    reason = "external_delivery_disabled" if not EXTERNAL_DELIVERY_ENABLED else "runtime_probe_not_configured"
    return ProviderHealth(
        status=status,
        checked_at=datetime.now(timezone.utc),
        providers=[
            ProviderHealthItem(provider="middleware", channel=channel, status=status, reason=reason)
            for channel in Channel
        ],
    )


@router.get(
    "/usage",
    response_model=UsageReport,
    dependencies=[Depends(require_scope("communications.usage.read"))],
)
async def communication_usage(
    x_tenant_id: TenantHeader,
    from_at: datetime = Query(alias="from"),
    to: datetime = Query(alias="to"),
    channel: Channel | None = None,
    session: AsyncSession = Depends(get_session),
) -> UsageReport:
    if from_at.tzinfo is None or to.tzinfo is None or from_at >= to:
        raise HTTPException(status_code=422, detail="usage_window_invalid")
    selected = [channel] if channel is not None else list(Channel)
    totals: list[UsageTotal] = []
    for item in selected:
        rows = await session.execute(
            select(MessageModel.status, func.count()).where(
                MessageModel.tenant_id == x_tenant_id,
                MessageModel.channel == item.value,
                MessageModel.created_at >= from_at,
                MessageModel.created_at < to,
            ).group_by(MessageModel.status)
        )
        counts = {str(state): int(count) for state, count in rows.all()}
        failed = sum(counts.get(state, 0) for state in ("failed", "delivery_failed", "bounced", "complained"))
        totals.append(UsageTotal(
            channel=item,
            accepted=sum(counts.values()) - counts.get("suppressed", 0),
            delivered=counts.get("delivered", 0),
            failed=failed,
            suppressed=counts.get("suppressed", 0),
        ))
    return UsageReport(from_at=from_at, to=to, totals=totals)


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
        "reconciliation_required": {"sent", "delivered", "failed", "bounced", "complained", "cancelled"},
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
        provider=provider,
        provider_event_type=event.event_type,
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
