from typing import Any

from fastapi import APIRouter, Depends, Response

from app.schemas import FiscalProfileUpdateRequest, FiscalVoucherCreateRequest
from app.security import AuthContext, require_tenant_owner, require_tenant_read, require_tenant_write
from app.services import fiscal as fiscal_svc

router = APIRouter(prefix="/api/v1/fiscal", tags=["Módulo Contable e Impositivo Opcional"])


@router.get("")
async def get_fiscal_data(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await fiscal_svc.get_fiscal_module_data(ctx)


@router.post("/toggle")
async def toggle_fiscal(
    enabled: bool = True,
    ctx: AuthContext = Depends(require_tenant_owner),
) -> dict[str, Any]:
    return await fiscal_svc.toggle_fiscal_module(ctx, enabled)


@router.put("/profile")
async def update_profile(
    data: FiscalProfileUpdateRequest,
    ctx: AuthContext = Depends(require_tenant_owner),
) -> dict[str, Any]:
    return await fiscal_svc.upsert_fiscal_profile(ctx, data)


@router.post("/vouchers", status_code=201)
async def create_voucher(
    data: FiscalVoucherCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await fiscal_svc.create_fiscal_voucher(ctx, data)


@router.get("/export-accountant.xlsx")
async def export_accountant_pack(
    ctx: AuthContext = Depends(require_tenant_owner),
) -> Response:
    content = await fiscal_svc.export_accountant_xlsx(ctx)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="para_contador_mi_tiendita.xlsx"'
        },
    )
