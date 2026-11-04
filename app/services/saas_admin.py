import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.config import money, money_float, new_id, utc_now
from app.db import get_db
from app.schemas import (
    AdminMonthlyReportRequest,
    AdminSubscriptionPaymentRequest,
    AdminTenantUpdateRequest,
)
from app.security import AuthContext, log_audit
from app.services.reports import compute_tenant_monthly_analytics


async def get_saas_overview(ctx: AuthContext) -> dict[str, Any]:
    """
    Panel de Administración General del Producto (exclusivo Propietaria SaaS):
    - Administra comercios, planes, estados y cobros de suscripciones mensuales.
    - Los ingresos por suscripción del software están 100% separados de las cajas de los comercios.
    - No expone listados cruzados de clientes ni tickets privados de los comercios.
    """
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        plans = await conn.fetch(
            "SELECT * FROM subscription_plans ORDER BY monthly_price_ars ASC"
        )
        tenants = await conn.fetch(
            """
            SELECT t.*, p.name AS plan_name, p.monthly_price_ars,
                   (SELECT COUNT(*) FROM users u WHERE u.tenant_id = t.id AND u.is_active = $1) AS users_count
            FROM tenants t
            JOIN subscription_plans p ON p.id = t.plan_id
            ORDER BY t.created_at DESC
            """,
            True,
        )
        payments = await conn.fetch(
            """
            SELECT sp.*, t.name AS tenant_name, p.name AS plan_name
            FROM subscription_payments sp
            JOIN tenants t ON t.id = sp.tenant_id
            JOIN subscription_plans p ON p.id = sp.plan_id
            ORDER BY sp.created_at DESC
            LIMIT 100
            """
        )

    active_real_tenants = [
        t
        for t in tenants
        if t["subscription_status"] in ("active", "trial") and not bool(t["is_demo"])
    ]
    estimated_mrr = sum(
        money_float(t["monthly_price_ars"]) for t in active_real_tenants
    )
    total_collected_saas = sum(money_float(pay["amount"]) for pay in payments)

    return {
        "kpis": {
            "total_tenants": len(tenants),
            "active_tenants": sum(
                1 for t in tenants if t["subscription_status"] == "active"
            ),
            "suspended_tenants": sum(
                1 for t in tenants if t["subscription_status"] == "suspended"
            ),
            "monthly_analysis_subscribers": sum(
                1 for t in tenants if bool(t["monthly_analysis_enabled"])
            ),
            "estimated_mrr_ars": round(estimated_mrr, 2),
            "total_saas_revenue_collected_ars": round(total_collected_saas, 2),
        },
        "plans": [
            {
                "id": p["id"],
                "code": p["code"],
                "name": p["name"],
                "description": p["description"],
                "monthly_price_ars": money_float(p["monthly_price_ars"]),
                "max_products": int(p["max_products"]),
                "max_users": int(p["max_users"]),
                "includes_fiscal_module": bool(p["includes_fiscal_module"]),
                "includes_monthly_analysis": bool(p["includes_monthly_analysis"]),
            }
            for p in plans
        ],
        "tenants": [
            {
                "id": t["id"],
                "slug": t["slug"],
                "name": t["name"],
                "business_type": t["business_type"],
                "owner_name": t["owner_name"],
                "email": t["email"],
                "phone": t["phone"],
                "plan_id": t["plan_id"],
                "plan_name": t["plan_name"],
                "monthly_price_ars": money_float(t["monthly_price_ars"]),
                "subscription_status": t["subscription_status"],
                "subscription_expires_at": t["subscription_expires_at"],
                "fiscal_module_enabled": bool(t["fiscal_module_enabled"]),
                "monthly_analysis_enabled": bool(t["monthly_analysis_enabled"]),
                "is_demo": bool(t["is_demo"]),
                "users_count": int(t["users_count"]),
                "created_at": t["created_at"],
            }
            for t in tenants
        ],
        "recent_subscription_payments": [
            {
                "id": pay["id"],
                "tenant_id": pay["tenant_id"],
                "tenant_name": pay["tenant_name"],
                "plan_name": pay["plan_name"],
                "amount": money_float(pay["amount"]),
                "includes_analysis_addon": bool(pay["includes_analysis_addon"]),
                "period_start": pay["period_start"],
                "period_end": pay["period_end"],
                "payment_method": pay["payment_method"],
                "reference": pay["reference"],
                "created_at": pay["created_at"],
            }
            for pay in payments
        ],
    }


