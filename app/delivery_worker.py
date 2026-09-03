from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select

from .data_protection import DataProtectionError, reveal
from .db import SessionLocal
from .middleware_client import MiddlewareCommunicationClient, MiddlewareDeliveryError, MiddlewareResult
from .events import record_message_event
from .models import (
    CommunicationAuditModel,
    CommunicationOperationModel,
    DeliveryOutboxModel,
    MessageModel,
)


UTC = timezone.utc


@dataclass(frozen=True)
class Claim:
    outbox_id: UUID
    operation_id: UUID
    attempts: int
    payload: dict[str, object]


def _payload(row: DeliveryOutboxModel) -> dict[str, object]:
    cleartext = reveal(
        ciphertext=row.payload_json if row.payload_json.startswith("v1:") else None,
        legacy_plaintext=None if row.payload_json.startswith("v1:") else row.payload_json,
        tenant_id=row.tenant_id,
        purpose="delivery-payload",
    )
    try:
        decoded = json.loads(cleartext)
    except (TypeError, ValueError) as exc:
        raise DataProtectionError("protected_payload_invalid") from exc
    if not isinstance(decoded, dict):
        raise DataProtectionError("protected_payload_invalid")
    return decoded


def capability_enabled() -> bool:
    return os.getenv("EXTERNAL_DELIVERY_ENABLED", "false").strip().lower() == "true"


async def claim_one(lease_seconds: int, *, session_factory=SessionLocal) -> Claim | None:
    if not capability_enabled():
        return None
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await session.scalar(
            select(DeliveryOutboxModel)
            .where(
                or_(
                    and_(DeliveryOutboxModel.state == "pending", DeliveryOutboxModel.available_at <= now),
                    and_(DeliveryOutboxModel.state == "processing", DeliveryOutboxModel.lease_until < now),
                )
            )
            .order_by(DeliveryOutboxModel.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.state = "processing"
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=lease_seconds)
        operation = await session.get(CommunicationOperationModel, row.operation_id, with_for_update=True)
        if operation is None:
            row.state = "dead_letter"
            row.last_error_code = "operation_missing"
            await session.commit()
            return None
        try:
            payload = _payload(row)
        except DataProtectionError:
            row.state = "dead_letter"
            row.lease_until = None
            row.last_error_code = "data_protection_unavailable"
            operation.state = "failed"
            operation.error_code = "data_protection_unavailable"
            message = await session.scalar(
                select(MessageModel)
                .where(
                    MessageModel.id == operation.message_id,
                    MessageModel.tenant_id == operation.tenant_id,
                )
                .with_for_update()
            )
            if message is not None:
                previous = message.status
                message.status = "delivery_failed"
                if previous != message.status:
                    message.resource_version += 1
                event_type = "communication.message.delivery_failed"
                await record_message_event(
                    session, message, event_type=event_type, previous_status=previous,
                    actor_id="communication-delivery-worker",
                    correlation_id=operation.correlation_id,
                    safe_detail="data_protection_unavailable",
                )
                session.add(CommunicationAuditModel(
                    tenant_id=message.tenant_id, aggregate_type="message",
                    aggregate_id=message.id, action=event_type, outcome="failed",
                    actor_id="communication-delivery-worker",
                    correlation_id=operation.correlation_id,
                ))
            await session.commit()
            return None
        operation.attempts = row.attempts
        await session.commit()
        return Claim(row.id, row.operation_id, row.attempts, payload)


async def complete(
    claim: Claim, result: MiddlewareResult, *, session_factory=SessionLocal
) -> None:
    async with session_factory() as session:
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(DeliveryOutboxModel.id == claim.outbox_id).with_for_update()
        )
        operation = await session.scalar(
            select(CommunicationOperationModel)
            .where(CommunicationOperationModel.id == claim.operation_id)
            .with_for_update()
        )
        if (
            outbox is None or operation is None or outbox.state != "processing"
            or outbox.attempts != claim.attempts
        ):
            return
        message = await session.scalar(
            select(MessageModel)
            .where(MessageModel.id == operation.message_id, MessageModel.tenant_id == operation.tenant_id)
            .with_for_update()
        )
        if message is None:
            return
        previous = message.status
        operation.middleware_operation_id = result.operation_id
        operation.state = "accepted"
        outbox.state = "completed"
        outbox.completed_at = datetime.now(UTC)
        outbox.lease_until = None
        if operation.kind == "deliver" and message.status == "queued":
            message.status = "middleware_accepted"
        elif operation.kind == "cancel" and result.state.lower() in {"cancelled", "canceled"}:
            message.status = "cancelled"
            message.cancelled_at = datetime.now(UTC)
        elif operation.kind == "cancel":
            message.status = "reconciliation_required"
        elif operation.kind == "reconcile":
            try:
                target_id = UUID(str(claim.payload["target_operation_id"]))
            except (KeyError, TypeError, ValueError):
                operation.state = "failed"
                operation.error_code = "reconciliation_payload_invalid"
                message.status = "reconciliation_required"
            else:
                target = await session.scalar(
                    select(CommunicationOperationModel)
                    .where(
                        CommunicationOperationModel.id == target_id,
                        CommunicationOperationModel.tenant_id == operation.tenant_id,
                        CommunicationOperationModel.message_id == message.id,
                    )
                    .with_for_update()
                )
                state = result.state.lower()
                target_kind = str(claim.payload.get("target_kind", ""))
                if target is None or target.kind != target_kind:
                    operation.state = "failed"
                    operation.error_code = "reconciliation_target_invalid"
                    message.status = "reconciliation_required"
                elif target_kind == "cancel" and state in {"cancelled", "canceled"}:
                    target.state = "accepted"
                    operation.state = "accepted"
                    message.status = "cancelled"
                    message.cancelled_at = datetime.now(UTC)
                elif target_kind == "deliver" and state in {
                    "accepted", "completed", "succeeded", "sent", "delivered"
                }:
                    target.state = "accepted"
                    operation.state = "accepted"
                    message.status = "middleware_accepted" if state == "accepted" else state
                elif state in {"failed", "rejected", "dead_letter"}:
                    target.state = "failed"
                    operation.state = "accepted"
                    message.status = "delivery_failed"
                else:
                    target.state = "reconciliation_required"
                    operation.state = "reconciliation_required"
                    message.status = "reconciliation_required"
        if message.status != previous:
            message.resource_version += 1
        if operation.kind == "cancel" and message.status == "cancelled":
            event_type = "communication.message.cancelled"
        elif operation.kind == "cancel":
            event_type = "communication.message.cancellation_reconciliation_required"
        elif operation.kind == "reconcile":
            event_type = f"communication.message.reconciliation_{message.status}"
        else:
            event_type = "communication.message.deliver.middleware_accepted"
        await record_message_event(
            session, message, event_type=event_type, previous_status=previous,
            actor_id="communication-delivery-worker", correlation_id=operation.correlation_id,
            safe_detail=result.state[:32],
        )
        await session.commit()


