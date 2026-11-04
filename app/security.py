import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings, new_id, utc_now
from app.db import DBConnectionWrapper, get_db

_ph = PasswordHasher(time_cost=2, memory_cost=32768, parallelism=2)
_bearer = HTTPBearer(auto_error=False)


class LoginRateLimiter:
    """Protección contra fuerza bruta en autenticación (ventana deslizante en memoria)."""

    def __init__(self, max_attempts: int = 12, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window
        recent = [t for t in self._attempts[key] if t > cutoff]
        self._attempts[key] = recent
        if len(recent) >= self.max_attempts:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiados intentos fallidos. Aguardá unos minutos antes de reintentar.",
            )

    def record_failure(self, key: str) -> None:
        self._attempts[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)


login_limiter = LoginRateLimiter()


def client_ip(request: Request) -> str:
    """
    IP del cliente para límites de intentos y auditoría. Detrás de un proxy (Render) la conexión
    directa es siempre la del proxy; con TRUSTED_PROXY_HOPS=N se toma la N-ésima entrada contando
    desde la derecha de X-Forwarded-For (las de la izquierda las puede inventar el cliente).
    """
    hops = get_settings().trusted_proxy_hops
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"


def hash_password(plain_password: str) -> str:
    return _ph.hash(plain_password)


# Hash de relleno para igualar el tiempo de respuesta del login cuando el correo no existe
# (evita enumerar cuentas midiendo la demora).
_DUMMY_HASH = _ph.hash("relleno-para-igualar-tiempos")


def burn_password_check(plain_password: str) -> None:
    verify_password(plain_password, _DUMMY_HASH)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, plain_password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(
    user_id: str,
    role: str,
    tenant_id: str | None,
    token_version: int = 1,
    expires_minutes: int | None = None,
) -> str:
    settings = get_settings()
    exp_mins = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "tid": tenant_id,
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=exp_mins)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tu sesión venció. Volvé a ingresar.",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales de sesión inválidas.",
        )


class AuthContext:
    def __init__(
        self,
        user_id: str,
        email: str,
        full_name: str,
        role: str,
        tenant_id: str | None,
        tenant_name: str | None = None,
        subscription_status: str | None = None,
        plan_id: str | None = None,
        fiscal_module_enabled: bool = False,
        monthly_analysis_enabled: bool = False,
        ip_address: str = "",
    ) -> None:
        self.user_id = user_id
        self.email = email
        self.full_name = full_name
        self.role = role
        self.tenant_id = tenant_id
        self.tenant_name = tenant_name
        self.subscription_status = subscription_status
        self.plan_id = plan_id
        self.fiscal_module_enabled = fiscal_module_enabled
        self.monthly_analysis_enabled = monthly_analysis_enabled
        self.ip_address = ip_address


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    token: str | None = None
    if creds and creds.credentials:
        token = creds.credentials
    elif "mt_session" in request.cookies:
        token = request.cookies.get("mt_session")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autenticado. Iniciá sesión para continuar.",
        )

    payload = decode_access_token(token)
    user_id = payload.get("sub")
    token_tv = payload.get("tv", 1)
    if not user_id:
        raise HTTPException(status_code=401, detail="Sesión inválida.")

    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        user = await conn.fetchrow(
            """
            SELECT u.id, u.tenant_id, u.email, u.full_name, u.role, u.is_active, u.token_version,
                   t.name AS tenant_name, t.subscription_status, t.plan_id,
                   t.fiscal_module_enabled, t.monthly_analysis_enabled
            FROM users u
            LEFT JOIN tenants t ON t.id = u.tenant_id
            WHERE u.id = $1
            """,
            user_id,
        )

    if not user or not bool(user["is_active"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario inactivo o inexistente.",
        )
    if int(user["token_version"]) != int(token_tv):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="La sesión fue cerrada por seguridad. Volvé a iniciar sesión.",
        )

    return AuthContext(
        user_id=user["id"],
        email=user["email"],
        full_name=user["full_name"],
        role=user["role"],
        tenant_id=user["tenant_id"],
        tenant_name=user["tenant_name"],
        subscription_status=user["subscription_status"],
        plan_id=user["plan_id"],
        fiscal_module_enabled=bool(user["fiscal_module_enabled"]),
        monthly_analysis_enabled=bool(user["monthly_analysis_enabled"]),
        ip_address=client_ip(request),
    )


async def require_tenant_read(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
    """Exige pertenecer a un comercio aislado (bloquea superadmin sin scope comercial)."""
    if not ctx.tenant_id or ctx.role == "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operación pertenece al espacio aislado de un comercio.",
        )
    if ctx.subscription_status == "suspended":
        raise HTTPException(status_code=403, detail="Servicio suspendido por la administración. Contactá a Mi Tiendita; el titular puede exportar sus datos.")
    return ctx


async def require_tenant_export(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
    if not ctx.tenant_id or ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Solo el titular puede exportar los datos de su comercio.")
    return ctx


async def require_tenant_write(ctx: AuthContext = Depends(require_tenant_read)) -> AuthContext:
    """Exige rol operativo (owner o cashier). La suspensión ya la corta require_tenant_read."""
    if ctx.role not in ("owner", "cashier"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tu perfil es de solo lectura y no puede registrar operaciones.",
        )
    return ctx


async def require_tenant_owner(ctx: AuthContext = Depends(require_tenant_write)) -> AuthContext:
    """Exige rol titular del comercio (owner) para acciones sensibles o configuraciones."""
    if ctx.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el titular del comercio está autorizado para esta operación.",
        )
    return ctx


async def require_superadmin(ctx: AuthContext = Depends(get_current_user)) -> AuthContext:
    """Exige cuenta de administración general del producto (Propietaria SaaS)."""
    if ctx.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso exclusivo para la Administración General de Mi Tiendita.",
        )
    return ctx


async def require_saas_owner(ctx: AuthContext = Depends(require_superadmin)) -> AuthContext:
    if ctx.email.strip().lower() != get_settings().superadmin_email:
        raise HTTPException(status_code=403, detail="Esta acción está reservada a la cuenta propietaria configurada del SaaS.")
    return ctx


async def log_audit(
    conn: DBConnectionWrapper,
    *,
    tenant_id: str | None,
    actor_user_id: str | None,
    actor_role: str,
    action: str,
    entity_type: str,
    entity_id: str = "",
    details: dict[str, Any] | None = None,
    ip_address: str = "",
) -> None:
    await conn.execute(
        """
        INSERT INTO audit_logs (
            id, tenant_id, actor_user_id, actor_role, action,
            entity_type, entity_id, ip_address, details_json, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        new_id(),
        tenant_id,
        actor_user_id,
        actor_role,
        action,
        entity_type,
        entity_id,
        ip_address,
        json.dumps(details or {}, ensure_ascii=False, default=str),
        utc_now(),
    )
