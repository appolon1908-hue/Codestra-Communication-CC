#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
UP = [ROOT / "migrations/001_stage4.sql", ROOT / "migrations/002_stage5.sql"]
DOWN = [ROOT / "migrations/002_stage5.down.sql", ROOT / "migrations/001_stage4.down.sql"]
TABLES = ("messages", "communication_consents", "communication_suppressions")


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
        await conn.execute((ROOT / "migrations/001_stage4.down.sql").read_text(encoding="utf-8"))
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
