#!/usr/bin/env python3
"""Explicit pre-009 rollback preparation; writes plaintext only for rollback."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

from app.data_protection import unprotect


ROOT = Path(__file__).resolve().parents[1]


def dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def main() -> None:
    if os.getenv("COMMUNICATION_CONFIRM_PLAINTEXT_ROLLBACK") != "YES":
        raise SystemExit("COMMUNICATION_CONFIRM_PLAINTEXT_ROLLBACK=YES is required")
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(6510965169)")
            await conn.execute(
                (ROOT / "migrations/010_data_protection_enforcement.down.sql").read_text()
            )
            await conn.execute(
                "LOCK TABLE messages, communication_consents, communication_suppressions, "
                "communication_preferences, communication_templates, "
                "communication_delivery_outbox IN ACCESS EXCLUSIVE MODE"
            )
            specs = (
                ("messages", "recipient", "recipient_ciphertext", "message-recipient"),
                ("communication_consents", "subject_key", "subject_ciphertext", "consent-subject"),
                ("communication_suppressions", "recipient", "recipient_ciphertext", "suppression-recipient"),
                ("communication_preferences", "subject", "subject_ciphertext", "preference-subject"),
            )
            for table, clear, cipher, purpose in specs:
                rows = await conn.fetch(
                    f'SELECT id, tenant_id, "{cipher}" AS cipher_value FROM "{table}" '
                    f'WHERE "{clear}" IS NULL FOR UPDATE'
                )
                for row in rows:
                    value = unprotect(row["cipher_value"], tenant_id=row["tenant_id"], purpose=purpose)
                    await conn.execute(f'UPDATE "{table}" SET "{clear}"=$1 WHERE id=$2', value, row["id"])
            templates = await conn.fetch(
                "SELECT id, tenant_id, subject_ciphertext, body_ciphertext FROM communication_templates "
                "WHERE body_template IS NULL FOR UPDATE"
            )
            for row in templates:
                subject = (
                    unprotect(row["subject_ciphertext"], tenant_id=row["tenant_id"], purpose="template-subject")
                    if row["subject_ciphertext"] is not None else None
                )
                body = unprotect(row["body_ciphertext"], tenant_id=row["tenant_id"], purpose="template-body")
                await conn.execute(
                    "UPDATE communication_templates SET subject_template=$1, body_template=$2 WHERE id=$3",
                    subject, body, row["id"],
                )
            outbox = await conn.fetch(
                "SELECT id, tenant_id, payload_json FROM communication_delivery_outbox "
                "WHERE payload_json LIKE 'v1:%' FOR UPDATE"
            )
            for row in outbox:
                clear = unprotect(row["payload_json"], tenant_id=row["tenant_id"], purpose="delivery-payload")
                await conn.execute(
                    "UPDATE communication_delivery_outbox SET payload_json=$1 WHERE id=$2", clear, row["id"]
                )
            await conn.execute(
                (ROOT / "migrations/009_data_protection_columns.down.sql").read_text()
            )
    finally:
        await conn.close()
    print("COMMUNICATION_PLAINTEXT_ROLLBACK_PREPARATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
