#!/usr/bin/env python3
"""One-shot, idempotent plaintext-to-ciphertext migration. Never logs values."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

from app.data_protection import blind_index, protect, unprotect


ROOT = Path(__file__).resolve().parents[1]


def dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def _protected_rows(conn, table: str, clear: str, cipher: str, digest: str, purpose: str) -> int:
    rows = await conn.fetch(
        f'SELECT id, tenant_id, "{clear}" AS clear_value, "{cipher}" AS cipher_value '
        f'FROM "{table}" WHERE "{clear}" IS NOT NULL FOR UPDATE'
    )
    for row in rows:
        value = row["clear_value"]
        if row["cipher_value"] is not None:
            if unprotect(row["cipher_value"], tenant_id=row["tenant_id"], purpose=purpose) != value:
                raise RuntimeError(f"{table}_protected_value_mismatch")
            envelope = row["cipher_value"]
        else:
            envelope = protect(value, tenant_id=row["tenant_id"], purpose=purpose)
        value_hash = blind_index(value, tenant_id=row["tenant_id"], purpose=purpose)
        await conn.execute(
            f'UPDATE "{table}" SET "{clear}" = NULL, "{cipher}" = $1, "{digest}" = $2 WHERE id = $3',
            envelope, value_hash, row["id"],
        )
    return len(rows)


async def main() -> None:
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    counts: dict[str, int] = {}
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(6510965169)")
            # Schema expansion, data conversion, and final enforcement are one
            # atomic migration. No protected-only/legacy split state is ever
            # committed, and ACCESS EXCLUSIVE locks prevent concurrent writers.
            await conn.execute((ROOT / "migrations/009_data_protection_columns.sql").read_text())
            await conn.execute(
                "LOCK TABLE messages, communication_consents, communication_suppressions, "
                "communication_preferences, communication_templates, "
                "communication_delivery_outbox IN ACCESS EXCLUSIVE MODE"
            )
            counts["messages"] = await _protected_rows(
                conn, "messages", "recipient", "recipient_ciphertext", "recipient_hash", "message-recipient"
            )
            counts["consents"] = await _protected_rows(
                conn, "communication_consents", "subject_key", "subject_ciphertext", "subject_hash", "consent-subject"
            )
            counts["suppressions"] = await _protected_rows(
                conn, "communication_suppressions", "recipient", "recipient_ciphertext", "recipient_hash", "suppression-recipient"
            )
            counts["preferences"] = await _protected_rows(
                conn, "communication_preferences", "subject", "subject_ciphertext", "subject_hash", "preference-subject"
            )
            templates = await conn.fetch(
                "SELECT id, tenant_id, subject_template, body_template, subject_ciphertext, body_ciphertext "
                "FROM communication_templates WHERE subject_template IS NOT NULL OR body_template IS NOT NULL FOR UPDATE"
            )
            for row in templates:
                subject_ciphertext = row["subject_ciphertext"]
                if row["subject_template"] is not None:
                    if subject_ciphertext is not None and unprotect(
                        subject_ciphertext, tenant_id=row["tenant_id"], purpose="template-subject"
                    ) != row["subject_template"]:
                        raise RuntimeError("communication_templates_subject_mismatch")
                    subject_ciphertext = subject_ciphertext or protect(
                        row["subject_template"], tenant_id=row["tenant_id"], purpose="template-subject"
                    )
                body_ciphertext = row["body_ciphertext"]
                if row["body_template"] is not None:
                    if body_ciphertext is not None and unprotect(
                        body_ciphertext, tenant_id=row["tenant_id"], purpose="template-body"
                    ) != row["body_template"]:
                        raise RuntimeError("communication_templates_body_mismatch")
                    body_ciphertext = body_ciphertext or protect(
                        row["body_template"], tenant_id=row["tenant_id"], purpose="template-body"
                    )
                await conn.execute(
                    "UPDATE communication_templates SET subject_template=NULL, body_template=NULL, "
                    "subject_ciphertext=$1, body_ciphertext=$2 WHERE id=$3",
                    subject_ciphertext, body_ciphertext, row["id"],
                )
            counts["templates"] = len(templates)
            outbox = await conn.fetch(
                "SELECT id, tenant_id, payload_json FROM communication_delivery_outbox "
                "WHERE payload_json NOT LIKE 'v1:%' FOR UPDATE"
            )
            for row in outbox:
                await conn.execute(
                    "UPDATE communication_delivery_outbox SET payload_json=$1 WHERE id=$2",
                    protect(row["payload_json"], tenant_id=row["tenant_id"], purpose="delivery-payload"),
                    row["id"],
                )
            counts["delivery_outbox"] = len(outbox)
            await conn.execute((ROOT / "migrations/010_data_protection_enforcement.sql").read_text())
    finally:
        await conn.close()
    print("COMMUNICATION_DATA_PROTECTION_BACKFILL=PASS " + " ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    asyncio.run(main())
