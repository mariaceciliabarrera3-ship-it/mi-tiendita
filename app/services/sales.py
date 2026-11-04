from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from app.config import money, money_float, new_id, qty, qty_float, utc_now
from app.db import DBConnectionWrapper, get_db
from app.schemas import SaleCreateRequest, SaleVoidRequest
from app.security import AuthContext, log_audit
from app.services.cash import record_channel_movements


def _determine_payment_summary(cash: Decimal, transfer: Decimal, fiado: Decimal, wallet: Decimal = Decimal("0")) -> str:
    active_methods = []
    if cash > Decimal("0"):
        active_methods.append("efectivo")
    if transfer > Decimal("0"):
        active_methods.append("transferencia")
    if fiado > Decimal("0"):
        active_methods.append("fiado")
    if wallet > Decimal("0"):
        active_methods.append("billetera")
    if len(active_methods) == 0:
        return "bonificado"
    if len(active_methods) == 1:
        return active_methods[0]
    return "combinado"


async def _fetch_sale_detail(
    conn: DBConnectionWrapper, tenant_id: str, sale_id: str
) -> dict[str, Any]:
    sale = await conn.fetchrow(
        """
        SELECT s.*, c.name AS customer_name, c.phone AS customer_phone, u.full_name AS seller_name, t.currency AS currency
        FROM sales s
        JOIN tenants t ON t.id = s.tenant_id
        LEFT JOIN customers c ON c.id = s.customer_id AND c.tenant_id = s.tenant_id
        LEFT JOIN users u ON u.id = s.created_by
        WHERE s.id = $1 AND s.tenant_id = $2
        """,
        sale_id,
        tenant_id,
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Venta no encontrada.")

    items = await conn.fetch(
        """
        SELECT * FROM sale_items
        WHERE tenant_id = $1 AND sale_id = $2
        ORDER BY id ASC
        """,
        tenant_id,
        sale_id,
    )

    total = money(sale["total_amount"])
    cash = money(sale["paid_cash_amount"])
    transfer = money(sale["paid_transfer_amount"])
    fiado = money(sale["fiado_amount"])
    wallet = money(sale["paid_wallet_amount"])
    received_now = money(cash + transfer + wallet)

    # Generar enlace opcional gratuito wa.me para comprobante digital
    wa_link = ""
    if sale.get("customer_phone"):
        digits = "".join(ch for ch in str(sale["customer_phone"]) if ch.isdigit())
        if digits:
            msg = (
                f"Hola {sale.get('customer_name') or ''}! Comprobante de compra #{sale['sale_number']}:\n"
                f"Total: {sale['currency']} {money_float(total):,.2f}\n"
                f"Pagado hoy: {sale['currency']} {money_float(received_now):,.2f}"
            )
            if fiado > Decimal("0"):
                msg += f"\nAnotado en cuenta (fiado): {sale['currency']} {money_float(fiado):,.2f}"
            msg += "\n¡Gracias por tu compra!"
            wa_link = f"https://wa.me/{digits}?text={quote(msg)}"

    return {
        "id": sale["id"],
        "sale_number": int(sale["sale_number"]),
        "idempotency_key": sale["idempotency_key"],
        "status": sale["status"],
        "customer_id": sale["customer_id"],
        "customer_name": sale.get("customer_name") or "",
        "customer_phone": sale.get("customer_phone") or "",
        "subtotal_amount": money_float(sale["subtotal_amount"]),
        "discount_amount": money_float(sale["discount_amount"]),
        "total_amount": money_float(total),
        "total_cost_snapshot": money_float(sale["total_cost_snapshot"]),
        "gross_profit_snapshot": money_float(sale["gross_profit_snapshot"]),
        "paid_cash_amount": money_float(cash),
        "paid_transfer_amount": money_float(transfer),
        "paid_wallet_amount": money_float(wallet),
        "money_received_amount": money_float(received_now),
        "fiado_amount": money_float(fiado),
        "payment_summary": sale["payment_summary"],
        "has_missing_cost": bool(sale["has_missing_cost"]),
        "notes": sale["notes"] or "",
        "voided_reason": sale.get("voided_reason") or "",
        "voided_at": sale.get("voided_at"),
        "seller_name": sale.get("seller_name") or "",
        "whatsapp_receipt_url": wa_link,
        "created_at": sale["created_at"],
        "items": [
            {
                "id": it["id"],
                "product_id": it["product_id"],
                "description": it["description"],
                "unit": it["unit"],
                "quantity": qty_float(it["quantity"]),
                "unit_price_snapshot": money_float(it["unit_price_snapshot"]),
                "unit_cost_snapshot": money_float(it["unit_cost_snapshot"]),
                "cost_known": bool(it["cost_known"]),
                "line_subtotal": money_float(it["line_subtotal"]),
                "line_cost_total": money_float(it["line_cost_total"]),
                "line_gross_profit": money_float(it["line_gross_profit"]),
            }
            for it in items
        ],
    }


async def create_sale(ctx: AuthContext, data: SaleCreateRequest) -> dict[str, Any]:
    """
    Registra una venta de forma atómica, idempotente y segura ante concurrencia:
    - Bloquea productos (FOR UPDATE / BEGIN IMMEDIATE) evitando stock negativo.
    - Congela el costo histórico vigente al momento exacto de la operación.
    - Admite productos del catálogo y productos ocasionales/manuales rápidos.
    - Desglosa pagos simples o combinados (Efectivo + Transferencia + Fiado).
    - Distingue dinero efectivamente recibido (caja/banco) de deuda pendiente (cuenta corriente).
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    idem_key = data.idempotency_key.strip()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        # 1. Control de idempotencia contra reintentos o doble toque en celular
        existing_id = await conn.fetchval(
            "SELECT id FROM sales WHERE tenant_id = $1 AND idempotency_key = $2",
            ctx.tenant_id,
            idem_key,
        )
        if existing_id:
            return await _fetch_sale_detail(conn, ctx.tenant_id, existing_id)

        # 2. Agrupar demanda por producto y bloquear filas en orden determinístico (evita deadlocks)
        demanded_by_product: dict[str, Decimal] = defaultdict(lambda: Decimal("0.000"))
        for item in data.items:
            if item.product_id:
                demanded_by_product[item.product_id] += qty(item.quantity)

        locked_products: dict[str, dict[str, Any]] = {}
        for pid in sorted(demanded_by_product.keys()):
            prod = await conn.fetchrow(
                "SELECT * FROM products WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                pid,
                ctx.tenant_id,
            )
            if not prod or not bool(prod["is_active"]):
                raise HTTPException(
                    status_code=404,
                    detail="Uno de los productos seleccionados no existe o está inactivo.",
                )
            avail = qty(prod["stock_qty"])
            needed = demanded_by_product[pid]
            if avail < needed:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Stock insuficiente para '{prod['name']}': "
                        f"disponible {qty_float(avail)} {prod['unit']}, solicitado {qty_float(needed)}."
                    ),
                )
            locked_products[pid] = prod

        # 3. Preparar renglones y calcular totales con Decimal exacto
        prepared_lines: list[dict[str, Any]] = []
        subtotal = Decimal("0.00")
        total_cost = Decimal("0.00")
        has_missing_cost = False

        for item in data.items:
            q = qty(item.quantity)
            if item.product_id:
                prod = locked_products[item.product_id]
                unit_price = (
                    money(item.unit_price)
                    if item.unit_price is not None
                    else money(prod["sale_price"])
                )
                unit_cost = money(prod["cost_price"])
                cost_known = True
                desc = (item.description or "").strip() or prod["name"]
                unit_name = prod["unit"]
            else:
                # Venta rápida de producto ocasional/manual sin creación previa
                desc = (item.description or "").strip()
                if not desc:
                    desc = "Venta rápida / Varios"
                if item.unit_price is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"El artículo manual '{desc}' requiere indicar su precio de venta.",
                    )
                unit_price = money(item.unit_price)
                if item.manual_unit_cost is not None:
                    unit_cost = money(item.manual_unit_cost)
                    cost_known = True
                else:
                    unit_cost = Decimal("0.00")
                    cost_known = False
                    has_missing_cost = True
                unit_name = item.unit or "unidad"

            line_sub = money(q * unit_price)
            line_cost = money(q * unit_cost)
            line_profit = money(line_sub - line_cost)

            subtotal += line_sub
            total_cost += line_cost
            prepared_lines.append(
                {
                    "product_id": item.product_id,
                    "description": desc,
                    "unit": unit_name,
                    "quantity": q,
                    "unit_price_snapshot": unit_price,
                    "unit_cost_snapshot": unit_cost,
                    "cost_known": cost_known,
                    "line_subtotal": line_sub,
                    "line_cost_total": line_cost,
                    "line_gross_profit": line_profit,
                }
            )

        discount = money(data.discount_amount)
        if discount > subtotal:
            raise HTTPException(
                status_code=400,
                detail="El descuento no puede superar el subtotal de la venta.",
            )

        total_amount = money(subtotal - discount)
        gross_profit = money(total_amount - total_cost)

        paid_cash = money(data.paid_cash_amount)
        paid_transfer = money(data.paid_transfer_amount)
        fiado_amt = money(data.fiado_amount)
        paid_wallet = money(data.paid_wallet_amount)
        sum_payments = money(paid_cash + paid_transfer + paid_wallet + fiado_amt)

        if sum_payments != total_amount:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La suma de medios de pago (${money_float(sum_payments):,.2f}) "
                    f"no coincide con el total de la venta (${money_float(total_amount):,.2f})."
                ),
            )

        if fiado_amt > Decimal("0") and not data.customer_id:
            raise HTTPException(
                status_code=400,
                detail="Para registrar un monto fiado debés seleccionar o crear un cliente.",
            )

        # Validar cliente si existe o si hay fiado
        customer_row: dict[str, Any] | None = None
        if data.customer_id:
            customer_row = await conn.fetchrow(
                "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                data.customer_id,
                ctx.tenant_id,
            )
            if not customer_row or not bool(customer_row["is_active"]):
                raise HTTPException(
                    status_code=404,
                    detail="El cliente seleccionado no pertenece a tu comercio o está inactivo.",
                )
            if fiado_amt > Decimal("0"):
                limit_amt = money(customer_row["credit_limit"])
                projected = money(money(customer_row["current_balance"]) + fiado_amt)
                if limit_amt > Decimal("0") and projected > limit_amt and ctx.role != "owner":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"El fiado supera el límite acordado del cliente "
                            f"(${money_float(limit_amt):,.2f}). Solicitar autorización del titular."
                        ),
                    )

        # Obtener numeración correlativa por comercio
        last_num = await conn.fetchval(
            "SELECT COALESCE(MAX(sale_number), 0) FROM sales WHERE tenant_id = $1",
            ctx.tenant_id,
        )
        next_sale_num = int(last_num or 0) + 1
        sale_id = new_id()
        payment_summary = _determine_payment_summary(paid_cash, paid_transfer, fiado_amt, paid_wallet)

        # 4. Insertar cabecera de venta
        await conn.execute(
            """
            INSERT INTO sales (
                id, tenant_id, sale_number, idempotency_key, customer_id,
                status, subtotal_amount, discount_amount, total_amount,
                total_cost_snapshot, gross_profit_snapshot,
                paid_cash_amount, paid_transfer_amount, fiado_amount,
                payment_summary, has_missing_cost, notes, created_by, created_at, paid_wallet_amount
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            """,
            sale_id,
            ctx.tenant_id,
            next_sale_num,
            idem_key,
            data.customer_id,
            "completed",
            subtotal,
            discount,
            total_amount,
            total_cost,
            gross_profit,
            paid_cash,
            paid_transfer,
            fiado_amt,
            payment_summary,
            has_missing_cost,
            data.notes.strip(),
            ctx.user_id,
            now,
            paid_wallet,
        )

        # 5. Insertar ítems y descontar stock trazablemente
        running_stock: dict[str, Decimal] = {
            pid: qty(pr["stock_qty"]) for pid, pr in locked_products.items()
        }

        for line in prepared_lines:
            await conn.execute(
                """
                INSERT INTO sale_items (
                    id, tenant_id, sale_id, product_id, description, unit,
                    quantity, unit_price_snapshot, unit_cost_snapshot, cost_known,
                    line_subtotal, line_cost_total, line_gross_profit
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                new_id(),
                ctx.tenant_id,
                sale_id,
                line["product_id"],
                line["description"],
                line["unit"],
                line["quantity"],
                line["unit_price_snapshot"],
                line["unit_cost_snapshot"],
                line["cost_known"],
                line["line_subtotal"],
                line["line_cost_total"],
                line["line_gross_profit"],
            )

            pid = line["product_id"]
            if pid:
                before = running_stock[pid]
                delta = -line["quantity"]
                after = qty(before + delta)
                running_stock[pid] = after

                await conn.execute(
                    "UPDATE products SET stock_qty = $1, updated_at = $2 WHERE id = $3 AND tenant_id = $4",
                    after,
                    now,
                    pid,
                    ctx.tenant_id,
                )
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
                    pid,
                    "sale",
                    delta,
                    before,
                    after,
                    line["unit_cost_snapshot"],
                    "sale",
                    sale_id,
                    f"Venta #{next_sale_num}",
                    ctx.user_id,
                    now,
                )

        # 6. Registrar ingresos efectivos en Caja / Transferencias (solo dinero efectivamente cobrado)
        open_session_id = await conn.fetchval(
            "SELECT id FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            ctx.tenant_id,
        )
        if (paid_cash > 0 or paid_transfer > 0 or paid_wallet > 0) and not open_session_id:
            raise HTTPException(status_code=409, detail="Abrí un turno de caja antes de registrar movimientos de dinero.")
        await record_channel_movements(
            conn,
            tenant_id=ctx.tenant_id,
            session_id=open_session_id,
            key_prefix="sale",
            idem_key=idem_key,
            movement_type="sale_income",
            direction="in",
            amounts={"efectivo": paid_cash, "transferencia": paid_transfer, "billetera": paid_wallet},
            category="Ventas",
            reference_type="sale",
            reference_id=sale_id,
            description=f"Cobro Venta #{next_sale_num}",
            user_id=ctx.user_id,
            now=now,
        )

        # 7. Registrar saldo pendiente en Cuenta Corriente (Fiado — NO ingresa a caja)
        if fiado_amt > Decimal("0") and customer_row is not None:
            bal_before = money(customer_row["current_balance"])
            bal_after = money(bal_before + fiado_amt)
            oldest_unpaid = customer_row.get("oldest_unpaid_at") or now
            if conn.backend == "postgres" and isinstance(oldest_unpaid, str):
                oldest_unpaid = datetime.fromisoformat(oldest_unpaid)

            await conn.execute(
                """
                UPDATE customers
                SET current_balance = $1, oldest_unpaid_at = $2, updated_at = $3
                WHERE id = $4 AND tenant_id = $5
                """,
                bal_after,
                oldest_unpaid,
                now,
                customer_row["id"],
                ctx.tenant_id,
            )

            await conn.execute(
                """
                INSERT INTO customer_ledger (
                    id, tenant_id, customer_id, entry_type, idempotency_key,
                    amount, signed_delta, remaining_unpaid, balance_before, balance_after,
                    payment_method, cash_amount, transfer_amount,
                    sale_id, due_date, notes, created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                """,
                new_id(),
                ctx.tenant_id,
                customer_row["id"],
                "fiado_charge",
                f"sale-fiado-{idem_key}",
                fiado_amt,
                fiado_amt,
                fiado_amt,
                bal_before,
                bal_after,
                "fiado",
                Decimal("0.00"),
                Decimal("0.00"),
                sale_id,
                data.due_date,
                f"Fiado en Venta #{next_sale_num}",
                ctx.user_id,
                now,
            )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="sale.create",
            entity_type="sale",
            entity_id=sale_id,
            details={
                "sale_number": next_sale_num,
                "total": str(total_amount),
                "cash": str(paid_cash),
                "transfer": str(paid_transfer),
                "fiado": str(fiado_amt),
            },
            ip_address=ctx.ip_address,
        )

        return await _fetch_sale_detail(conn, ctx.tenant_id, sale_id)


