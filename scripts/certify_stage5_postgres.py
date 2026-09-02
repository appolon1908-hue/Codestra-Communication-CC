#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
UP = [ROOT / "migrations/001_stage4.sql", ROOT / "migrations/002_stage5.sql", ROOT / "migrations/003_message_lifecycle.sql", ROOT / "migrations/004_policy_templates.sql", ROOT / "migrations/005_delivery_control.sql", ROOT / "migrations/006_event_outbox.sql", ROOT / "migrations/007_preferences.sql", ROOT / "migrations/008_sender_domains.sql"]
DOWN = [ROOT / "migrations/008_sender_domains.down.sql", ROOT / "migrations/007_preferences.down.sql", ROOT / "migrations/006_event_outbox.down.sql", ROOT / "migrations/005_delivery_control.down.sql", ROOT / "migrations/004_policy_templates.down.sql", ROOT / "migrations/003_message_lifecycle.down.sql", ROOT / "migrations/002_stage5.down.sql", ROOT / "migrations/001_stage4.down.sql"]
TABLES = (
    "messages", "communication_consents", "communication_suppressions",
    "communication_message_events", "communication_message_mutations", "communication_audit_events",
    "communication_templates", "communication_domain_mutations",
    "communication_operations", "communication_delivery_outbox", "communication_provider_inbox",
    "communication_event_outbox",
    "communication_preferences",
    "communication_sending_domains", "communication_sender_identities",
)


def dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def run_files(conn: asyncpg.Connection, paths: list[Path]) -> None:
    for path in paths:
        await conn.execute(path.read_text(encoding="utf-8"))


async def main() -> None:
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    try:
        await run_files(conn, DOWN)
        await run_files(conn, UP)
        await run_files(conn, UP)
        for table in TABLES:
            assert await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") == table
        await run_files(conn, DOWN)
        for table in TABLES:
            assert await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") is None
        await run_files(conn, UP)
    finally:
        await conn.close()
    print("COMMUNICATION_STAGE5_POSTGRES_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
