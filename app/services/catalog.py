import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from openpyxl import Workbook, load_workbook

from app.config import money, money_float, new_id, qty, qty_float, utc_now
from app.db import get_db
from app.schemas import ProductCreateRequest, ProductUpdateRequest, StockMovementRequest
from app.security import AuthContext, log_audit
from app.xlsx_safe import neutralize_formulas


MAX_IMPORT_ROWS = 2000  # cada fila es una transacción propia: se acota para no tomar el servidor


def enrich_product_metrics(row: dict[str, Any]) -> dict[str, Any]:
    cost = money(row["cost_price"])
    price = money(row["sale_price"])
    stock = qty(row["stock_qty"])
    min_stock = qty(row["min_stock_qty"])
    diff = money(price - cost)

    margin_on_sale = (
        float(((price - cost) / price * Decimal("100")).quantize(Decimal("0.01")))
        if price > Decimal("0")
        else 0.0
    )
    markup_on_cost = (
        float(((price - cost) / cost * Decimal("100")).quantize(Decimal("0.01")))
        if cost > Decimal("0")
        else 0.0
    )
    inv_cost_val = money(stock * cost)
    inv_sale_val = money(stock * price)
    inv_profit_val = money(inv_sale_val - inv_cost_val)
    is_low_stock = bool(row.get("is_active", True)) and (
        stock <= min_stock and (min_stock > Decimal("0") or stock == Decimal("0"))
    )

    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "sku": row.get("sku") or "",
        "name": row["name"],
        "category": row["category"],
        "unit": row["unit"],
        "stock_qty": qty_float(stock),
        "min_stock_qty": qty_float(min_stock),
        "cost_price": money_float(cost),
        "sale_price": money_float(price),
        "unit_profit": money_float(diff),
        "margin_pct": margin_on_sale,
        "markup_pct": markup_on_cost,
        "inventory_cost_value": money_float(inv_cost_val),
        "inventory_sale_value": money_float(inv_sale_val),
        "potential_gross_profit": money_float(inv_profit_val),
        "is_low_stock": is_low_stock,
        "notes": row.get("notes") or "",
        "is_active": bool(row.get("is_active", True)),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def list_products_with_valuation(
    ctx: AuthContext,
    search: str = "",
    category: str = "",
    include_inactive: bool = False,
    low_stock_only: bool = False,
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM products
            WHERE tenant_id = $1
            ORDER BY is_active DESC, name ASC
            """,
            ctx.tenant_id,
        )

    total_cost_val = Decimal("0.00")
    total_sale_val = Decimal("0.00")
    low_stock_alerts: list[dict[str, Any]] = []
    categories_set: set[str] = set()
    filtered_items: list[dict[str, Any]] = []

    q = search.strip().lower()
    cat_filter = category.strip().lower()

    for r in rows:
        is_act = bool(r["is_active"])
        if is_act:
            categories_set.add(r["category"])
            st = qty(r["stock_qty"])
            cp = money(r["cost_price"])
            sp = money(r["sale_price"])
            total_cost_val += money(st * cp)
            total_sale_val += money(st * sp)

        enriched = enrich_product_metrics(r)
        if enriched["is_low_stock"] and is_act:
            low_stock_alerts.append(
                {
                    "id": enriched["id"],
                    "name": enriched["name"],
                    "sku": enriched["sku"],
                    "stock_qty": enriched["stock_qty"],
                    "min_stock_qty": enriched["min_stock_qty"],
                    "unit": enriched["unit"],
                }
            )

        if not include_inactive and not is_act:
            continue
        if low_stock_only and not enriched["is_low_stock"]:
            continue
        if cat_filter and r["category"].strip().lower() != cat_filter:
            continue
        if q:
            haystack = f"{r['name']} {r.get('sku') or ''} {r['category']}".lower()
            if q not in haystack:
                continue

        filtered_items.append(enriched)

    potential_profit = money(total_sale_val - total_cost_val)
    overall_margin = (
        float((potential_profit / total_sale_val * Decimal("100")).quantize(Decimal("0.01")))
        if total_sale_val > Decimal("0")
        else 0.0
    )
    overall_markup = (
        float((potential_profit / total_cost_val * Decimal("100")).quantize(Decimal("0.01")))
        if total_cost_val > Decimal("0")
        else 0.0
    )

    return {
        "items": filtered_items,
        "categories": sorted(categories_set),
        "valuation": {
            "active_products_count": sum(1 for r in rows if bool(r["is_active"])),
            "inventory_cost_value": money_float(total_cost_val),
            "inventory_sale_value": money_float(total_sale_val),
            "potential_gross_profit": money_float(potential_profit),
            "overall_margin_pct": overall_margin,
            "overall_markup_pct": overall_markup,
            "low_stock_count": len(low_stock_alerts),
        },
        "low_stock_alerts": low_stock_alerts,
    }


async def create_product(ctx: AuthContext, data: ProductCreateRequest) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    sku_clean = (data.sku or "").strip() or None
    now = utc_now()
    cost = money(data.cost_price)
    price = money(data.sale_price)
    init_stock = qty(data.stock_qty)
    min_stock = qty(data.min_stock_qty)

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        # Verificar límite del plan del comercio
        plan_info = await conn.fetchrow(
            """
            SELECT p.max_products,
                   (SELECT COUNT(*) FROM products pr WHERE pr.tenant_id = $1 AND pr.is_active = $2) AS current_count
            FROM tenants t
            JOIN subscription_plans p ON p.id = t.plan_id
            WHERE t.id = $1
            """,
            ctx.tenant_id,
            True,
        )
        if plan_info and int(plan_info["current_count"]) >= int(plan_info["max_products"]):
            raise HTTPException(
                status_code=403,
                detail=f"Alcanzaste el límite de {plan_info['max_products']} productos activos de tu plan actual.",
            )

        if sku_clean:
            dup = await conn.fetchval(
                "SELECT id FROM products WHERE tenant_id = $1 AND sku = $2",
                ctx.tenant_id,
                sku_clean,
            )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Ya existe un producto con el código '{sku_clean}' en tu negocio.",
                )

        prod_id = new_id()
        await conn.execute(
            """
            INSERT INTO products (
                id, tenant_id, sku, name, category, unit,
                stock_qty, min_stock_qty, cost_price, sale_price,
                notes, is_active, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """,
            prod_id,
            ctx.tenant_id,
            sku_clean,
            data.name.strip(),
            data.category.strip() or "General",
            data.unit,
            init_stock,
            min_stock,
            cost,
            price,
            data.notes.strip(),
            True,
            now,
            now,
        )

        # Registrar movimiento inicial de kárdex para trazabilidad y reconstrucción exacta
        if init_stock > Decimal("0"):
            await conn.execute(
                """
                INSERT INTO inventory_movements (
                    id, tenant_id, product_id, movement_type,
                    qty_delta, stock_before, stock_after, unit_cost_snapshot,
                    reference_type, reference_id, notes, created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                new_id(),
                ctx.tenant_id,
                prod_id,
                "initial_load",
                init_stock,
                Decimal("0.000"),
                init_stock,
                cost,
                "product_create",
                prod_id,
                "Carga inicial de mercadería",
                ctx.user_id,
                now,
            )

        await conn.execute(
            """
            INSERT INTO product_price_history (
                id, tenant_id, product_id, old_cost, new_cost,
                old_sale_price, new_sale_price, reason, changed_by, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            new_id(),
            ctx.tenant_id,
            prod_id,
            cost,
            cost,
            price,
            price,
            "Alta inicial de producto",
            ctx.user_id,
            now,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="product.create",
            entity_type="product",
            entity_id=prod_id,
            details={"name": data.name.strip(), "stock": str(init_stock), "cost": str(cost), "price": str(price)},
            ip_address=ctx.ip_address,
        )

        created = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2",
            prod_id,
            ctx.tenant_id,
        )

    assert created is not None
    return enrich_product_metrics(created)


async def update_product(
    ctx: AuthContext, product_id: str, data: ProductUpdateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        current = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            product_id,
            ctx.tenant_id,
        )
        if not current:
            raise HTTPException(status_code=404, detail="Producto no encontrado en tu comercio.")

        old_cost = money(current["cost_price"])
        old_price = money(current["sale_price"])
        new_cost = money(data.cost_price) if data.cost_price is not None else old_cost
        new_price = money(data.sale_price) if data.sale_price is not None else old_price
        new_name = data.name.strip() if data.name is not None else current["name"]
        new_cat = data.category.strip() if data.category is not None else current["category"]
        new_unit = data.unit if data.unit is not None else current["unit"]
        new_min = qty(data.min_stock_qty) if data.min_stock_qty is not None else qty(current["min_stock_qty"])
        new_notes = data.notes.strip() if data.notes is not None else (current["notes"] or "")
        new_active = data.is_active if data.is_active is not None else bool(current["is_active"])

        if data.sku is not None:
            new_sku = data.sku.strip() or None
            if new_sku and new_sku != current.get("sku"):
                dup = await conn.fetchval(
                    "SELECT id FROM products WHERE tenant_id = $1 AND sku = $2 AND id <> $3",
                    ctx.tenant_id,
                    new_sku,
                    product_id,
                )
                if dup:
                    raise HTTPException(
                        status_code=409,
                        detail=f"El código '{new_sku}' ya está asignado a otro producto.",
                    )
        else:
            new_sku = current.get("sku")

        await conn.execute(
            """
            UPDATE products
            SET sku = $1, name = $2, category = $3, unit = $4,
                min_stock_qty = $5, cost_price = $6, sale_price = $7,
                notes = $8, is_active = $9, updated_at = $10
            WHERE id = $11 AND tenant_id = $12
            """,
            new_sku,
            new_name,
            new_cat,
            new_unit,
            new_min,
            new_cost,
            new_price,
            new_notes,
            new_active,
            now,
            product_id,
            ctx.tenant_id,
        )

        # Trazabilidad de cambios de costos/precios SIN alterar ventas históricas
        if new_cost != old_cost or new_price != old_price:
            await conn.execute(
                """
                INSERT INTO product_price_history (
                    id, tenant_id, product_id, old_cost, new_cost,
                    old_sale_price, new_sale_price, reason, changed_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                new_id(),
                ctx.tenant_id,
                product_id,
                old_cost,
                new_cost,
                old_price,
                new_price,
                data.price_change_reason.strip(),
                ctx.user_id,
                now,
            )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="product.update",
            entity_type="product",
            entity_id=product_id,
            details={
                "old_cost": str(old_cost),
                "new_cost": str(new_cost),
                "old_price": str(old_price),
                "new_price": str(new_price),
                "is_active": new_active,
            },
            ip_address=ctx.ip_address,
        )

        updated = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2",
            product_id,
            ctx.tenant_id,
        )

    assert updated is not None
    return enrich_product_metrics(updated)


async def register_stock_movement(
    ctx: AuthContext, product_id: str, data: StockMovementRequest
) -> dict[str, Any]:
    """
    Registra entradas/salidas manuales auditadas: ajustes, pérdidas, dañados,
    devoluciones y correcciones autorizadas.
    """
    assert ctx.tenant_id is not None
    if data.movement_type == "authorized_correction" and ctx.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Las correcciones autorizadas de stock requieren permiso del titular del comercio.",
        )

    positive_types = {"adjustment_add", "customer_return"}
    negative_types = {"adjustment_sub", "loss", "damaged", "supplier_return"}

    amount = qty(data.quantity)
    now = utc_now()
    db_mgr = get_db()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        prod = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            product_id,
            ctx.tenant_id,
        )
        if not prod:
            raise HTTPException(status_code=404, detail="Producto no encontrado.")

        before = qty(prod["stock_qty"])
        if data.movement_type == "authorized_correction":
            # En corrección autorizada, 'quantity' fija el stock físico real arqueado (o ajuste exacto)
            # Aquí permitimos también pasar el nuevo stock objetivo si viene en authorized_correction:
            # Para claridad uniforme: en authorized_correction, 'quantity' representa el stock físico contado real!
            # Espera: si quantity > 0, calculamos delta = quantity - before. (Si quiere llevar a 0, usa loss/adjustment_sub).
            after = amount
            delta = qty(after - before)
        elif data.movement_type in positive_types:
            delta = amount
            after = qty(before + delta)
        elif data.movement_type in negative_types:
            delta = -amount
            after = qty(before + delta)
            if after < Decimal("0"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Stock insuficiente para descontar {qty_float(amount)} {prod['unit']}. Disponible actual: {qty_float(before)}.",
                )
        else:
            raise HTTPException(status_code=400, detail="Tipo de movimiento inválido.")

        await conn.execute(
            "UPDATE products SET stock_qty = $1, updated_at = $2 WHERE id = $3 AND tenant_id = $4",
            after,
            now,
            product_id,
            ctx.tenant_id,
        )

        mov_id = new_id()
        await conn.execute(
            """
            INSERT INTO inventory_movements (
                id, tenant_id, product_id, movement_type,
                qty_delta, stock_before, stock_after, unit_cost_snapshot,
                reference_type, reference_id, notes, created_by, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            mov_id,
            ctx.tenant_id,
            product_id,
            data.movement_type,
            delta,
            before,
            after,
            money(prod["cost_price"]),
            "manual_movement",
            mov_id,
            data.notes.strip() or f"Movimiento: {data.movement_type}",
            ctx.user_id,
            now,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=f"inventory.{data.movement_type}",
            entity_type="product",
            entity_id=product_id,
            details={"before": str(before), "delta": str(delta), "after": str(after), "notes": data.notes},
            ip_address=ctx.ip_address,
        )

        updated = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2",
            product_id,
            ctx.tenant_id,
        )

    assert updated is not None
    return enrich_product_metrics(updated)


async def get_product_history(ctx: AuthContext, product_id: str) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        prod = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1 AND tenant_id = $2",
            product_id,
            ctx.tenant_id,
        )
        if not prod:
            raise HTTPException(status_code=404, detail="Producto no encontrado.")

        movements = await conn.fetch(
            """
            SELECT m.*, u.full_name AS actor_name
            FROM inventory_movements m
            LEFT JOIN users u ON u.id = m.created_by
            WHERE m.tenant_id = $1 AND m.product_id = $2
            ORDER BY m.created_at DESC
            LIMIT 200
            """,
            ctx.tenant_id,
            product_id,
        )
        prices = await conn.fetch(
            """
            SELECT h.*, u.full_name AS actor_name
            FROM product_price_history h
            LEFT JOIN users u ON u.id = h.changed_by
            WHERE h.tenant_id = $1 AND h.product_id = $2
            ORDER BY h.created_at DESC
            LIMIT 100
            """,
            ctx.tenant_id,
            product_id,
        )
        reconstructed_val = await conn.fetchval(
            "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_movements WHERE tenant_id = $1 AND product_id = $2",
            ctx.tenant_id,
            product_id,
        )

    reconstructed = qty(reconstructed_val)
    actual = qty(prod["stock_qty"])
    return {
        "product": enrich_product_metrics(prod),
        "reconstructed_stock": qty_float(reconstructed),
        "stock_matches_kardex": reconstructed == actual,
        "movements": [
            {
                "id": m["id"],
                "movement_type": m["movement_type"],
                "qty_delta": qty_float(m["qty_delta"]),
                "stock_before": qty_float(m["stock_before"]),
                "stock_after": qty_float(m["stock_after"]),
                "unit_cost_snapshot": money_float(m["unit_cost_snapshot"]),
                "notes": m["notes"],
                "actor_name": m.get("actor_name") or "Sistema",
                "created_at": m["created_at"],
            }
            for m in movements
        ],
        "price_history": [
            {
                "id": p["id"],
                "old_cost": money_float(p["old_cost"]),
                "new_cost": money_float(p["new_cost"]),
                "old_sale_price": money_float(p["old_sale_price"]),
                "new_sale_price": money_float(p["new_sale_price"]),
                "reason": p["reason"],
                "actor_name": p.get("actor_name") or "Sistema",
                "created_at": p["created_at"],
            }
            for p in prices
        ],
    }


async def verify_kardex_integrity(ctx: AuthContext) -> dict[str, Any]:
    """Reconstruye el stock de todos los productos desde inventory_movements y comprueba consistencia."""
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.name, p.stock_qty,
                   COALESCE((SELECT SUM(m.qty_delta) FROM inventory_movements m
                             WHERE m.tenant_id = p.tenant_id AND m.product_id = p.id), 0) AS kardex_sum
            FROM products p
            WHERE p.tenant_id = $1
            """,
            ctx.tenant_id,
        )

    discrepancies = []
    for r in rows:
        st = qty(r["stock_qty"])
        ks = qty(r["kardex_sum"])
        if st != ks:
            discrepancies.append(
                {
                    "product_id": r["id"],
                    "name": r["name"],
                    "recorded_stock": qty_float(st),
                    "reconstructed_stock": qty_float(ks),
                }
            )

    return {
        "total_products_checked": len(rows),
        "is_consistent": len(discrepancies) == 0,
        "discrepancies": discrepancies,
    }


