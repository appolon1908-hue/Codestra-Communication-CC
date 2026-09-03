from __future__ import annotations

import pytest

from app.event_worker import redis_url


def test_event_redis_requires_tls_off_host(monkeypatch):
    monkeypatch.setenv("EVENT_REDIS_URL", "redis://redis.internal:6379/0")
    with pytest.raises(RuntimeError, match="event_redis_url_invalid"):
        redis_url()


def test_event_redis_allows_tls_and_local_test_transport(monkeypatch):
    monkeypatch.setenv("EVENT_REDIS_URL", "rediss://redis.internal:6379/0")
    assert redis_url().startswith("rediss://")
    monkeypatch.setenv("EVENT_REDIS_URL", "redis://127.0.0.1:6379/0")
    assert redis_url().startswith("redis://")
