import json
from typing import Any

from fastapi import APIRouter, Depends, Response, File, UploadFile, HTTPException

from app.db import get_db
from app.security import AuthContext, require_tenant_owner, require_tenant_read, require_tenant_export
from app.services import reports as reports_svc
from app.services import saas_admin as saas_svc

router = APIRouter(prefix="/api/v1/reports", tags=["Ganancias, Reportes y Respaldos"])


@router.get("/financial")
async def financial_results(
    period_month: str = "",
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await reports_svc.get_financial_results(ctx, period_month=period_month)


@router.get("/analytics")
async def monthly_analytics(
    period_month: str = "",
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    return await reports_svc.compute_tenant_monthly_analytics(
        ctx.tenant_id, target_month=period_month or None
    )


@router.get("/verify-consistency")
async def verify_consistency(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await reports_svc.verify_financial_consistency(ctx)


@router.get("/monthly-published")
async def published_monthly_reports(
    ctx: AuthContext = Depends(require_tenant_read),
) -> list[dict[str, Any]]:
    return await saas_svc.list_tenant_published_reports(ctx)


@router.get("/audit-logs")
async def list_audit_logs(
    ctx: AuthContext = Depends(require_tenant_owner),
) -> list[dict[str, Any]]:
    db_mgr = get_db()
    async with db_mgr.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT a.*, u.full_name AS actor_name
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.actor_user_id
            WHERE a.tenant_id = $1
            ORDER BY a.created_at DESC
            LIMIT 100
            """,
            ctx.tenant_id,
        )
    return [
        {
            "id": r["id"],
            "action": r["action"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "actor_role": r["actor_role"],
            "actor_name": r.get("actor_name") or "Sistema",
            "details": json.loads(r["details_json"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("/backup.json")
async def export_backup_json(
    ctx: AuthContext = Depends(require_tenant_export),
) -> Response:
    payload = await reports_svc.export_tenant_backup_json(ctx)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=raw,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="respaldo_mi_tiendita.json"'
        },
    )


@router.get("/export.xlsx")
async def export_excel(
    ctx: AuthContext = Depends(require_tenant_read),
) -> Response:
    content = await reports_svc.export_tenant_excel_xlsx(ctx)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="reporte_integral_mi_tiendita.xlsx"'
        },
    )


@router.post("/restore.json")
async def restore_json(file: UploadFile = File(...), ctx: AuthContext = Depends(require_tenant_owner)):
    from app.services.restore import restore_empty_tenant
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024: raise HTTPException(413, "Respaldo demasiado grande (máximo 5 MB).")
    try: payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError): raise HTTPException(400, "El archivo no es JSON válido.")
    return await restore_empty_tenant(ctx, payload)
