from __future__ import annotations

import hashlib
import json
import os
import re
import asyncio
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .auth import require_scope
from .models import (
    CommunicationAuditModel,
    ConsentModel,
    DomainMutationModel,
    MessageEventModel,
    MessageModel,
    MessageMutationModel,
    SuppressionModel,
    TemplateModel,
)

app = FastAPI(title="Codestra Communication API", version="0.3.0")
router = APIRouter(prefix="/v1/communications")
EXTERNAL_DELIVERY_ENABLED = os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").lower() == "true"
BUSINESS_WRITES_ENABLED = os.getenv("BUSINESS_WRITES_ENABLED", "false").lower() == "true"
SERVICE = "codestra-communication"


@app.middleware("http")
async def operational_headers(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
    request.state.correlation_id = correlation_id
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(status_code=500, content={"detail": "internal_error", "correlation_id": correlation_id})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Correlation-ID"] = correlation_id
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


@app.get("/health")
def health(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {"status": "ok", "service": SERVICE, "timestamp": datetime.now(timezone.utc).isoformat(), "correlation_id": correlation_id}


@app.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2.0)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "service": SERVICE, "dependencies": {"database": "unavailable"}, "correlation_id": request.state.correlation_id})
    return {"status": "ready", "service": SERVICE, "dependencies": {"database": "ready", "configuration": "ready"}, "correlation_id": request.state.correlation_id}


@app.get("/version")
def version(request: Request = None) -> dict[str, object]:
    correlation_id = getattr(getattr(request, "state", None), "correlation_id", str(uuid4()))
    return {"service": SERVICE, "application_version": app.version, "api_versions": ["v1"], "git_sha": os.getenv("CODESTRA_GIT_SHA", "unknown"), "image_digest": os.getenv("CODESTRA_IMAGE_DIGEST", "unknown"), "build_timestamp": os.getenv("CODESTRA_BUILD_TIMESTAMP", "unknown"), "migration_revision": os.getenv("CODESTRA_MIGRATION_REVISION", "unknown"), "environment": os.getenv("CODESTRA_ENVIRONMENT", "unknown"), "correlation_id": correlation_id}


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
    if not BUSINESS_WRITES_ENABLED:
        raise HTTPException(status_code=423, detail="business_writes_disabled")
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
            )
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

    if EXTERNAL_DELIVERY_ENABLED:
        raise HTTPException(status_code=501, detail="provider_delivery_not_implemented")
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
    if row.status not in {"accepted_delivery_disabled", "queued"}:
        raise HTTPException(status_code=409, detail="message_not_cancellable")
    previous = row.status
    row.status = "cancelled"
    row.cancelled_at = datetime.now(timezone.utc)
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
    await _message_event(
        session,
        row,
        event_type="communication.message.cancelled",
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
    for field in ("subject_template", "body_template", "active"):
        value = getattr(body, field)
        if value is not None:
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
    await session.flush()
    _audit_domain(
        session, tenant_id=x_tenant_id, aggregate_type="consent", aggregate_id=row.id,
        action="communication.consent.granted", request=request,
    )
    await session.commit()
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
    await session.flush()
    _audit_domain(
        session, tenant_id=x_tenant_id, aggregate_type="suppression", aggregate_id=row.id,
        action="communication.suppression.created", request=request,
    )
    await session.commit()
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


app.include_router(router)