async def fail(
    claim: Claim, error: MiddlewareDeliveryError, max_attempts: int, *, session_factory=SessionLocal
) -> None:
    async with session_factory() as session:
        outbox = await session.scalar(
            select(DeliveryOutboxModel).where(DeliveryOutboxModel.id == claim.outbox_id).with_for_update()
        )
        operation = await session.scalar(
            select(CommunicationOperationModel)
            .where(CommunicationOperationModel.id == claim.operation_id)
            .with_for_update()
        )
        if (
            outbox is None or operation is None or outbox.state != "processing"
            or outbox.attempts != claim.attempts
        ):
            return
        terminal = error.outcome_unknown or not error.retryable or claim.attempts >= max_attempts
        outbox.state = (
            "reconciliation_required"
            if error.outcome_unknown
            else "dead_letter" if terminal else "pending"
        )
        outbox.available_at = datetime.now(UTC) + timedelta(seconds=min(2 ** min(claim.attempts, 8), 300))
        outbox.lease_until = None
        outbox.last_error_code = error.code[:80]
        if terminal:
            operation.state = "reconciliation_required" if error.outcome_unknown else "failed"
            operation.error_code = error.code[:80]
            message = await session.scalar(
                select(MessageModel)
                .where(
                    MessageModel.id == operation.message_id,
                    MessageModel.tenant_id == operation.tenant_id,
                )
                .with_for_update()
            )
            if message is not None:
                previous = message.status
                message.status = (
                    "reconciliation_required" if error.outcome_unknown else "delivery_failed"
                )
                message.resource_version += 1
                event_type = f"communication.message.{message.status}"
                await record_message_event(
                    session, message, event_type=event_type, previous_status=previous,
                    actor_id="communication-delivery-worker", correlation_id=operation.correlation_id,
                    safe_detail=error.code[:80],
                )
                session.add(
                    CommunicationAuditModel(
                        tenant_id=message.tenant_id,
                        aggregate_type="message",
                        aggregate_id=message.id,
                        action=event_type,
                        outcome="reconciliation_required" if error.outcome_unknown else "failed",
                        actor_id="communication-delivery-worker",
                        correlation_id=operation.correlation_id,
                    )
                )
        await session.commit()


async def run_once(
    client: MiddlewareCommunicationClient, *, lease_seconds: int, max_attempts: int,
    session_factory=SessionLocal,
) -> bool:
    claim = await claim_one(lease_seconds, session_factory=session_factory)
    if claim is None:
        return False
    try:
        dispatch_payload = claim.payload
        if claim.payload.get("action") == "reconcile" and not claim.payload.get("middleware_operation_id"):
            try:
                target_id = UUID(str(claim.payload["target_operation_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise MiddlewareDeliveryError(
                    "reconciliation_target_invalid", retryable=False
                ) from exc
            async with session_factory() as session:
                original = await session.scalar(
                    select(DeliveryOutboxModel).where(
                        DeliveryOutboxModel.operation_id == target_id,
                        DeliveryOutboxModel.tenant_id == str(claim.payload.get("tenant_id", "")),
                    )
                )
            if original is None:
                raise MiddlewareDeliveryError("reconciliation_source_missing", retryable=False)
            # Replay the exact original command with its original operation UUID as
            # the Middleware Idempotency-Key. Middleware returns the pre-existing
            # operation when it accepted the ambiguous request, without another effect.
            try:
                dispatch_payload = _payload(original)
            except DataProtectionError as exc:
                raise MiddlewareDeliveryError(
                    "data_protection_unavailable", retryable=False
                ) from exc
        result = await client.dispatch(dispatch_payload)
    except MiddlewareDeliveryError as exc:
        await fail(claim, exc, max_attempts, session_factory=session_factory)
    else:
        await complete(claim, result, session_factory=session_factory)
    return True


async def main() -> None:
    client = MiddlewareCommunicationClient()
    lease = max(5, min(int(os.getenv("DELIVERY_OUTBOX_LEASE_SECONDS", "30")), 300))
    attempts = max(1, min(int(os.getenv("DELIVERY_OUTBOX_MAX_ATTEMPTS", "8")), 32))
    poll = max(0.1, min(float(os.getenv("DELIVERY_OUTBOX_POLL_SECONDS", "1")), 30.0))
    while True:
        if not await run_once(client, lease_seconds=lease, max_attempts=attempts):
            await asyncio.sleep(poll)


if __name__ == "__main__":
    asyncio.run(main())
