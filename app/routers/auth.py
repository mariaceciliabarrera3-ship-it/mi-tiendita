import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import get_settings, money, money_float, new_id, utc_now
from app.db import get_db
from app.schemas import (
    CreateStaffUserRequest,
    LoginRequest,
    RegisterTenantRequest,
    EmailRequest, VerifyRequest, ResetRequest,
    UpdateTenantSettingsRequest,
)
from app.security import (
    AuthContext,
    burn_password_check,
    client_ip,
    create_access_token,
    get_current_user,
    hash_password,
    log_audit,
    login_limiter,
    require_tenant_owner,
    require_tenant_read,
    verify_password,
)

from app.auth_actions import limit, issue, consume

router = APIRouter(prefix="/api/v1/auth", tags=["Autenticación y Comercio"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "mi-comercio"


@router.post("/register")
async def register_tenant(
    data: RegisterTenantRequest, request: Request
) -> dict[str, Any]:
    """
    Registra un nuevo comercio y su usuario titular.
    IMPORTANTE: El inventario comienza 100% vacío (sin productos genéricos precargados).
    """
    ip = client_ip(request)
    await limit("register", ip, 5, 3600)
    db_mgr = get_db()
    now = utc_now()
    tenant_id = new_id()
    user_id = new_id()
    base_slug = _slugify(data.store_name)
    slug = f"{base_slug}-{tenant_id[:6]}"

    async with db_mgr.transaction() as conn:
        dup = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1", data.email
        )
        if dup:
            raise HTTPException(
                status_code=409,
                detail="Ya existe una cuenta registrada con ese correo electrónico.",
            )

        await conn.execute(
            """
            INSERT INTO tenants (
                id, slug, name, business_type, owner_name, phone, email, address,
                currency, plan_id, subscription_status, subscription_expires_at,
                fiscal_module_enabled, monthly_analysis_enabled,
                debt_alert_days, debt_alert_amount, is_demo, created_at, updated_at, timezone
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            """,
            tenant_id,
            slug,
            data.store_name.strip(),
            data.business_type.strip() or "Almacén",
            data.owner_name.strip(),
            data.phone.strip(),
            data.email,
            data.address.strip(),
            data.currency,
            "plan_inicial",
            "trial",
            now + timedelta(days=30),
            False,
            False,
            15,
            money("20000.00"),
            False,
            now,
            now,
            data.timezone,
        )

        await conn.execute(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                role, is_active, token_version, last_login_at, created_at, email_verified
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            user_id,
            tenant_id,
            data.email,
            data.owner_name.strip(),
            hash_password(data.password),
            "owner",
            False,  # pendiente de verificación del correo
            1,
            now,
            now,
            False,
        )

        await log_audit(
            conn,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            actor_role="owner",
            action="tenant.register",
            entity_type="tenant",
            entity_id=tenant_id,
            details={"store_name": data.store_name.strip(), "email": data.email},
            ip_address=ip,
        )

    await issue(user_id, data.email, "verify_email")
    return {"status": "verification_pending", "message": "Revisá tu correo para activar la cuenta."}


@router.post("/login")
async def login(
    data: LoginRequest, request: Request, response: Response
) -> dict[str, Any]:
    email_norm = data.email.strip().lower()
    ip = client_ip(request)
    rate_key = f"{ip}:{email_norm}"
    login_limiter.check(rate_key)

    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        user = await conn.fetchrow(
            """
            SELECT u.*, t.name AS tenant_name, t.subscription_status, t.plan_id,
                   t.fiscal_module_enabled, t.monthly_analysis_enabled
            FROM users u
            LEFT JOIN tenants t ON t.id = u.tenant_id
            WHERE u.email = $1
            """,
            email_norm,
        )

    if not user:
        burn_password_check(data.password)
    if not user or not verify_password(data.password, user["password_hash"]):
        login_limiter.record_failure(rate_key)
        raise HTTPException(
            status_code=401,
            detail="Correo electrónico o contraseña incorrectos.",
        )

    if not bool(user["is_active"]):
        if not bool(user.get("email_verified", True)):
            raise HTTPException(
                status_code=403,
                detail="Todavía no confirmaste tu correo. Abrí el enlace que te enviamos o pedí uno nuevo.",
            )
        raise HTTPException(
            status_code=403,
            detail="Tu usuario se encuentra desactivado. Contactá al titular del comercio.",
        )

    login_limiter.reset(rate_key)
    now = utc_now()
    async with db_mgr.transaction() as conn:
        await conn.execute(
            "UPDATE users SET last_login_at = $1 WHERE id = $2", now, user["id"]
        )
        await log_audit(
            conn,
            tenant_id=user["tenant_id"],
            actor_user_id=user["id"],
            actor_role=user["role"],
            action="auth.login",
            entity_type="user",
            entity_id=user["id"],
            ip_address=ip,
        )

    token = create_access_token(
        user_id=user["id"],
        role=user["role"],
        tenant_id=user["tenant_id"],
        token_version=int(user["token_version"]),
    )
    response.set_cookie(
        key="mt_session",
        value=token,
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        max_age=get_settings().access_token_expire_minutes * 60,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
            "tenant_id": user["tenant_id"],
            "tenant_name": user.get("tenant_name") or "Administración SaaS",
            "subscription_status": user.get("subscription_status"),
            "plan_id": user.get("plan_id"),
            "fiscal_module_enabled": bool(user.get("fiscal_module_enabled", False)),
            "monthly_analysis_enabled": bool(user.get("monthly_analysis_enabled", False)),
        },
    }


