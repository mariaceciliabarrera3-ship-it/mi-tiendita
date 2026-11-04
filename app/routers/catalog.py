from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from app.schemas import (
    ProductCreateRequest,
    ProductUpdateRequest,
    StockMovementRequest,
)
from app.security import AuthContext, require_tenant_read, require_tenant_write
from app.services import catalog as catalog_svc

router = APIRouter(prefix="/api/v1/products", tags=["Productos e Inventario"])


@router.get("")
async def list_products(
    search: str = "",
    category: str = "",
    include_inactive: bool = False,
    low_stock_only: bool = False,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await catalog_svc.list_products_with_valuation(
        ctx,
        search=search,
        category=category,
        include_inactive=include_inactive,
        low_stock_only=low_stock_only,
    )


@router.post("", status_code=201)
async def create_product(
    data: ProductCreateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await catalog_svc.create_product(ctx, data)


@router.get("/verify-kardex")
async def verify_kardex(
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await catalog_svc.verify_kardex_integrity(ctx)


@router.get("/import-template.xlsx")
async def download_import_template(
    ctx: AuthContext = Depends(require_tenant_read),
) -> Response:
    content = catalog_svc.generate_import_template_xlsx()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="plantilla_carga_mi_tiendita.xlsx"'
        },
    )


@router.post("/import")
async def import_products(
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail="El archivo supera el tamaño máximo de 5 MB."
        )
    return await catalog_svc.import_products_file(
        ctx, file.filename or "carga.csv", raw
    )


@router.patch("/{product_id}")
async def update_product(
    product_id: str,
    data: ProductUpdateRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await catalog_svc.update_product(ctx, product_id, data)


@router.post("/{product_id}/movements")
async def create_stock_movement(
    product_id: str,
    data: StockMovementRequest,
    ctx: AuthContext = Depends(require_tenant_write),
) -> dict[str, Any]:
    return await catalog_svc.register_stock_movement(ctx, product_id, data)


@router.get("/{product_id}/history")
async def get_product_history(
    product_id: str,
    ctx: AuthContext = Depends(require_tenant_read),
) -> dict[str, Any]:
    return await catalog_svc.get_product_history(ctx, product_id)
