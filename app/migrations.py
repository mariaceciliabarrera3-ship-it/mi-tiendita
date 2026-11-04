import hashlib
from decimal import Decimal
from pathlib import Path

from app.config import BASE_DIR, get_settings, new_id, utc_now, validate_production_settings
from app.db import DatabaseManager, get_db
from app.security import hash_password


_MIGRATION_LOCK_ID = 727274001  # arbitrario pero fijo para toda la aplicación


def _split_sql_statements(sql_script: str, backend: str) -> list[str]:
    """Divide sentencias SQL respetando bloques DO $$ ... $$ en PostgreSQL."""
    if backend == "postgres" and "DO $$" in sql_script:
        return [sql_script.strip()]
    statements: list[str] = []
    current: list[str] = []
    in_quote = False
    i = 0
    n = len(sql_script)
    while i < n:
        ch = sql_script[i]
        if ch == "-" and not in_quote and i + 1 < n and sql_script[i + 1] == "-":
            # Saltar comentario de línea
            while i < n and sql_script[i] != "\n":
                i += 1
            continue
        if ch == "'":
            current.append(ch)
            if in_quote and i + 1 < n and sql_script[i + 1] == "'":
                current.append("'")
                i += 2
                continue
            in_quote = not in_quote
            i += 1
            continue
        if ch == ";" and not in_quote:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


async def run_migrations(db_mgr: DatabaseManager | None = None) -> list[str]:
    """
    Aplica migraciones SQL versionadas e inicializa únicamente:
    1) Planes comerciales SaaS base
    2) Cuenta Superadmin de la propietaria del producto (si no existe)
    JAMÁS precarga productos genéricos en comercios reales.
    """
    mgr = db_mgr or get_db()
    settings = get_settings()
    validate_production_settings(settings, mgr.backend)
    await mgr.connect()

    mig_dir: Path = BASE_DIR / "migrations" / mgr.backend
    sql_files = sorted(mig_dir.glob("*.sql"))
    applied_now: list[str] = []

    async with mgr.transaction() as conn:
        if mgr.backend == "postgres":
            # Un solo ejecutor de migraciones a la vez: en un deploy de Render la instancia nueva
            # arranca mientras la vieja sigue viva, y dos procesos migrando a la vez se pisarían.
            await conn.fetchval("SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_ID)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        else:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

        existing_rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        existing = {r["version"]: r["checksum"] for r in existing_rows}

        for sql_file in sql_files:
            version = sql_file.stem
            content = sql_file.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if version in existing:
                if existing[version] != checksum:
                    raise RuntimeError(f"La migración aplicada {version} fue modificada; crear una migración nueva.")
                continue

            for stmt in _split_sql_statements(content, mgr.backend):
                await conn.execute(stmt)

            await conn.execute(
                "INSERT INTO schema_migrations (version, filename, checksum, applied_at) VALUES ($1, $2, $3, $4)",
                version,
                sql_file.name,
                checksum,
                utc_now(),
            )
            applied_now.append(sql_file.name)

        # Semilla estructural SaaS: Planes de suscripción (sin mercadería precargada)
        plans = [
            ("plan_gratuito", "gratuito_oculto", "Plan gratuito (asignación manual)", "No disponible en el registro público.", Decimal("0.00"), 100, 1, False, False),
            (
                "plan_inicial",
                "inicial",
                "Plan Barrio Inicial",
                "Ideal para kioscos, almacenes y emprendimientos que reemplazan el cuaderno y la calculadora.",
                Decimal("8900.00"),
                500,
                2,
                False,
                False,
            ),
            (
                "plan_crecimiento",
                "crecimiento",
                "Plan Comercio Completo",
                "Incluye multi-cajero, módulo contable/impositivo opcional y hasta 5.000 artículos.",
                Decimal("14900.00"),
                5000,
                5,
                True,
                False,
            ),
            (
                "plan_pro",
                "pro_analisis",
                "Plan Pro + Análisis Mensual",
                "Todo ilimitado + Servicio mensual de diagnóstico comercial preparado por Mi Tiendita.",
                Decimal("24900.00"),
                50000,
                15,
                True,
                True,
            ),
        ]
        now = utc_now()
        for pid, code, name, desc, price, max_p, max_u, inc_fisc, inc_ana in plans:
            exists_plan = await conn.fetchval(
                "SELECT id FROM subscription_plans WHERE id = $1", pid
            )
            if not exists_plan:
                await conn.execute(
                    """
                    INSERT INTO subscription_plans (
                        id, code, name, description, monthly_price_ars,
                        max_products, max_users, includes_fiscal_module,
                        includes_monthly_analysis, is_active, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    pid,
                    code,
                    name,
                    desc,
                    price,
                    max_p,
                    max_u,
                    inc_fisc,
                    inc_ana,
                    True,
                    now,
                )

        # Cuenta de Administración General del Producto (Propietaria)
        sa_exists = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1", settings.superadmin_email
        )
        if not sa_exists:
            await conn.execute(
                """
                INSERT INTO users (
                    id, tenant_id, email, full_name, password_hash,
                    role, is_active, token_version, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                new_id(),
                None,
                settings.superadmin_email,
                settings.superadmin_name,
                hash_password(settings.superadmin_password),
                "superadmin",
                True,
                1,
                now,
            )

    return applied_now
