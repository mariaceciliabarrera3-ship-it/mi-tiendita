import os
from typing import Any

from fastapi import APIRouter, Depends, UploadFile, HTTPException
import threading

from app.schemas import (
    PurchaseCreateRequest,
    SupplierCreateRequest,
    SupplierPaymentRequest,
)
from app.security import AuthContext, require_tenant_read, require_tenant_write
from app.services import suppliers as suppliers_svc

router = APIRouter(prefix="/api/v1/suppliers", tags=["Compras y Proveedores"])


@router.get("")
async def list_suppliers(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await suppliers_svc.list_suppliers(ctx)


@router.post("", status_code=201)
async def create_supplier(
    data: SupplierCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await suppliers_svc.create_supplier(ctx, data)


@router.get("/purchases")
async def list_purchases(
    status: str = "",
    ctx: AuthContext = Depends(require_tenant_read),
) -> list[dict[str, Any]]:
    return await suppliers_svc.list_purchases(ctx, status_filter=status)


@router.post("/purchases", status_code=201)
async def create_purchase(
    data: PurchaseCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await suppliers_svc.create_purchase(ctx, data)


@router.post("/{supplier_id}/payments")
async def pay_supplier(
    supplier_id: str,
    data: SupplierPaymentRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await suppliers_svc.register_supplier_payment(ctx, supplier_id, data)

# This endpoint is extraction-only: no SQL write and no purchase confirmation.
# Una extracción a la vez por defecto: Tesseract/Poppler consumen cientos de MB y una instancia
# chica que se queda sin memoria corta también las ventas. Ajustable con OCR_MAX_CONCURRENT.
_ocr_slots = threading.BoundedSemaphore(max(1, int(os.getenv("OCR_MAX_CONCURRENT", "1") or 1)))
@router.post('/document-extraction')
async def extract_purchase_document(file: UploadFile, ctx: AuthContext = Depends(require_tenant_write)):
    import asyncio
    from app.services.ocr import extract, MAX_BYTES
    data=await file.read(MAX_BYTES+1)
    await file.close()
    if not _ocr_slots.acquire(blocking=False):
        raise HTTPException(429, 'Hay documentos procesándose. Reintentá en unos segundos.')
    try:
        return await asyncio.to_thread(extract,data)
    finally:
        _ocr_slots.release()