def _normalize_header(h: Any) -> str:
    s = str(h or "").strip().lower()
    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        " ": "_",
        "-": "_",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return s


def _parse_decimal_cell(val: Any, default: str = "0") -> Decimal:
    if val is None or str(val).strip() == "":
        return Decimal(default)
    s = str(val).strip().replace("$", "").replace(" ", "")
    # Soportar formato argentino 1.250,50 si contiene coma decimal
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Valor numérico inválido: '{val}'")


async def import_products_file(
    ctx: AuthContext, filename: str, raw_bytes: bytes
) -> dict[str, Any]:
    """Importa catálogo inicial o masivo desde archivo CSV o Excel (.xlsx)."""
    assert ctx.tenant_id is not None
    fname = filename.lower()
    parsed_rows: list[dict[str, Any]] = []

    if fname.endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(raw_bytes), data_only=True)
        ws = wb.active
        if ws.max_row and ws.max_row > MAX_IMPORT_ROWS + 1:
            raise HTTPException(status_code=413, detail=f"El archivo supera el máximo de {MAX_IMPORT_ROWS} filas por carga. Dividilo en partes.")
        rows_iter = list(ws.iter_rows(values_only=True))
        if not rows_iter:
            raise HTTPException(status_code=400, detail="El archivo Excel está vacío.")
        headers = [_normalize_header(c) for c in rows_iter[0]]
        for raw_r in rows_iter[1:]:
            if not raw_r or all(c is None or str(c).strip() == "" for c in raw_r):
                continue
            parsed_rows.append(dict(zip(headers, raw_r)))
    elif fname.endswith(".csv"):
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        # Detectar delimitador ';' (habitual en Excel en español) o ','
        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        all_rows = list(reader)
        if not all_rows:
            raise HTTPException(status_code=400, detail="El archivo CSV está vacío.")
        if len(all_rows) > MAX_IMPORT_ROWS + 1:
            raise HTTPException(status_code=413, detail=f"El archivo supera el máximo de {MAX_IMPORT_ROWS} filas por carga. Dividilo en partes.")
        headers = [_normalize_header(c) for c in all_rows[0]]
        for raw_r in all_rows[1:]:
            if not raw_r or all(str(c).strip() == "" for c in raw_r):
                continue
            parsed_rows.append(dict(zip(headers, raw_r)))
    else:
        raise HTTPException(
            status_code=400,
            detail="Formato no soportado. Subí un archivo .csv o .xlsx.",
        )

    valid_units = {"unidad", "kg", "g", "litro", "pack", "docena", "metro"}
    created_count = 0
    errors: list[str] = []

    for idx, row in enumerate(parsed_rows, start=2):
        name = str(
            row.get("nombre") or row.get("producto") or row.get("name") or ""
        ).strip()
        if not name:
            errors.append(f"Fila {idx}: falta el nombre del producto.")
            continue
        try:
            sku = str(row.get("codigo") or row.get("sku") or "").strip() or None
            category = str(row.get("categoria") or row.get("category") or "General").strip() or "General"
            unit_raw = str(row.get("unidad") or row.get("unidad_de_medida") or row.get("unit") or "unidad").strip().lower()
            unit = unit_raw if unit_raw in valid_units else "unidad"
            stock_val = _parse_decimal_cell(
                row.get("cantidad") or row.get("cantidad_disponible") or row.get("stock") or "0"
            )
            min_val = _parse_decimal_cell(
                row.get("stock_minimo") or row.get("minimo") or "0"
            )
            cost_val = _parse_decimal_cell(
                row.get("costo") or row.get("precio_compra") or row.get("costo_unitario") or "0"
            )
            price_val = _parse_decimal_cell(
                row.get("precio_venta") or row.get("precio") or row.get("venta") or "0"
            )
            notes = str(row.get("observaciones") or row.get("notas") or "").strip()

            req = ProductCreateRequest(
                name=name,
                sku=sku,
                category=category,
                unit=unit,  # type: ignore[arg-type]
                stock_qty=stock_val,
                min_stock_qty=min_val,
                cost_price=cost_val,
                sale_price=price_val,
                notes=notes,
            )
            await create_product(ctx, req)
            created_count += 1
        except HTTPException as he:
            errors.append(f"Fila {idx} ({name}): {he.detail}")
        except Exception as exc:
            errors.append(f"Fila {idx} ({name}): {exc}")

    return {
        "imported_count": created_count,
        "total_rows_read": len(parsed_rows),
        "errors": errors,
    }


def generate_import_template_xlsx() -> bytes:
    """Genera una plantilla Excel (.xlsx) vacía con encabezados claros y una fila guía explicativa."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Carga_Mercaderia"
    ws.append(
        [
            "nombre",
            "codigo",
            "categoria",
            "cantidad",
            "unidad",
            "precio_compra",
            "precio_venta",
            "stock_minimo",
            "observaciones",
        ]
    )
    # No precargamos catálogo genérico: dejamos una sola fila de ejemplo claramente rotulada para borrar o reemplazar
    ws.append(
        [
            "EJEMPLO BORRAR - Mi Producto",
            "COD-001",
            "Almacén",
            10,
            "unidad",
            500,
            800,
            3,
            "Reemplazar por tus artículos reales",
        ]
    )
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
