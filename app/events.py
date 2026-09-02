from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import CommunicationEventOutboxModel, MessageEventModel, MessageModel


async def record_message_event(
    session: AsyncSession,
    message: MessageModel,
    *,
    event_type: str,
    previous_status: str | None,
    actor_id: str,
    correlation_id: str,
    safe_detail: str | None = None,
) -> MessageEventModel:
    """Persist an event and its publish intent in the caller's transaction.

    The integration event deliberately contains identifiers and state only. Recipient,
    template content, provider payloads, and credentials never enter the event stream.
    """
    event = MessageEventModel(
        tenant_id=message.tenant_id,
        message_id=message.id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=message.status,
        actor_id=actor_id[:160],
        correlation_id=correlation_id[:128],
        safe_detail=safe_detail[:240] if safe_detail else None,
    )
    session.add(event)
    await session.flush()
    payload: dict[str, Any] = {
        "specversion": "1.0",
        "id": str(event.id),
        "type": event.event_type,
        "source": "urn:codestra:communication",
        "subject": f"messages/{message.id}",
        "tenant_id": message.tenant_id,
        "message_id": str(message.id),
        "previous_status": event.previous_status,
        "status": event.new_status,
        "correlation_id": event.correlation_id,
    }
    session.add(
        CommunicationEventOutboxModel(
            event_id=event.id,
            topic="codestra.communication.events.v1",
            payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
    )
    return event