@router.post("/logout")
async def logout(response: Response, ctx: AuthContext = Depends(get_current_user)) -> dict[str, str]:
    # Incrementar versión revoca todos los JWT vigentes de este usuario, en todos sus dispositivos.
    async with get_db().transaction() as conn:
        await conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = $1", ctx.user_id)
        await log_audit(conn, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, actor_role=ctx.role,
                        action="auth.logout", entity_type="user", entity_id=ctx.user_id, ip_address=ctx.ip_address)
    response.delete_cookie("mt_session", secure=get_settings().app_env == "production", httponly=True, samesite="lax")
    return {"status": "logged_out"}

@router.post("/verify-email")
async def verify_email(data: VerifyRequest, request: Request):
    await limit("verify", client_ip(request), 30, 3600)
    await consume(data.token, "verify_email")
    return {"status": "verified"}

async def _send_action(data: EmailRequest, request: Request, purpose: str):
    await limit(purpose + "-ip", client_ip(request), 10, 3600)
    email=data.email.strip().lower()
    await limit(purpose + "-email", email, 3, 3600)
    async with get_db().connection() as c:
        user=await c.fetchrow("SELECT id,email,is_active,email_verified FROM users WHERE email=$1",email)
    if user and ((purpose == "verify_email" and not bool(user["email_verified"]) and not bool(user["is_active"])) or (purpose == "reset_password" and bool(user["is_active"]))):
        try:
            await issue(user['id'],user['email'],purpose)
        except HTTPException:
            # Respuesta idéntica incluso si el servidor de correo falla: no enumerar cuentas.
            pass
    return {"status": "requested", "message": "Si la cuenta corresponde, recibirás un correo."}

@router.post("/resend-verification")
async def resend_verification(data: EmailRequest, request: Request):
    return await _send_action(data, request, "verify_email")

@router.post("/forgot-password")
async def forgot_password(data: EmailRequest, request: Request):
    return await _send_action(data, request, "reset_password")

@router.post("/reset-password")
async def reset_password(data: ResetRequest, request: Request):
    await limit("reset-consume", client_ip(request), 30, 3600)
    await consume(data.token, "reset_password", data.password)
    return {"status": "password_changed"}


@router.get("/me")
async def get_me(ctx: AuthContext = Depends(get_current_user)) -> dict[str, Any]:
    tenant_detail = None
    if ctx.tenant_id:
        db_mgr = get_db()
        async with db_mgr.connection() as conn:
            t = await conn.fetchrow(
                """
                SELECT t.*, p.name AS plan_name
                FROM tenants t
                JOIN subscription_plans p ON p.id = t.plan_id
                WHERE t.id = $1
                """,
                ctx.tenant_id,
            )
            if t:
                tenant_detail = {
                    "id": t["id"],
                    "name": t["name"],
                    "business_type": t["business_type"],
                    "currency": t["currency"],
                    "timezone": t["timezone"],
                    "owner_name": t["owner_name"],
                    "phone": t["phone"],
                    "email": t["email"],
                    "address": t["address"],
                    "plan_id": t["plan_id"],
                    "plan_name": t["plan_name"],
                    "subscription_status": t["subscription_status"],
                    "fiscal_module_enabled": bool(t["fiscal_module_enabled"]),
                    "monthly_analysis_enabled": bool(t["monthly_analysis_enabled"]),
                    "debt_alert_days": int(t["debt_alert_days"]),
                    "debt_alert_amount": money_float(t["debt_alert_amount"]),
                    "is_demo": bool(t["is_demo"]),
                }
    return {
        "user": {
            "id": ctx.user_id,
            "email": ctx.email,
            "full_name": ctx.full_name,
            "role": ctx.role,
            "tenant_id": ctx.tenant_id,
            "tenant_name": ctx.tenant_name or "Administración SaaS",
            "subscription_status": ctx.subscription_status,
            "plan_id": ctx.plan_id,
            "fiscal_module_enabled": ctx.fiscal_module_enabled,
            "monthly_analysis_enabled": ctx.monthly_analysis_enabled,
        },
        "tenant": tenant_detail,
    }