async def update_tenant_subscription(
    ctx: AuthContext, tenant_id: str, data: AdminTenantUpdateRequest
) -> dict[str, Any]:
    db_mgr = get_db()
    now = utc_now()

    async with db_mgr.transaction(tenant_id=tenant_id) as conn:
        t = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="Comercio no encontrado.")

        new_plan = data.plan_id if data.plan_id is not None else t["plan_id"]
        plan_row = await conn.fetchrow(
            "SELECT * FROM subscription_plans WHERE id = $1", new_plan
        )
        if not plan_row:
            raise HTTPException(status_code=400, detail="Plan inexistente.")

        new_status = (
            data.subscription_status
            if data.subscription_status is not None
            else t["subscription_status"]
        )
        if data.subscription_status is not None and data.subscription_status != t["subscription_status"] and (data.subscription_status == "suspended" or t["subscription_status"] == "suspended"):
            raise HTTPException(status_code=400, detail="Usá la acción explícita de cortar o reactivar servicio, con confirmación.")
        new_exp = (
            data.subscription_expires_at
            if data.subscription_expires_at is not None
            else t["subscription_expires_at"]
        )
        if conn.backend == "postgres" and isinstance(new_exp, str) and new_exp:
            try:
                new_exp = datetime.fromisoformat(new_exp.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Fecha de vencimiento inválida.")
        new_fisc = (
            data.fiscal_module_enabled
            if data.fiscal_module_enabled is not None
            else bool(t["fiscal_module_enabled"])
        )
        new_ana = (
            data.monthly_analysis_enabled
            if data.monthly_analysis_enabled is not None
            else (
                bool(t["monthly_analysis_enabled"])
                or bool(plan_row["includes_monthly_analysis"])
            )
        )

        await conn.execute(
            """
            UPDATE tenants
            SET plan_id = $1, subscription_status = $2, subscription_expires_at = $3,
                fiscal_module_enabled = $4, monthly_analysis_enabled = $5, updated_at = $6
            WHERE id = $7
            """,
            new_plan,
            new_status,
            new_exp,
            new_fisc,
            new_ana,
            now,
            tenant_id,
        )

        await log_audit(
            conn,
            tenant_id=tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="saas.tenant_update",
            entity_type="tenant",
            entity_id=tenant_id,
            details={
                "plan_id": new_plan,
                "subscription_status": new_status,
                "monthly_analysis_enabled": new_ana,
            },
            ip_address=ctx.ip_address,
        )

    return {"status": "ok", "tenant_id": tenant_id, "subscription_status": new_status}


async def record_subscription_payment(
    ctx: AuthContext, tenant_id: str, data: AdminSubscriptionPaymentRequest
) -> dict[str, Any]:
    db_mgr = get_db()
    now = utc_now()
    pay_id = new_id()

    async with db_mgr.transaction(tenant_id=tenant_id) as conn:
        t = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="Comercio no encontrado.")
        if not await conn.fetchval("SELECT id FROM subscription_plans WHERE id = $1", data.plan_id):
            raise HTTPException(status_code=400, detail="Plan inexistente.")

        await conn.execute(
            """
            INSERT INTO subscription_payments (
                id, tenant_id, plan_id, amount, includes_analysis_addon,
                period_start, period_end, payment_method, reference,
                notes, recorded_by, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            pay_id,
            tenant_id,
            data.plan_id,
            money(data.amount),
            data.includes_analysis_addon,
            data.period_start,
            data.period_end,
            data.payment_method,
            data.reference.strip(),
            data.notes.strip(),
            ctx.user_id,
            now,
        )

        # Registrar el pago no reactiva un servicio cortado: solo la acción explícita lo hace.
        await conn.execute(
            """
            UPDATE tenants
            SET plan_id = $1,
                monthly_analysis_enabled = CASE WHEN $2 THEN $2 ELSE monthly_analysis_enabled END,
                updated_at = $3
            WHERE id = $4
            """,
            data.plan_id,
            data.includes_analysis_addon,
            now,
            tenant_id,
        )

        await log_audit(
            conn,
            tenant_id=tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="saas.subscription_payment",
            entity_type="subscription_payment",
            entity_id=pay_id,
            details={
                "amount": str(money(data.amount)),
                "period": f"{data.period_start}..{data.period_end}",
            },
            ip_address=ctx.ip_address,
        )

    return {"id": pay_id, "status": "recorded"}


async def prepare_individual_monthly_report(
    ctx: AuthContext, tenant_id: str, data: AdminMonthlyReportRequest
) -> dict[str, Any]:
    """
    Servicio Adicional de Análisis Mensual (Sección 6):
    - Solo permite generar informe si el comercio tiene activo el servicio adicional
      (monthly_analysis_enabled = true), evitando accesos indebidos a comercios que no lo contrataron.
    - Ejecuta el cálculo en una sesión estrictamente aislada al tenant_id objetivo.
    - Deja constancia en audit_logs visible para trazabilidad.
    """
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        t = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="Comercio no encontrado.")
        if not bool(t["monthly_analysis_enabled"]):
            raise HTTPException(
                status_code=403,
                detail="Este comercio no tiene habilitado el Servicio Adicional de Análisis Mensual. Respetamos el aislamiento de datos no contratados.",
            )

    metrics = await compute_tenant_monthly_analytics(tenant_id, data.period_month)
    now = utc_now()
    status_val = "published" if data.publish else "draft"
    pub_at = now if data.publish else None

    # Si no se redactó resumen manual aún, generamos una base verificable para revisión de la propietaria
    bh = metrics["business_health_indicators"]
    auto_summary = data.executive_summary.strip() or (
        f"Informe mensual {data.period_month} para {t['name']}: "
        f"Índice de salud comercial {bh['health_score_0_100']}/100 ({bh['status_label']}). "
        f"Margen bruto realizado: {bh['realized_margin_pct']}%. "
        f"Tasa de recupero de fiados: {bh['fiado_recovery_rate_pct']}%."
    )
    auto_recs = data.recommendations.strip() or (
        "1) Revisar reposición de los productos líderes de rotación.\n"
        "2) Gestionar cobros preventivos sobre las cuentas corrientes de mayor antigüedad.\n"
        "3) Evaluar promociones sobre mercadería inmovilizada para liberar capital de trabajo."
    )

    async with db_mgr.transaction(tenant_id=tenant_id) as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM monthly_analysis_reports WHERE tenant_id = $1 AND period_month = $2",
            tenant_id,
            data.period_month,
        )
        if existing:
            rep_id = existing["id"]
            await conn.execute(
                """
                UPDATE monthly_analysis_reports
                SET status = $1, metrics_json = $2, executive_summary = $3,
                    recommendations = $4, prepared_by = $5, published_at = $6
                WHERE id = $7 AND tenant_id = $8
                """,
                status_val,
                json.dumps(metrics, ensure_ascii=False),
                auto_summary,
                auto_recs,
                ctx.user_id,
                pub_at,
                rep_id,
                tenant_id,
            )
        else:
            rep_id = new_id()
            await conn.execute(
                """
                INSERT INTO monthly_analysis_reports (
                    id, tenant_id, period_month, status, metrics_json,
                    executive_summary, recommendations, prepared_by,
                    published_at, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                rep_id,
                tenant_id,
                data.period_month,
                status_val,
                json.dumps(metrics, ensure_ascii=False),
                auto_summary,
                auto_recs,
                ctx.user_id,
                pub_at,
                now,
            )

        await log_audit(
            conn,
            tenant_id=tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=f"monthly_report.{status_val}",
            entity_type="monthly_analysis_report",
            entity_id=rep_id,
            details={"period_month": data.period_month},
            ip_address=ctx.ip_address,
        )

    return {
        "id": rep_id,
        "tenant_id": tenant_id,
        "tenant_name": t["name"],
        "period_month": data.period_month,
        "status": status_val,
        "executive_summary": auto_summary,
        "recommendations": auto_recs,
        "metrics": metrics,
    }


async def list_tenant_published_reports(ctx: AuthContext) -> list[dict[str, Any]]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM monthly_analysis_reports
            WHERE tenant_id = $1 AND status = 'published'
            ORDER BY period_month DESC
            """,
            ctx.tenant_id,
        )
    return [
        {
            "id": r["id"],
            "period_month": r["period_month"],
            "status": r["status"],
            "executive_summary": r["executive_summary"],
            "recommendations": r["recommendations"],
            "metrics": json.loads(r["metrics_json"]),
            "published_at": r["published_at"],
        }
        for r in rows
    ]


async def change_service(ctx: AuthContext, tenant_id: str, action: str, reason: str):
    if action not in ('suspend', 'reactivate'):
        raise HTTPException(400, 'Acción desconocida.')
    if len(reason.strip()) < 3: raise HTTPException(400, 'Ingresá un motivo válido.')
    now = utc_now()
    async with get_db().transaction(tenant_id=tenant_id) as conn:
        tenant = await conn.fetchrow('SELECT id,name,subscription_status FROM tenants WHERE id=$1 FOR UPDATE',tenant_id)
        if not tenant: raise HTTPException(404, 'Comercio no encontrado.')
        old=tenant['subscription_status']; new='suspended' if action=='suspend' else 'active'
        if old==new: raise HTTPException(409, 'El servicio ya tiene ese estado.')
        await conn.execute('UPDATE tenants SET subscription_status=$1, updated_at=$2 WHERE id=$3',new,now,tenant_id)
        await log_audit(conn,tenant_id=tenant_id,actor_user_id=ctx.user_id,actor_role=ctx.role,
            action='saas.service_'+action,entity_type='tenant',entity_id=tenant_id,
            details={'previous_status':old,'new_status':new,'reason':reason.strip(),'actor_email':ctx.email},ip_address=ctx.ip_address)
    return {'status':new,'tenant_id':tenant_id}

async def service_history(tenant_id: str):
    async with get_db().connection() as conn:
        tenant=await conn.fetchval('SELECT id FROM tenants WHERE id=$1',tenant_id)
        if not tenant: raise HTTPException(404,'Comercio no encontrado.')
        rows=await conn.fetch("""SELECT a.action,a.actor_user_id,a.actor_role,a.details_json,a.created_at,u.email,u.full_name
            FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_user_id
            WHERE a.tenant_id=$1 AND a.action IN ('saas.service_suspend','saas.service_reactivate')
            ORDER BY a.created_at DESC LIMIT 100""",tenant_id)
    return [{'action':r['action'],'actor_user_id':r['actor_user_id'],'actor_name':r['full_name'] or '',
             'actor_email':r['email'] or '', 'created_at':r['created_at'],'details':json.loads(r['details_json'])} for r in rows]
