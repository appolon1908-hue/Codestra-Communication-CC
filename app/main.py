from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

app = FastAPI(title="Codestra Communication API", version="0.1.0")

EXTERNAL_DELIVERY_ENABLED = False

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

@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "external_delivery_enabled": EXTERNAL_DELIVERY_ENABLED}

@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "email": True,
        "sms": True,
        "whatsapp": True,
        "push": True,
        "consent_enforcement": True,
        "suppression_enforcement": True,
        "external_delivery": EXTERNAL_DELIVERY_ENABLED,
    }

@app.post("/v1/messages", response_model=Message, status_code=status.HTTP_202_ACCEPTED)
def create_message(body: MessageCreate) -> Message:
    if EXTERNAL_DELIVERY_ENABLED:
        raise HTTPException(status_code=501, detail="provider_delivery_not_implemented")
    return Message(id=uuid4(), status="accepted_delivery_disabled", channel=body.channel, template_key=body.template_key)
