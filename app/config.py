import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_file(BASE_DIR / ".env")

TWO_PLACES = Decimal("0.01")
THREE_PLACES = Decimal("0.001")


def money(value: Any) -> Decimal:
    """Convierte cualquier entrada numérica a Decimal exacto de 2 decimales (ROUND_HALF_UP)."""
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if isinstance(value, float):
        return Decimal(f"{value:.6f}").quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    """Convierte cantidades de stock/venta a Decimal exacto de 3 decimales."""
    if value is None or value == "":
        return Decimal("0.000")
    if isinstance(value, Decimal):
        return value.quantize(THREE_PLACES, rounding=ROUND_HALF_UP)
    if isinstance(value, float):
        return Decimal(f"{value:.6f}").quantize(THREE_PLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(THREE_PLACES, rounding=ROUND_HALF_UP)


def money_float(value: Any) -> float:
    """Serializa Decimal monetario a float limpio de 2 decimales para respuestas JSON."""
    return float(money(value))


def qty_float(value: Any) -> float:
    """Serializa Decimal de cantidad a float limpio de hasta 3 decimales para JSON."""
    return float(qty(value))


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Settings:
    def __init__(self) -> None:
        self.app_env: str = os.getenv("APP_ENV", "development")
        self.app_name: str = os.getenv("APP_NAME", "Mi Tiendita")
        self.app_version: str = os.getenv("APP_VERSION", "2.0.0")
        self.database_url: str = os.getenv(
            "DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'mi_tiendita.db'}"
        )
        self.db_pool_min_size: int = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
        self.db_pool_max_size: int = int(os.getenv("DB_POOL_MAX_SIZE", "5"))
        self.jwt_secret_key: str = os.getenv(
            "JWT_SECRET_KEY",
            "dev-only-secret-cambiar-en-produccion-mi-tiendita-2026-seguro-64chars",
        )
        self.jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
        self.access_token_expire_minutes: int = int(
            os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "720")
        )
        self.superadmin_email: str = os.getenv(
            "SUPERADMIN_EMAIL", "propietaria@mitiendita.ar"
        ).strip().lower()
        self.superadmin_password: str = os.getenv(
            "SUPERADMIN_PASSWORD", "MiTienditaAdmin2026!"
        )
        self.superadmin_name: str = os.getenv(
            "SUPERADMIN_NAME", "Administración Mi Tiendita"
        )
        # Cantidad de proxies de confianza delante de la app (Render = 1). Con 0 se usa la IP
        # de la conexión directa. Ver security.client_ip.
        self.trusted_proxy_hops: int = max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "0") or 0))
        self.enable_demo_seed: bool = os.getenv("ENABLE_DEMO_SEED", "false").lower() in (
            "1",
            "true",
            "yes",
        )


def get_settings() -> Settings:
    return Settings()


def validate_production_settings(settings: Settings, backend: str) -> None:
    if settings.app_env != "production":
        return
    if backend != "postgres":
        raise RuntimeError("Producción requiere PostgreSQL persistente; SQLite local no es durable en Render.")
    if len(settings.jwt_secret_key) < 32 or settings.jwt_secret_key.startswith("dev-only") or "CAMBIAR_EN_PRODUCCION" in settings.jwt_secret_key:
        raise RuntimeError("Configurá un JWT_SECRET_KEY aleatorio y privado de al menos 32 caracteres.")
    if not os.getenv("SUPERADMIN_EMAIL", "").strip():
        # La cuenta propietaria del SaaS se recupera por correo: su casilla debe ser elegida
        # explícitamente por la dueña, nunca un valor por defecto de dominio ajeno.
        raise RuntimeError("Configurá SUPERADMIN_EMAIL con una casilla propia (es la cuenta que puede cortar/reactivar comercios).")
    if len(settings.superadmin_password) < 12 or settings.superadmin_password in ("MiTienditaAdmin2026!", "CambiarEstaClaveSegura2026!"):
        raise RuntimeError("Configurá una contraseña de administración segura y única.")
    if settings.enable_demo_seed:
        raise RuntimeError("Los datos de demostración no deben habilitarse en producción.")
