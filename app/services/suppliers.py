from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from app.config import money, money_float, new_id, qty, utc_now
from app.db import get_db
from app.schemas import (
    PurchaseCreateRequest,
    SupplierCreateRequest,
    SupplierPaymentRequest,
)
from app.security import AuthContext, log_audit
from app.services.cash import record_channel_movements


async def list_suppliers(ctx: AuthContext) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM suppliers WHERE tenant_id = $1 AND is_active = $2 ORDER BY current_debt DESC, name ASC",
            ctx.tenant_id,
            True,
        )

    total_debt = sum((money(r["current_debt"]) for r in rows), Decimal("0.00"))
    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "tax_id": r["tax_id"] or "",
                "phone": r["phone"] or "",
                "email": r["email"] or "",
                "address": r["address"] or "",
                "notes": r["notes"] or "",
                "current_debt": money_float(r["current_debt"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "total_supplier_debt": money_float(total_debt),
    }


async def create_supplier(
    ctx: AuthContext, data: SupplierCreateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    sup_id = new_id()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO suppliers (
                id, tenant_id, name, tax_id, phone, email, address,
                notes, current_debt, is_active, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            sup_id,
            ctx.tenant_id,
            data.name.strip(),
            data.tax_id.strip(),
            data.phone.strip(),
            data.email.strip(),
            data.address.strip(),
            data.notes.strip(),
            Decimal("0.00"),
            True,
            now,
            now,
        )
        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="supplier.create",
            entity_type="supplier",
            entity_id=sup_id,
            details={"name": data.name.strip()},
            ip_address=ctx.ip_address,
        )
        row = await conn.fetchrow(
            "SELECT * FROM suppliers WHERE id = $1 AND tenant_id = $2",
            sup_id,
            ctx.tenant_id,
        )

    assert row is not None
    return {
        "id": row["id"],
        "name": row["name"],
        "tax_id": row["tax_id"] or "",
        "phone": row["phone"] or "",
        "email": row["email"] or "",
        "address": row["address"] or "",
        "notes": row["notes"] or "",
        "current_debt": money_float(row["current_debt"]),
        "created_at": row["created_at"],
    }


async def create_purchase(
    ctx: AuthContext, data: PurchaseCreateRequest
) -> dict[str, Any]:
    """
    Registra una compra de mercadería:
    - Incrementa el stock de productos catalogados en inventory_movements ('purchase').
    - Actualiza opcionalmente el costo vigente y precio futuro de venta SIN modificar
      retroactivamente el costo histórico ni la ganancia de ventas ya ocurridas.
    - Registra egreso en Caja/Transferencia por lo pagado hoy y deuda con proveedor por lo pendiente.
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    idem_key = data.idempotency_key.strip()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        existing_id = await conn.fetchval(
            "SELECT id FROM purchases WHERE tenant_id = $1 AND idempotency_key = $2",
            ctx.tenant_id,
            idem_key,
        )
        if existing_id:
            existing_p = await conn.fetchrow(
                "SELECT * FROM purchases WHERE id = $1 AND tenant_id = $2",
                existing_id,
                ctx.tenant_id,
            )
            assert existing_p is not None
            return {
                "id": existing_p["id"],
                "purchase_number": int(existing_p["purchase_number"]),
                "total_amount": money_float(existing_p["total_amount"]),
                "payment_status": existing_p["payment_status"],
                "already_processed": True,
            }

        supplier = await conn.fetchrow(
            "SELECT * FROM suppliers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            data.supplier_id,
            ctx.tenant_id,
        )
        if not supplier or not bool(supplier["is_active"]):
            raise HTTPException(status_code=404, detail="Proveedor no encontrado.")

        total_amount = Decimal("0.00")
        prepared_items: list[dict[str, Any]] = []

        for it in data.items:
            q = qty(it.quantity)
            uc = money(it.unit_cost)
            new_sp = money(it.new_sale_price) if it.new_sale_price is not None else None
            line_tot = money(q * uc)
            total_amount += line_tot

            prod_row = None
            if it.product_id:
                prod_row = await conn.fetchrow(
                    "SELECT * FROM products WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                    it.product_id,
                    ctx.tenant_id,
                )
                if not prod_row:
                    raise HTTPException(
                        status_code=404,
                        detail="Uno de los productos de la compra no pertenece a tu comercio.",
                    )
            desc = (it.description or "").strip() or (
                prod_row["name"] if prod_row else "Insumos / Mercadería"
            )
            prepared_items.append(
                {
                    "product_row": prod_row,
                    "product_id": it.product_id,
                    "description": desc,
                    "quantity": q,
                    "unit_cost": uc,
                    "new_sale_price": new_sp,
                    "line_total": line_tot,
                }
            )

        paid_cash = money(data.paid_cash_amount)
        paid_transfer = money(data.paid_transfer_amount)
        pending_debt = money(data.pending_debt_amount)
        paid_wallet = money(data.paid_wallet_amount)
        sum_financing = money(paid_cash + paid_transfer + paid_wallet + pending_debt)

        if sum_financing != total_amount:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La suma de pagos y saldo pendiente (${money_float(sum_financing):,.2f}) "
                    f"debe coincidir con el total de la compra (${money_float(total_amount):,.2f})."
                ),
            )

        if pending_debt == Decimal("0"):
            pay_status = "paid"
        elif paid_cash == Decimal("0") and paid_transfer == Decimal("0") and paid_wallet == 0:
            pay_status = "pending"
        else:
            pay_status = "partial"

        last_num = await conn.fetchval(
            "SELECT COALESCE(MAX(purchase_number), 0) FROM purchases WHERE tenant_id = $1",
            ctx.tenant_id,
        )
        next_num = int(last_num or 0) + 1
        purchase_id = new_id()

        await conn.execute(
            """
            INSERT INTO purchases (
                id, tenant_id, purchase_number, idempotency_key, supplier_id,
                invoice_number, total_amount, paid_cash_amount, paid_transfer_amount,
                pending_debt_amount, payment_status, updated_costs, notes, created_by, created_at, paid_wallet_amount
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            """,
            purchase_id,
            ctx.tenant_id,
            next_num,
            idem_key,
            supplier["id"],
            data.invoice_number.strip(),
            total_amount,
            paid_cash,
            paid_transfer,
            pending_debt,
            pay_status,
            data.update_product_costs,
            data.notes.strip(),
            ctx.user_id,
            now,
            paid_wallet,
        )

        for item in prepared_items:
            await conn.execute(
                """
                INSERT INTO purchase_items (
                    id, tenant_id, purchase_id, product_id, description,
                    quantity, unit_cost, new_sale_price, line_total
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                new_id(),
                ctx.tenant_id,
                purchase_id,
                item["product_id"],
                item["description"],
                item["quantity"],
                item["unit_cost"],
                item["new_sale_price"],
                item["line_total"],
            )

            if item["product_id"]:
                # Re-leer stock actualizado por si el mismo producto viene dos veces en la factura
                p_curr = await conn.fetchrow(
                    "SELECT * FROM products WHERE id = $1 AND tenant_id = $2",
                    item["product_id"],
                    ctx.tenant_id,
                )
                assert p_curr is not None
                before = qty(p_curr["stock_qty"])
                delta = item["quantity"]
                after = qty(before + delta)
                old_cost = money(p_curr["cost_price"])
                old_price = money(p_curr["sale_price"])
                target_cost = item["unit_cost"] if data.update_product_costs else old_cost
                target_price = (
                    item["new_sale_price"]
                    if item["new_sale_price"] is not None
                    else old_price
                )

                await conn.execute(
                    """
                    UPDATE products
                    SET stock_qty = $1, cost_price = $2, sale_price = $3, updated_at = $4
                    WHERE id = $5 AND tenant_id = $6
                    """,
                    after,
                    target_cost,
                    target_price,
                    now,
                    item["product_id"],
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
                    item["product_id"],
                    "purchase",
                    delta,
                    before,
                    after,
                    item["unit_cost"],
                    "purchase",
                    purchase_id,
                    f"Compra #{next_num} - {supplier['name']}",
                    ctx.user_id,
                    now,
                )

                if target_cost != old_cost or target_price != old_price:
                    await conn.execute(
                        """
                        INSERT INTO product_price_history (
                            id, tenant_id, product_id, old_cost, new_cost,
                            old_sale_price, new_sale_price, reason, changed_by, created_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        """,
                        new_id(),
                        ctx.tenant_id,
                        item["product_id"],
                        old_cost,
                        target_cost,
                        old_price,
                        target_price,
                        f"Reposición Compra #{next_num}",
                        ctx.user_id,
                        now,
                    )

        # Registrar salida real de dinero en Caja / Transferencia
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
            key_prefix="purch",
            idem_key=idem_key,
            movement_type="purchase_payment",
            direction="out",
            amounts={"efectivo": paid_cash, "transferencia": paid_transfer, "billetera": paid_wallet},
            category="Compra Mercadería",
            reference_type="purchase",
            reference_id=purchase_id,
            description=f"Pago Compra #{next_num} ({supplier['name']})",
            user_id=ctx.user_id,
            now=now,
        )

        # Registrar deuda pendiente con proveedor
        if pending_debt > Decimal("0"):
            debt_before = money(supplier["current_debt"])
            debt_after = money(debt_before + pending_debt)
            await conn.execute(
                "UPDATE suppliers SET current_debt = $1, updated_at = $2 WHERE id = $3 AND tenant_id = $4",
                debt_after,
                now,
                supplier["id"],
                ctx.tenant_id,
            )
            await conn.execute(
                """
                INSERT INTO supplier_ledger (
                    id, tenant_id, supplier_id, entry_type, idempotency_key,
                    amount, signed_delta, balance_before, balance_after,
                    payment_method, cash_amount, transfer_amount,
                    purchase_id, notes, created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                """,
                new_id(),
                ctx.tenant_id,
                supplier["id"],
                "purchase_debt",
                f"purch-debt-{idem_key}",
                pending_debt,
                pending_debt,
                debt_before,
                debt_after,
                "cuenta_corriente",
                Decimal("0.00"),
                Decimal("0.00"),
                purchase_id,
                f"Saldo pendiente Compra #{next_num}",
                ctx.user_id,
                now,
            )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="purchase.create",
            entity_type="purchase",
            entity_id=purchase_id,
            details={
                "purchase_number": next_num,
                "total": str(total_amount),
                "pending_debt": str(pending_debt),
            },
            ip_address=ctx.ip_address,
        )

    return {
        "id": purchase_id,
        "purchase_number": next_num,
        "supplier_name": supplier["name"],
        "total_amount": money_float(total_amount),
        "paid_cash_amount": money_float(paid_cash),
        "paid_transfer_amount": money_float(paid_transfer),
        "paid_wallet_amount": money_float(paid_wallet),
        "pending_debt_amount": money_float(pending_debt),
        "payment_status": pay_status,
        "already_processed": False,
    }


