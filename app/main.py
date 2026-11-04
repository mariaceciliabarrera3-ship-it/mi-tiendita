import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings, validate_production_settings
from app.db import DatabaseManager, get_db
from app.migrations import run_migrations
from app.routers import (
    auth,
    ayudante,
    cash,
    catalog,
    customers,
    fiscal,
    reports,
    saas_admin,
    sales,
    suppliers,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger("mi_tiendita")


async def _startup_diagnostics(settings, db_mgr: DatabaseManager) -> None:
    """Avisos de arranque en producción. Nunca frenan el servicio: solo dejan rastro en el log."""
    if settings.app_env != "production":
        return
    if db_mgr.backend == "postgres":
        # RLS es defensa en profundidad solo si el rol de la app NO es superusuario ni BYPASSRLS.
        try:
            async with db_mgr.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            if row and (row["rolsuper"] or row["rolbypassrls"]):
                logger.warning(
                    "SEGURIDAD: el rol de la base tiene SUPERUSER o BYPASSRLS; las políticas RLS "
                    "NO protegen el aislamiento entre comercios (queda solo el filtro por tenant_id "
                    "de la aplicación). Usá un rol de aplicación sin esos privilegios."
                )
        except Exception as exc:  # diagnóstico: nunca debe impedir el arranque
            logger.warning("No se pudo verificar los privilegios del rol de base de datos (%s).", type(exc).__name__)
    if os.getenv("MAIL_MODE", "disabled").lower() != "smtp" or not os.getenv("PUBLIC_BASE_URL", "").startswith("https://"):
        logger.warning(
            "CORREO: MAIL_MODE=smtp y PUBLIC_BASE_URL https son obligatorios; "
            "sin ellos nadie puede confirmar su cuenta ni recuperar la contraseña."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db_mgr = get_db()
    validate_production_settings(settings, db_mgr.backend)
    await db_mgr.connect()
    await run_migrations(db_mgr)
    await _startup_diagnostics(settings, db_mgr)
    if settings.enable_demo_seed:
        from scripts.seed_demo import ensure_demo_data

        await ensure_demo_data(db_mgr)
    try:
        yield
    finally:
        await db_mgr.disconnect()


app = FastAPI(
    title="Mi Tiendita",
    description=(
        "SaaS comercial de nueva generación para pequeños negocios, almacenes, "
        "kioscos y emprendimientos familiares."
    ),
    version="2.0.0",
    lifespan=lifespan,
)




@app.middleware("http")
async def security_headers_middleware(request: Request, call_next) -> Response:
    # Navegador con cookies: rechazar mutaciones desde otro origen (defensa CSRF).
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get("host"):
                return Response("Origen no autorizado", status_code=403)
        elif request.headers.get("sec-fetch-site") == "cross-site":
            return Response("Origen no autorizado", status_code=403)
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    if get_settings().app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/api/"):
        # Datos comerciales y de sesión: que ningún proxy ni navegador los guarde en caché.
        response.headers["Cache-Control"] = "no-store"
    return response


# Healthcheck liviano sin consulta a base de datos (protege cuota de compute en Neon Free Tier)
@app.get("/healthz", tags=["Infraestructura"])
async def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "db_backend": get_db().backend,
    }


# Readiness con verificación efectiva de conexión SQL
@app.get("/readyz", tags=["Infraestructura"])
async def readiness_check() -> dict[str, object]:
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        val = await conn.fetchval("SELECT 1")
    return {"status": "ready", "db_ok": val == 1, "backend": db_mgr.backend}


# Registrar routers de negocio
app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(sales.router)
app.include_router(customers.router)
app.include_router(suppliers.router)
app.include_router(cash.router)
app.include_router(reports.router)
app.include_router(fiscal.router)
app.include_router(ayudante.router)
app.include_router(saas_admin.router)

# Servir PWA estática
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def pwa_manifest() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/", include_in_schema=False)
async def serve_pwa_root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