async def list_sales(
    ctx: AuthContext, limit: int = 100, status_filter: str = ""
) -> list[dict[str, Any]]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        if status_filter:
            rows = await conn.fetch(
                """
                SELECT s.*, c.name AS customer_name
                FROM sales s
                LEFT JOIN customers c ON c.id = s.customer_id AND c.tenant_id = s.tenant_id
                WHERE s.tenant_id = $1 AND s.status = $2
                ORDER BY s.created_at DESC
                LIMIT $3
                """,
                ctx.tenant_id,
                status_filter,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT s.*, c.name AS customer_name
                FROM sales s
                LEFT JOIN customers c ON c.id = s.customer_id AND c.tenant_id = s.tenant_id
                WHERE s.tenant_id = $1
                ORDER BY s.created_at DESC
                LIMIT $2
                """,
                ctx.tenant_id,
                limit,
            )

    return [
        {
            "id": r["id"],
            "sale_number": int(r["sale_number"]),
            "status": r["status"],
            "customer_id": r["customer_id"],
            "customer_name": r.get("customer_name") or "",
            "total_amount": money_float(r["total_amount"]),
            "total_cost_snapshot": money_float(r["total_cost_snapshot"]),
            "gross_profit_snapshot": money_float(r["gross_profit_snapshot"]),
            "paid_cash_amount": money_float(r["paid_cash_amount"]),
            "paid_transfer_amount": money_float(r["paid_transfer_amount"]),
            "paid_wallet_amount": money_float(r["paid_wallet_amount"]),
            "money_received_amount": money_float(
                money(r["paid_cash_amount"]) + money(r["paid_transfer_amount"]) + money(r["paid_wallet_amount"])
            ),
            "fiado_amount": money_float(r["fiado_amount"]),
            "payment_summary": r["payment_summary"],
            "has_missing_cost": bool(r["has_missing_cost"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def get_sale(ctx: AuthContext, sale_id: str) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        return await _fetch_sale_detail(conn, ctx.tenant_id, sale_id)


async def void_sale(
    ctx: AuthContext, sale_id: str, data: SaleVoidRequest
) -> dict[str, Any]:
    """
    Anula una venta sin eliminar físicamente ningún registro:
    revierte stock, caja y deuda pendiente de fiado con trazabilidad completa.
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        sale = await conn.fetchrow(
            "SELECT * FROM sales WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            sale_id,
            ctx.tenant_id,
        )
        if not sale:
            raise HTTPException(status_code=404, detail="Venta no encontrada.")
        if sale["status"] == "voided":
            raise HTTPException(status_code=409, detail="Esta venta ya fue anulada.")

        # Evitar anulación incompleta: si ya se imputó algún cobro posterior,
        # no sabemos por qué canal ni cómo devolver ese dinero. Requiere conciliación manual.
        if money(sale["fiado_amount"]) > 0:
            charge = await conn.fetchrow(
                "SELECT remaining_unpaid FROM customer_ledger WHERE tenant_id = $1 AND sale_id = $2 AND entry_type = 'fiado_charge'",
                ctx.tenant_id, sale_id,
            )
            if not charge or money(charge["remaining_unpaid"]) != money(sale["fiado_amount"]):
                raise HTTPException(status_code=409, detail="No se puede anular una venta con fiado parcialmente cobrado; requiere conciliación manual.")
        items = await conn.fetch(
            "SELECT * FROM sale_items WHERE tenant_id = $1 AND sale_id = $2",
            ctx.tenant_id,
            sale_id,
        )

        # Revertir stock de productos catalogados
        for it in items:
            pid = it["product_id"]
            if pid:
                prod = await conn.fetchrow(
                    "SELECT * FROM products WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                    pid,
                    ctx.tenant_id,
                )
                if prod:
                    before = qty(prod["stock_qty"])
                    delta = qty(it["quantity"])
                    after = qty(before + delta)
                    await conn.execute(
                        "UPDATE products SET stock_qty = $1, updated_at = $2 WHERE id = $3 AND tenant_id = $4",
                        after,
                        now,
                        pid,
                        ctx.tenant_id,
                    )
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
                        pid,
                        "sale_void",
                        delta,
                        before,
                        after,
                        money(it["unit_cost_snapshot"]),
                        "sale_void",
                        sale_id,
                        f"Anulación Venta #{sale['sale_number']}: {data.reason.strip()}",
                        ctx.user_id,
                        now,
                    )

        # Revertir dinero recibido en caja / transferencias
        open_session_id = await conn.fetchval(
            "SELECT id FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            ctx.tenant_id,
        )
        if (money(sale["paid_cash_amount"]) > 0 or money(sale["paid_transfer_amount"]) > 0 or money(sale["paid_wallet_amount"]) > 0) and not open_session_id:
            raise HTTPException(status_code=409, detail="Abrí un turno de caja antes de registrar movimientos de dinero.")
        await record_channel_movements(
            conn,
            tenant_id=ctx.tenant_id,
            session_id=open_session_id,
            key_prefix="void",
            idem_key=sale_id,
            movement_type="sale_void_outflow",
            direction="out",
            amounts={
                "efectivo": money(sale["paid_cash_amount"]),
                "transferencia": money(sale["paid_transfer_amount"]),
                "billetera": money(sale["paid_wallet_amount"]),
            },
            category="Anulación Venta",
            reference_type="sale_void",
            reference_id=sale_id,
            description=f"Devolución por anulación Venta #{sale['sale_number']}",
            user_id=ctx.user_id,
            now=now,
        )

        # Revertir saldo pendiente de fiado vinculado
        fiado_amt = money(sale["fiado_amount"])
        if fiado_amt > Decimal("0") and sale["customer_id"]:
            cust = await conn.fetchrow(
                "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                sale["customer_id"],
                ctx.tenant_id,
            )
            if cust:
                charge_row = await conn.fetchrow(
                    """
                    SELECT id, remaining_unpaid FROM customer_ledger
                    WHERE tenant_id = $1 AND sale_id = $2 AND entry_type = 'fiado_charge'
                    """,
                    ctx.tenant_id,
                    sale_id,
                )
                reversible = (
                    min(money(cust["current_balance"]), money(charge_row["remaining_unpaid"]))
                    if charge_row
                    else min(money(cust["current_balance"]), fiado_amt)
                )
                if reversible > Decimal("0"):
                    bal_before = money(cust["current_balance"])
                    bal_after = money(bal_before - reversible)
                    if charge_row:
                        await conn.execute(
                            "UPDATE customer_ledger SET remaining_unpaid = $1 WHERE id = $2 AND tenant_id = $3",
                            money(money(charge_row["remaining_unpaid"]) - reversible),
                            charge_row["id"],
                            ctx.tenant_id,
                        )
                    oldest = await conn.fetchval(
                        """
                        SELECT MIN(created_at) FROM customer_ledger
                        WHERE tenant_id = $1 AND customer_id = $2 AND remaining_unpaid > 0
                        """,
                        ctx.tenant_id,
                        cust["id"],
                    )
                    await conn.execute(
                        "UPDATE customers SET current_balance = $1, oldest_unpaid_at = $2, updated_at = $3 WHERE id = $4 AND tenant_id = $5",
                        bal_after,
                        oldest if bal_after > Decimal("0") else None,
                        now,
                        cust["id"],
                        ctx.tenant_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO customer_ledger (
                            id, tenant_id, customer_id, entry_type, idempotency_key,
                            amount, signed_delta, remaining_unpaid, balance_before, balance_after,
                            payment_method, cash_amount, transfer_amount,
                            sale_id, notes, created_by, created_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                        """,
                        new_id(),
                        ctx.tenant_id,
                        cust["id"],
                        "sale_void_reversal",
                        f"void-fiado-{sale_id}",
                        reversible,
                        -reversible,
                        Decimal("0.00"),
                        bal_before,
                        bal_after,
                        "reversion",
                        Decimal("0.00"),
                        Decimal("0.00"),
                        sale_id,
                        f"Reversión fiado por anulación Venta #{sale['sale_number']}",
                        ctx.user_id,
                        now,
                    )

        await conn.execute(
            "UPDATE sales SET status = 'voided', voided_reason = $1, voided_at = $2 WHERE id = $3 AND tenant_id = $4",
            data.reason.strip(),
            now,
            sale_id,
            ctx.tenant_id,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="sale.void",
            entity_type="sale",
            entity_id=sale_id,
            details={"sale_number": sale["sale_number"], "reason": data.reason.strip()},
            ip_address=ctx.ip_address,
        )

        return await _fetch_sale_detail(conn, ctx.tenant_id, sale_id)