@router.get("/demo-accounts")
async def list_demo_accounts() -> dict[str, Any]:
    """
    Devuelve únicamente comercios explícitamente marcados con is_demo = TRUE
    para facilitar la evaluación interactiva del jurado, claramente separados de producción.
    """
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT u.email, u.role, t.name AS store_name, t.business_type
            FROM users u
            JOIN tenants t ON t.id = u.tenant_id
            WHERE t.is_demo = $1 AND u.is_active = $2
            ORDER BY t.name ASC, u.role DESC
            """,
            True,
            True,
        )
    return {"has_demo_data": len(rows) > 0, "demo_accounts": rows}


@router.patch("/tenant-settings")
async def update_tenant_settings(
    data: UpdateTenantSettingsRequest,
    ctx: AuthContext = Depends(require_tenant_owner),
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        curr = await conn.fetchrow(
            "SELECT * FROM tenants WHERE id = $1", ctx.tenant_id
        )
        if not curr:
            raise HTTPException(status_code=404, detail="Comercio no encontrado.")

        if data.currency and data.currency != curr["currency"]:
            for table in ("products", "sales", "purchases", "cash_movements", "customers", "suppliers"):
                if await conn.fetchval(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=$1", ctx.tenant_id):
                    raise HTTPException(409, "No se puede cambiar la moneda con datos comerciales existentes: se necesitan reglas de conversión históricas.")
        if data.timezone is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try: ZoneInfo(data.timezone)
            except (ZoneInfoNotFoundError, ValueError): raise HTTPException(400, "Zona horaria IANA inválida.")
        await conn.execute(
            "UPDATE tenants SET currency=$1, timezone=$2 WHERE id=$3",
            data.currency or curr["currency"], data.timezone or curr["timezone"], ctx.tenant_id,
        )
        await conn.execute(
            """
            UPDATE tenants
            SET name = $1, business_type = $2, phone = $3, address = $4,
                debt_alert_days = $5, debt_alert_amount = $6,
                fiscal_module_enabled = $7, updated_at = $8
            WHERE id = $9
            """,
            data.name.strip() if data.name is not None else curr["name"],
            data.business_type.strip() if data.business_type is not None else curr["business_type"],
            data.phone.strip() if data.phone is not None else curr["phone"],
            data.address.strip() if data.address is not None else curr["address"],
            data.debt_alert_days if data.debt_alert_days is not None else int(curr["debt_alert_days"]),
            money(data.debt_alert_amount) if data.debt_alert_amount is not None else money(curr["debt_alert_amount"]),
            data.fiscal_module_enabled if data.fiscal_module_enabled is not None else bool(curr["fiscal_module_enabled"]),
            now,
            ctx.tenant_id,
        )
    return {"status": "updated"}


@router.get("/staff")
async def list_staff(
    ctx: AuthContext = Depends(require_tenant_read),
) -> list[dict[str, Any]]:
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        rows = await conn.fetch(
            "SELECT id, email, full_name, role, is_active, last_login_at, created_at FROM users WHERE tenant_id = $1 ORDER BY created_at ASC",
            ctx.tenant_id,
        )
    return [
        {
            "id": r["id"],
            "email": r["email"],
            "full_name": r["full_name"],
            "role": r["role"],
            "is_active": bool(r["is_active"]),
            "last_login_at": r["last_login_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.post("/staff")
async def create_staff_user(
    data: CreateStaffUserRequest,
    ctx: AuthContext = Depends(require_tenant_owner),
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    uid = new_id()

    async with db_mgr.transaction() as conn:
        plan_check = await conn.fetchrow(
            """
            SELECT p.max_users,
                   (SELECT COUNT(*) FROM users u WHERE u.tenant_id = $1 AND u.is_active = $2) AS cnt
            FROM tenants t
            JOIN subscription_plans p ON p.id = t.plan_id
            WHERE t.id = $1
            """,
            ctx.tenant_id,
            True,
        )
        if plan_check and int(plan_check["cnt"]) >= int(plan_check["max_users"]):
            raise HTTPException(
                status_code=403,
                detail=f"Alcanzaste el máximo de {plan_check['max_users']} usuarios de tu plan actual.",
            )

        dup = await conn.fetchval("SELECT id FROM users WHERE email = $1", data.email)
        if dup:
            raise HTTPException(
                status_code=409,
                detail="Ese correo ya está registrado por otro usuario.",
            )

        await conn.execute(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                role, is_active, token_version, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            uid,
            ctx.tenant_id,
            data.email,
            data.full_name.strip(),
            hash_password(data.password),
            data.role,
            True,
            1,
            now,
        )
        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="staff.create",
            entity_type="user",
            entity_id=uid,
            details={"email": data.email, "role": data.role},
            ip_address=ctx.ip_address,
        )

    return {"id": uid, "email": data.email, "full_name": data.full_name.strip(), "role": data.role}
