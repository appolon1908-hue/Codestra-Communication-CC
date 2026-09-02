from __future__ import annotations

import hashlib
import json
import os
import re
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import ConsentModel, MessageModel, SuppressionModel
from .telemetry import audit_event, configure_telemetry, install_correlation_middleware

app = FastAPI(title="Codestra Communication API", version="0.3.0")
install_correlation_middleware(app)
TELEMETRY_EXPORT_ENABLED = configure_telemetry(app)
router = APIRouter(prefix="/v1/communications")
EXTERNAL_DELIVERY_ENABLED = os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").lower() == "true"

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
    model_config = {"from_attributes": True}


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
def health() -> dict[str, object]:
    return {"status": "ok", "external_delivery_enabled": EXTERNAL_DELIVERY_ENABLED}


@router.get("/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "email": True,
        "sms": True,
        "whatsapp": True,
        "push": True,
        "consent_enforcement": True,
        "suppression_enforcement": True,
        "external_delivery": EXTERNAL_DELIVERY_ENABLED,
        "correlation_ids": True,
        "telemetry_export": TELEMETRY_EXPORT_ENABLED,
    }


@router.post("/messages", response_model=Message, status_code=status.HTTP_202_ACCEPTED)
async def create_message(
    body: MessageCreate,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> MessageModel:
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
        audit_event("message_replayed", message_id=str(row.id), status=row.status)
        return row

    suppressed = await session.execute(
        select(SuppressionModel).where(
            SuppressionModel.tenant_id == tenant_id,
            SuppressionModel.channel == body.channel.value,
            SuppressionModel.recipient == recipient,
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
    audit_event("message_recorded", message_id=str(row.id), status=row.status, channel=row.channel)

    if EXTERNAL_DELIVERY_ENABLED:
        raise HTTPException(status_code=501, detail="provider_delivery_not_implemented")
    return row


@router.get("/messages/{message_id}", response_model=Message)
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


app.include_router(router)