async def list_purchases(
    ctx: AuthContext, status_filter: str = ""
) -> list[dict[str, Any]]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        if status_filter:
            rows = await conn.fetch(
                """
                SELECT p.*, s.name AS supplier_name
                FROM purchases p
                JOIN suppliers s ON s.id = p.supplier_id AND s.tenant_id = p.tenant_id
                WHERE p.tenant_id = $1 AND p.payment_status = $2
                ORDER BY p.created_at DESC
                LIMIT 100
                """,
                ctx.tenant_id,
                status_filter,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT p.*, s.name AS supplier_name
                FROM purchases p
                JOIN suppliers s ON s.id = p.supplier_id AND s.tenant_id = p.tenant_id
                WHERE p.tenant_id = $1
                ORDER BY p.created_at DESC
                LIMIT 100
                """,
                ctx.tenant_id,
            )

    return [
        {
            "id": r["id"],
            "purchase_number": int(r["purchase_number"]),
            "supplier_id": r["supplier_id"],
            "supplier_name": r["supplier_name"],
            "invoice_number": r["invoice_number"] or "",
            "total_amount": money_float(r["total_amount"]),
            "paid_cash_amount": money_float(r["paid_cash_amount"]),
            "paid_transfer_amount": money_float(r["paid_transfer_amount"]),
            "paid_wallet_amount": money_float(r["paid_wallet_amount"]),
            "pending_debt_amount": money_float(r["pending_debt_amount"]),
            "payment_status": r["payment_status"],
            "notes": r["notes"] or "",
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def register_supplier_payment(
    ctx: AuthContext, supplier_id: str, data: SupplierPaymentRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    cash_amt = money(data.cash_amount)
    transfer_amt = money(data.transfer_amount)
    wallet_amt = money(data.wallet_amount)
    total_pay = money(cash_amt + transfer_amt + wallet_amt)
    if total_pay <= Decimal("0"):
        raise HTTPException(
            status_code=400, detail="El monto del pago debe ser mayor a cero."
        )

    idem_key = data.idempotency_key.strip()
    now = utc_now()
    db_mgr = get_db()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM supplier_ledger WHERE tenant_id = $1 AND idempotency_key = $2",
            ctx.tenant_id,
            idem_key,
        )
        if existing:
            return {
                "already_processed": True,
                "amount_paid": money_float(existing["amount"]),
                "debt_after": money_float(existing["balance_after"]),
            }

        sup = await conn.fetchrow(
            "SELECT * FROM suppliers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            supplier_id,
            ctx.tenant_id,
        )
        if not sup:
            raise HTTPException(status_code=404, detail="Proveedor no encontrado.")

        debt_before = money(sup["current_debt"])
        if total_pay > debt_before:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El pago (${money_float(total_pay):,.2f}) supera la deuda registrada "
                    f"con este proveedor (${money_float(debt_before):,.2f})."
                ),
            )
        debt_after = money(debt_before - total_pay)

        await conn.execute(
            "UPDATE suppliers SET current_debt = $1, updated_at = $2 WHERE id = $3 AND tenant_id = $4",
            debt_after,
            now,
            supplier_id,
            ctx.tenant_id,
        )

        methods = [name for name, amt in (("efectivo",cash_amt),("transferencia",transfer_amt),("billetera",wallet_amt)) if amt > 0]
        pm = methods[0] if len(methods)==1 else "mixto"
        entry_id = new_id()
        await conn.execute(
            """
            INSERT INTO supplier_ledger (
                id, tenant_id, supplier_id, entry_type, idempotency_key,
                amount, signed_delta, balance_before, balance_after,
                payment_method, cash_amount, transfer_amount,
                purchase_id, notes, created_by, created_at, wallet_amount
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            """,
            entry_id,
            ctx.tenant_id,
            supplier_id,
            "payment",
            idem_key,
            total_pay,
            -total_pay,
            debt_before,
            debt_after,
            pm,
            cash_amt,
            transfer_amt,
            None,
            data.notes.strip() or f"Pago a proveedor ({pm})",
            ctx.user_id,
            now,
            wallet_amt,
        )

        # Imputar pagos posteriores FIFO: el pendiente visible de cada compra
        # cambia sin alterar el movimiento original del libro mayor del proveedor.
        open_purchases = await conn.fetch(
            "SELECT id, pending_debt_amount FROM purchases WHERE tenant_id = $1 AND supplier_id = $2 AND pending_debt_amount > 0 ORDER BY created_at ASC, id ASC",
            ctx.tenant_id, supplier_id,
        )
        remaining = total_pay
        for purchase in open_purchases:
            if remaining <= 0:
                break
            pending = money(purchase["pending_debt_amount"])
            applied = min(pending, remaining)
            pending = money(pending - applied)
            remaining = money(remaining - applied)
            await conn.execute(
                "UPDATE purchases SET pending_debt_amount = $1, payment_status = $2 WHERE tenant_id = $3 AND id = $4",
                pending, "paid" if pending == 0 else "partial", ctx.tenant_id, purchase["id"],
            )
        if remaining != 0:
            raise HTTPException(status_code=409, detail="Deuda de proveedor no concilia con sus compras; operación cancelada.")

        open_session_id = await conn.fetchval(
            "SELECT id FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            ctx.tenant_id,
        )
        if not open_session_id:
            raise HTTPException(status_code=409, detail="Abrí un turno de caja antes de registrar movimientos de dinero.")
        await record_channel_movements(
            conn,
            tenant_id=ctx.tenant_id,
            session_id=open_session_id,
            key_prefix="sup-pay",
            idem_key=idem_key,
            movement_type="supplier_payment",
            direction="out",
            amounts={"efectivo": cash_amt, "transferencia": transfer_amt, "billetera": wallet_amt},
            category="Pago a Proveedores",
            reference_type="supplier_payment",
            reference_id=entry_id,
            description=f"Pago a proveedor {sup['name']}",
            user_id=ctx.user_id,
            now=now,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="supplier.payment",
            entity_type="supplier",
            entity_id=supplier_id,
            details={"paid": str(total_pay), "debt_after": str(debt_after)},
            ip_address=ctx.ip_address,
        )

    return {
        "already_processed": False,
        "supplier_id": supplier_id,
        "supplier_name": sup["name"],
        "amount_paid": money_float(total_pay),
        "debt_before": money_float(debt_before),
        "debt_after": money_float(debt_after),
    }
