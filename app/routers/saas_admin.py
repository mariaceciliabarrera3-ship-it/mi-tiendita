from typing import Any

from fastapi import APIRouter, Depends

from app.schemas import (
    AdminServiceChangeRequest,
    AdminMonthlyReportRequest,
    AdminSubscriptionPaymentRequest,
    AdminTenantUpdateRequest,
)
from app.security import AuthContext, require_saas_owner
from app.services import saas_admin as saas_svc

# Criterio único: TODO el panel de administración exige la cuenta propietaria configurada
# (rol superadmin + correo == SUPERADMIN_EMAIL), no solo el corte/reactivación.
router = APIRouter(prefix="/api/v1/admin", tags=["Administración General SaaS"])


@router.get("/overview")
async def saas_overview(
    ctx: AuthContext = Depends(require_saas_owner),
) -> dict[str, Any]:
    return await saas_svc.get_saas_overview(ctx)


@router.patch("/tenants/{tenant_id}")
async def update_tenant_plan(
    tenant_id: str,
    data: AdminTenantUpdateRequest,
    ctx: AuthContext = Depends(require_saas_owner),
) -> dict[str, Any]:
    return await saas_svc.update_tenant_subscription(ctx, tenant_id, data)


@router.post("/tenants/{tenant_id}/payments", status_code=201)
async def record_payment(
    tenant_id: str,
    data: AdminSubscriptionPaymentRequest,
    ctx: AuthContext = Depends(require_saas_owner),
) -> dict[str, Any]:
    return await saas_svc.record_subscription_payment(ctx, tenant_id, data)


@router.post("/tenants/{tenant_id}/monthly-reports", status_code=201)
async def generate_monthly_report(
    tenant_id: str,
    data: AdminMonthlyReportRequest,
    ctx: AuthContext = Depends(require_saas_owner),
) -> dict[str, Any]:
    return await saas_svc.prepare_individual_monthly_report(ctx, tenant_id, data)


@router.post("/tenants/{tenant_id}/service/{action}")
async def service_action(tenant_id: str, action: str, data: AdminServiceChangeRequest, ctx: AuthContext = Depends(require_saas_owner)):
    return await saas_svc.change_service(ctx, tenant_id, action, data.reason)

@router.get("/tenants/{tenant_id}/service-history")
async def service_history(tenant_id: str, ctx: AuthContext = Depends(require_saas_owner)):
    return await saas_svc.service_history(tenant_id)
