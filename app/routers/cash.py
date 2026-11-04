from typing import Any

from fastapi import APIRouter, Depends

from app.schemas import (
    CashMovementCreateRequest,
    CashSessionCloseRequest,
    CashSessionOpenRequest,
)
from app.security import AuthContext, require_tenant_read, require_tenant_write
from app.services import cash as cash_svc

router = APIRouter(prefix="/api/v1/cash", tags=["Caja, Ingresos y Egresos"])


@router.get("")
async def get_cash_status(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await cash_svc.get_cash_status(ctx)


@router.post("/open", status_code=201)
async def open_cash(
    data: CashSessionOpenRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await cash_svc.open_cash_session(ctx, data)


@router.post("/close")
async def close_cash(
    data: CashSessionCloseRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await cash_svc.close_cash_session(ctx, data)


@router.post("/movements", status_code=201)
async def create_cash_movement(
    data: CashMovementCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await cash_svc.register_manual_cash_movement(ctx, data)
