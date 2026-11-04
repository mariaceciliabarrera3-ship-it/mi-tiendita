#!/usr/bin/env python3
"""Ejecutor CLI de migraciones SQL versionadas para Mi Tiendita."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db
from app.migrations import run_migrations


async def main() -> None:
    db_mgr = get_db()
    applied = await run_migrations(db_mgr)
    if applied:
        print(f"[Mi Tiendita] Migraciones aplicadas ({db_mgr.backend}): {', '.join(applied)}")
    else:
        print(f"[Mi Tiendita] Esquema al día ({db_mgr.backend}).")
    await db_mgr.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
