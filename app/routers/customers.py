from typing import Any

from fastapi import APIRouter, Depends

from app.schemas import (
    CustomerCreateRequest,
    CustomerPaymentRequest,
    CustomerUpdateRequest,
)
from app.security import AuthContext, require_tenant_read, require_tenant_write
from app.services import customers as customers_svc

router = APIRouter(prefix="/api/v1/customers", tags=["Clientes y Fiados"])


@router.get("")
async def list_customers(
    search: str = "",
    debtors_only: bool = False,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await customers_svc.list_customers(
        ctx, search=search, debtors_only=debtors_only
    )


@router.post("", status_code=201)
async def create_customer(
    data: CustomerCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await customers_svc.create_customer(ctx, data)


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: str,
    data: CustomerUpdateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await customers_svc.update_customer(ctx, customer_id, data)


@router.get("/{customer_id}")
async def get_customer(
    customer_id: str,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await customers_svc.get_customer_detail(ctx, customer_id)


@router.post("/{customer_id}/payments")
async def register_payment(
    customer_id: str,
    data: CustomerPaymentRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await customers_svc.register_customer_payment(ctx, customer_id, data)
