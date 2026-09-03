#!/usr/bin/env python3
"""Certify legacy-data conversion and reversible rollback on an isolated database."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

from backfill_data_protection import main as migrate
from restore_plaintext_for_rollback import main as rollback

ROOT = Path(__file__).resolve().parents[1]
MARKER = "migration-person@example.invalid"


def dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def seed_schema_008(conn: asyncpg.Connection) -> None:
    await conn.execute((ROOT / "migrations/010_data_protection_enforcement.down.sql").read_text())
    await conn.execute((ROOT / "migrations/009_data_protection_columns.down.sql").read_text())
    message_id = await conn.fetchval(
        "INSERT INTO messages (tenant_id,channel,recipient,template_key,idempotency_key,status,purpose,request_fingerprint) "
        "VALUES ('tenant-migration','email',$1,'migration.template','migration-message','queued','transactional',repeat('0',64)) RETURNING id",
        MARKER,
    )
    await conn.execute(
        "INSERT INTO communication_consents (tenant_id,subject_key,channel,status,source,idempotency_key,request_fingerprint) "
        "VALUES ('tenant-migration',$1,'email','granted','migration','migration-consent',repeat('0',64))",
        MARKER,
    )
    await conn.execute(
        "INSERT INTO communication_suppressions (tenant_id,channel,recipient,reason,idempotency_key,request_fingerprint) "
        "VALUES ('tenant-migration','sms',$1,'migration','migration-suppression',repeat('0',64))",
        "+15555550123",
    )
    await conn.execute(
        "INSERT INTO communication_preferences (tenant_id,subject,channel,topic,consent,source,idempotency_key,request_fingerprint) "
        "VALUES ('tenant-migration',$1,'email','','granted','migration','migration-preference',repeat('0',64))",
        MARKER,
    )
    await conn.execute(
        "INSERT INTO communication_templates (tenant_id,key,channel,locale,subject_template,body_template,idempotency_key,request_fingerprint) "
        "VALUES ('tenant-migration','migration.template','email','en','Private subject','Private body','migration-template',repeat('0',64))"
    )
    operation_id = await conn.fetchval(
        "INSERT INTO communication_operations (tenant_id,message_id,kind,state,idempotency_key,correlation_id) "
        "VALUES ('tenant-migration',$1,'deliver','pending','migration-operation','migration-correlation') RETURNING id",
        message_id,
    )
    await conn.execute(
        "INSERT INTO communication_delivery_outbox (tenant_id,operation_id,payload_json) VALUES ('tenant-migration',$1,$2)",
        operation_id, '{"recipient":"migration-person@example.invalid"}',
    )


async def assert_protected(conn: asyncpg.Connection) -> None:
    checks = (
        ("messages", "recipient", "recipient_ciphertext", "recipient_hash"),
        ("communication_consents", "subject_key", "subject_ciphertext", "subject_hash"),
        ("communication_suppressions", "recipient", "recipient_ciphertext", "recipient_hash"),
        ("communication_preferences", "subject", "subject_ciphertext", "subject_hash"),
    )
    for table, clear, cipher, digest in checks:
        row = await conn.fetchrow(
            f'SELECT "{clear}" clear_value, "{cipher}" cipher_value, "{digest}" digest_value FROM "{table}" WHERE tenant_id=$1',
            "tenant-migration",
        )
        assert row["clear_value"] is None
        assert row["cipher_value"].startswith("v1:")
        assert row["digest_value"] and len(row["digest_value"]) == 64
        assert MARKER not in row["cipher_value"]
    template = await conn.fetchrow(
        "SELECT subject_template,body_template,subject_ciphertext,body_ciphertext FROM communication_templates WHERE tenant_id='tenant-migration'"
    )
    assert template["subject_template"] is None and template["body_template"] is None
    assert template["subject_ciphertext"].startswith("v1:")
    assert template["body_ciphertext"].startswith("v1:")
    payload = await conn.fetchval(
        "SELECT payload_json FROM communication_delivery_outbox WHERE tenant_id='tenant-migration'"
    )
    assert payload.startswith("v1:") and MARKER not in payload


async def main() -> None:
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    try:
        await seed_schema_008(conn)
    finally:
        await conn.close()
    await migrate()
    conn = await asyncpg.connect(dsn())
    try:
        await assert_protected(conn)
    finally:
        await conn.close()
    os.environ["COMMUNICATION_CONFIRM_PLAINTEXT_ROLLBACK"] = "YES"
    await rollback()
    conn = await asyncpg.connect(dsn())
    try:
        assert await conn.fetchval(
            "SELECT recipient FROM messages WHERE tenant_id='tenant-migration'"
        ) == MARKER
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='messages' AND column_name='recipient_ciphertext')"
        )
    finally:
        await conn.close()
    await migrate()
    conn = await asyncpg.connect(dsn())
    try:
        async with conn.transaction():
            for table in (
                "communication_consents", "communication_suppressions",
                "communication_preferences", "communication_templates", "messages",
            ):
                await conn.execute(f'DELETE FROM "{table}" WHERE tenant_id=$1', "tenant-migration")
    finally:
        await conn.close()
    print("COMMUNICATION_DATA_PROTECTION_MIGRATION_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
