from typing import Any

from fastapi import APIRouter, Depends

from app.schemas import SaleCreateRequest, SaleVoidRequest
from app.security import (
    AuthContext,
    require_tenant_owner,
    require_tenant_read,
    require_tenant_write,
)
from app.services import sales as sales_svc

router = APIRouter(prefix="/api/v1/sales", tags=["Calculadora de Ventas"])


@router.post("", status_code=201)
async def create_sale(
    data: SaleCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await sales_svc.create_sale(ctx, data)


@router.get("")
async def list_sales(
    limit: int = 100,
    status: str = "",
    ctx: AuthContext = Depends(require_tenant_read),
) -> list[dict[str, Any]]:
    return await sales_svc.list_sales(ctx, limit=min(limit, 500), status_filter=status)


@router.get("/{sale_id}")
async def get_sale(
    sale_id: str,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await sales_svc.get_sale(ctx, sale_id)


@router.post("/{sale_id}/void")
async def void_sale(
    sale_id: str,
    data: SaleVoidRequest,
    ctx: AuthContext = Depends(require_tenant_owner),
) -> dict[str, Any]:
    return await sales_svc.void_sale(ctx, sale_id, data)
