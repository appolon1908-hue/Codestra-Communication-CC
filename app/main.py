import os
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import MessageModel, SuppressionModel

app = FastAPI(title="Codestra Communication API", version="0.2.0")
EXTERNAL_DELIVERY_ENABLED = os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").lower() == "true"

class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"

class MessageCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    channel: Channel
    recipient: str = Field(min_length=1, max_length=512)
    template_key: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=200)

class Message(BaseModel):
    id: UUID
    status: str
    channel: Channel
    template_key: str
    model_config = {"from_attributes": True}

@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "external_delivery_enabled": EXTERNAL_DELIVERY_ENABLED}

@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {"email": True, "sms": True, "whatsapp": True, "push": True, "consent_enforcement": True, "suppression_enforcement": True, "external_delivery": EXTERNAL_DELIVERY_ENABLED}

@app.post("/v1/messages", response_model=Message, status_code=status.HTTP_202_ACCEPTED)
async def create_message(body: MessageCreate, session: AsyncSession = Depends(get_session)) -> MessageModel:
    existing = await session.execute(select(MessageModel).where(MessageModel.tenant_id == body.tenant_id, MessageModel.idempotency_key == body.idempotency_key))
    row = existing.scalar_one_or_none()
    if row is not None:
        return row
    suppressed = await session.execute(select(SuppressionModel).where(SuppressionModel.tenant_id == body.tenant_id, SuppressionModel.channel == body.channel.value, SuppressionModel.recipient == body.recipient))
    if suppressed.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="recipient_suppressed")
    row = MessageModel(**body.model_dump(mode="json"), status="accepted_delivery_disabled" if not EXTERNAL_DELIVERY_ENABLED else "queued")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    if EXTERNAL_DELIVERY_ENABLED:
        raise HTTPException(status_code=501, detail="provider_delivery_not_implemented")
    return row
