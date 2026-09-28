import os
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

app = FastAPI(
    title="Mi Tiendita",
    description="Asistente de IA para comercios sobre WhatsApp",
    version="1.0.0"
)

# Render provee DATABASE_URL con "postgresql://", pero asyncpg requiere "postgresql+asyncpg://"
raw_db_url = os.getenv("DATABASE_URL", "")
if raw_db_url.startswith("postgresql://"):
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = raw_db_url

@app.on_event("startup")
async def startup_db():
    if DATABASE_URL:
        # Crear motor de conexión asíncrono
        engine = create_async_engine(DATABASE_URL, echo=True)
        async with engine.begin() as conn:
            # Crear la tabla de usuarios si no existe
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(50) UNIQUE NOT NULL,
                    name VARCHAR(100),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))
        await engine.dispose()

@app.get("/healthz")
async def health_check():
    return {"status": "ok", "app": "Mi Tiendita", "db_configured": bool(DATABASE_URL)}
