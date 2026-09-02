from __future__ import annotations

import json
import uuid
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
    provider: str | None = None,
    provider_event_type: str | None = None,
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
    integration_id = uuid.uuid4()
    status_map = {
        "middleware_accepted": "accepted", "delivery_failed": "failed",
        "reconciliation_required": "indeterminate", "sent": "dispatched",
        "bounced": "failed", "complained": "failed",
    }
    normalized_status = status_map.get(message.status, message.status)
    normalized_previous = status_map.get(event.previous_status, event.previous_status)
    occurred_at = event.occurred_at.isoformat()
    is_provider_event = provider is not None
    event_name = (
        "codestra.communications.message.event.v1"
        if is_provider_event else "codestra.communications.message.status.v1"
    )
    data: dict[str, Any] = {
        "messageId": str(message.id), "tenantId": message.tenant_id,
        "channel": message.channel, "status": normalized_status,
        "correlationId": event.correlation_id, "occurredAt": occurred_at,
    }
    if is_provider_event:
        data.update({"eventId": str(integration_id), "provider": provider,
                     "providerEventType": provider_event_type})
    elif normalized_previous is not None:
        data["previousStatus"] = normalized_previous
    payload: dict[str, Any] = {
        "specversion": "1.0",
        "id": str(integration_id),
        "type": event_name,
        "source": "urn:codestra:communication",
        "subject": f"messages/{message.id}",
        "time": occurred_at,
        "datacontenttype": "application/json",
        "data": data,
    }
    session.add(
        CommunicationEventOutboxModel(
            event_id=event.id,
            topic=event_name,
            payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
    )
    return event
