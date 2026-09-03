from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select

from .db import SessionLocal
from .metrics import EVENT_OUTBOX_DEPTH, EVENT_PUBLICATIONS
from .models import CommunicationEventOutboxModel

UTC = timezone.utc


@dataclass(frozen=True)
class ClaimedEvent:
    id: UUID
    event_id: int
    topic: str
    payload_json: str
    attempts: int


def redis_url() -> str:
    value = os.getenv("EVENT_REDIS_URL", "").strip()
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    allowed = {"redis", "rediss"} if loopback else {"rediss"}
    if not parsed.hostname or parsed.scheme not in allowed:
        raise RuntimeError("event_redis_url_invalid")
    return value


async def claim(batch_size: int, lease_seconds: int, *, session_factory=SessionLocal) -> list[ClaimedEvent]:
    now = datetime.now(UTC)
    async with session_factory() as session:
        rows = list((await session.execute(
            select(CommunicationEventOutboxModel)
            .where(or_(
                and_(CommunicationEventOutboxModel.state == "pending", CommunicationEventOutboxModel.available_at <= now),
                and_(CommunicationEventOutboxModel.state == "publishing", CommunicationEventOutboxModel.lease_until < now),
            ))
            .order_by(CommunicationEventOutboxModel.created_at)
            .limit(batch_size).with_for_update(skip_locked=True)
        )).scalars())
        for row in rows:
            row.state = "publishing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.last_error_code = None
        await session.commit()
        return [ClaimedEvent(r.id, r.event_id, r.topic, r.payload_json, r.attempts) for r in rows]


async def acknowledge(identity: UUID, attempts: int, *, session_factory=SessionLocal) -> bool:
    async with session_factory() as session:
        row = await session.scalar(select(CommunicationEventOutboxModel).where(
            CommunicationEventOutboxModel.id == identity).with_for_update())
        if row is None or row.state != "publishing" or row.attempts != attempts:
            return False
        row.state = "published"
        row.published_at = datetime.now(UTC)
        row.lease_until = None
        await session.commit()
        return True


async def reject(identity: UUID, attempts: int, max_attempts: int, *, session_factory=SessionLocal) -> bool:
    async with session_factory() as session:
        row = await session.scalar(select(CommunicationEventOutboxModel).where(
            CommunicationEventOutboxModel.id == identity).with_for_update())
        if row is None or row.state != "publishing" or row.attempts != attempts:
            return False
        row.state = "dead_letter" if attempts >= max_attempts else "pending"
        row.available_at = datetime.now(UTC) + timedelta(seconds=min(2 ** min(attempts, 8), 300))
        row.lease_until = None
        row.last_error_code = "event_publish_failed"
        await session.commit()
        return True


async def pending_depth(*, session_factory=SessionLocal) -> int:
    async with session_factory() as session:
        value = await session.scalar(select(func.count()).select_from(CommunicationEventOutboxModel).where(
            CommunicationEventOutboxModel.state.in_(("pending", "publishing"))))
        return int(value or 0)


async def run_once(redis: Redis, *, batch_size: int, lease_seconds: int, max_attempts: int,
                   session_factory=SessionLocal) -> int:
    events = await claim(batch_size, lease_seconds, session_factory=session_factory)
    for event in events:
        try:
            await redis.xadd(event.topic, {"event_id": str(event.event_id), "payload": event.payload_json})
        except Exception:
            changed = await reject(event.id, event.attempts, max_attempts, session_factory=session_factory)
            if changed:
                EVENT_PUBLICATIONS.labels(outcome="retry" if event.attempts < max_attempts else "dead_letter").inc()
        else:
            if await acknowledge(event.id, event.attempts, session_factory=session_factory):
                EVENT_PUBLICATIONS.labels(outcome="published").inc()
    EVENT_OUTBOX_DEPTH.set(await pending_depth(session_factory=session_factory))
    return len(events)


async def main() -> None:
    batch_size = max(1, min(int(os.getenv("EVENT_OUTBOX_BATCH_SIZE", "50")), 200))
    lease_seconds = max(5, min(int(os.getenv("EVENT_OUTBOX_LEASE_SECONDS", "30")), 300))
    max_attempts = max(1, min(int(os.getenv("EVENT_OUTBOX_MAX_ATTEMPTS", "8")), 32))
    poll_seconds = max(0.1, min(float(os.getenv("EVENT_OUTBOX_POLL_SECONDS", "1")), 30.0))
    redis = Redis.from_url(redis_url(), decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        while True:
            if await run_once(redis, batch_size=batch_size, lease_seconds=lease_seconds,
                              max_attempts=max_attempts) == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
