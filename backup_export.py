#!/usr/bin/env python3
"""Herramienta CLI para exportar respaldos JSON por comercio desde línea de comandos."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db
from app.security import AuthContext
from app.services.reports import export_tenant_backup_json


async def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python scripts/backup_export.py <tenant_id> [archivo_salida.json]")
        sys.exit(1)
    tenant_id = sys.argv[1]
    out_file = (
        Path(sys.argv[2])
        if len(sys.argv) >= 3
        else Path(f"backups/backup_{tenant_id}.json")
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)

    db_mgr = get_db()
    await db_mgr.connect()
    ctx = AuthContext(
        user_id="cli-backup",
        email="backup@local",
        full_name="CLI Backup",
        role="owner",
        tenant_id=tenant_id,
    )
    payload = await export_tenant_backup_json(ctx)
    out_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[Mi Tiendita] Respaldo guardado en: {out_file}")
    await db_mgr.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
