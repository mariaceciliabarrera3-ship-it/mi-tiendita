from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from app.config import money, money_float, new_id, utc_now
from app.db import get_db
from app.schemas import (
    CustomerCreateRequest,
    CustomerPaymentRequest,
    CustomerUpdateRequest,
)
from app.security import AuthContext, log_audit
from app.services.cash import record_channel_movements


def _parse_iso_dt(val: Any) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _build_whatsapp_reminder(
    store_name: str, customer_name: str, phone: str, balance: Decimal, currency: str = "ARS"
) -> str:
    msg = (
        f"Hola {customer_name}, ¿cómo estás? Te escribimos de *{store_name}* "
        f"para recordarte amablemente que tenés un saldo pendiente en tu cuenta de "
        f"*{currency} {money_float(balance):,.2f}*. ¡Muchas gracias!"
    )
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits:
        return f"https://wa.me/{digits}?text={quote(msg)}"
    return f"https://wa.me/?text={quote(msg)}"


def _enrich_customer(
    row: dict[str, Any],
    default_alert_days: int,
    default_alert_amount: Decimal,
    store_name: str,
    overdue_due_date: str | None = None,
    last_payment_at: str | None = None,
    currency: str = "ARS",
) -> dict[str, Any]:
    bal = money(row["current_balance"])
    oldest_dt = _parse_iso_dt(row.get("oldest_unpaid_at"))
    now = utc_now()
    last_payment = _parse_iso_dt(last_payment_at)
    since = max(x for x in (oldest_dt, last_payment) if x is not None) if (oldest_dt or last_payment) else None
    days_unpaid = max(0, (now - since).days) if bal > 0 and since else 0

    eff_days = (
        int(row["alert_days_override"])
        if row.get("alert_days_override") is not None
        else default_alert_days
    )
    eff_amt = (
        money(row["alert_amount_override"])
        if row.get("alert_amount_override") is not None
        else default_alert_amount
    )

    alert_reasons: list[str] = []
    if bal > Decimal("0"):
        if overdue_due_date:
            alert_reasons.append(f"Vencimiento cumplido ({overdue_due_date})")
        if days_unpaid >= eff_days:
            alert_reasons.append(f"{days_unpaid} días de antigüedad (límite {eff_days}d)")
        if eff_amt > Decimal("0") and bal >= eff_amt:
            alert_reasons.append(
                f"Supera monto de aviso (${money_float(eff_amt):,.2f})"
            )

    reminder_msg = (
        f"Hola {row['name']}, te recordamos amablemente desde {store_name} "
        f"tu saldo pendiente de {currency} {money_float(bal):,.2f}. ¡Gracias!"
        if bal > Decimal("0")
        else ""
    )

    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row.get("phone") or "",
        "address": row.get("address") or "",
        "notes": row.get("notes") or "",
        "credit_limit": money_float(row["credit_limit"]),
        "current_balance": money_float(bal),
        "days_unpaid": days_unpaid,
        "oldest_unpaid_at": row.get("oldest_unpaid_at"),
        "alert_days_override": row.get("alert_days_override"),
        "alert_amount_override": (
            money_float(row["alert_amount_override"])
            if row.get("alert_amount_override") is not None
            else None
        ),
        "needs_reminder": len(alert_reasons) > 0,
        "alert_reasons": alert_reasons,
        "reminder_message": reminder_msg,
        "whatsapp_reminder_url": (
            _build_whatsapp_reminder(store_name, row["name"], row.get("phone") or "", bal, currency)
            if bal > Decimal("0")
            else ""
        ),
        "is_active": bool(row.get("is_active", True)),
        "created_at": row.get("created_at"),
    }


async def list_customers(
    ctx: AuthContext, search: str = "", debtors_only: bool = False
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        tenant = await conn.fetchrow(
            "SELECT name, debt_alert_days, debt_alert_amount, timezone, currency FROM tenants WHERE id = $1",
            ctx.tenant_id,
        )
        rows = await conn.fetch(
            "SELECT * FROM customers WHERE tenant_id = $1 ORDER BY current_balance DESC, name ASC",
            ctx.tenant_id,
        )
        today_str = utc_now().astimezone(ZoneInfo(tenant["timezone"] if tenant else "UTC")).strftime("%Y-%m-%d")
        last_payments = await conn.fetch("SELECT customer_id, MAX(created_at) AS last_payment FROM customer_ledger WHERE tenant_id=$1 AND entry_type='payment' GROUP BY customer_id", ctx.tenant_id)
        overdue_rows = await conn.fetch(
            """
            SELECT customer_id, MIN(due_date) AS earliest_due
            FROM customer_ledger
            WHERE tenant_id = $1 AND remaining_unpaid > 0
              AND due_date IS NOT NULL AND due_date <> '' AND due_date <= $2
            GROUP BY customer_id
            """,
            ctx.tenant_id,
            today_str,
        )

    overdue_map = {r["customer_id"]: r["earliest_due"] for r in overdue_rows}
    payments_map = {r["customer_id"]: r["last_payment"] for r in last_payments}
    default_days = int(tenant["debt_alert_days"]) if tenant else 15
    default_amt = money(tenant["debt_alert_amount"]) if tenant else Decimal("20000.00")
    store_name = tenant["name"] if tenant else (ctx.tenant_name or "Mi Tiendita")

    total_receivable = Decimal("0.00")
    debtors_count = 0
    alerts_list: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    q = search.strip().lower()

    for r in rows:
        if not bool(r["is_active"]):
            continue
        bal = money(r["current_balance"])
        if bal > Decimal("0"):
            total_receivable += bal
            debtors_count += 1

        enriched = _enrich_customer(
            r,
            default_days,
            default_amt,
            store_name,
            overdue_due_date=overdue_map.get(r["id"]),
            last_payment_at=payments_map.get(r["id"]),
            currency=tenant["currency"] if tenant else "ARS",
        )
        if enriched["needs_reminder"]:
            alerts_list.append(enriched)

        if debtors_only and bal <= Decimal("0"):
            continue
        if q and q not in f"{r['name']} {r.get('phone') or ''} {r.get('address') or ''}".lower():
            continue
        items.append(enriched)

    return {
        "items": items,
        "summary": {
            "total_pending_fiados": money_float(total_receivable),
            "debtors_count": debtors_count,
            "alerts_count": len(alerts_list),
            "default_alert_days": default_days,
            "default_alert_amount": money_float(default_amt),
        },
        "alerts": alerts_list,
    }


async def create_customer(
    ctx: AuthContext, data: CustomerCreateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    cust_id = new_id()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        tenant = await conn.fetchrow(
            "SELECT name, debt_alert_days, debt_alert_amount FROM tenants WHERE id = $1",
            ctx.tenant_id,
        )
        await conn.execute(
            """
            INSERT INTO customers (
                id, tenant_id, name, phone, address, notes,
                credit_limit, current_balance, alert_days_override,
                alert_amount_override, is_active, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            """,
            cust_id,
            ctx.tenant_id,
            data.name.strip(),
            data.phone.strip(),
            data.address.strip(),
            data.notes.strip(),
            money(data.credit_limit),
            Decimal("0.00"),
            data.alert_days_override,
            money(data.alert_amount_override) if data.alert_amount_override is not None else None,
            True,
            now,
            now,
        )
        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="customer.create",
            entity_type="customer",
            entity_id=cust_id,
            details={"name": data.name.strip()},
            ip_address=ctx.ip_address,
        )
        created = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2",
            cust_id,
            ctx.tenant_id,
        )

    assert created is not None
    return _enrich_customer(
        created,
        int(tenant["debt_alert_days"]) if tenant else 15,
        money(tenant["debt_alert_amount"]) if tenant else Decimal("20000.00"),
        tenant["name"] if tenant else "Mi Tiendita",
    )


async def update_customer(
    ctx: AuthContext, customer_id: str, data: CustomerUpdateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        curr = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            customer_id,
            ctx.tenant_id,
        )
        if not curr:
            raise HTTPException(status_code=404, detail="Cliente no encontrado.")

        tenant = await conn.fetchrow(
            "SELECT name, debt_alert_days, debt_alert_amount FROM tenants WHERE id = $1",
            ctx.tenant_id,
        )

        new_name = data.name.strip() if data.name is not None else curr["name"]
        new_phone = data.phone.strip() if data.phone is not None else (curr["phone"] or "")
        new_addr = data.address.strip() if data.address is not None else (curr["address"] or "")
        new_notes = data.notes.strip() if data.notes is not None else (curr["notes"] or "")
        new_limit = (
            money(data.credit_limit)
            if data.credit_limit is not None
            else money(curr["credit_limit"])
        )
        new_days = (
            data.alert_days_override
            if data.alert_days_override is not None
            else curr.get("alert_days_override")
        )
        new_amt = (
            money(data.alert_amount_override)
            if data.alert_amount_override is not None
            else curr.get("alert_amount_override")
        )
        new_active = (
            data.is_active if data.is_active is not None else bool(curr["is_active"])
        )
        # Un cliente inactivo desaparece de listados y totales de fiado: no se puede ocultar una deuda viva.
        if not new_active and bool(curr["is_active"]) and money(curr["current_balance"]) > Decimal("0"):
            raise HTTPException(
                status_code=409,
                detail="No se puede desactivar un cliente con deuda pendiente. Registrá primero el cobro.",
            )

        await conn.execute(
            """
            UPDATE customers
            SET name = $1, phone = $2, address = $3, notes = $4,
                credit_limit = $5, alert_days_override = $6, alert_amount_override = $7,
                is_active = $8, updated_at = $9
            WHERE id = $10 AND tenant_id = $11
            """,
            new_name,
            new_phone,
            new_addr,
            new_notes,
            new_limit,
            new_days,
            new_amt,
            new_active,
            now,
            customer_id,
            ctx.tenant_id,
        )
        updated = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2",
            customer_id,
            ctx.tenant_id,
        )

    assert updated is not None
    return _enrich_customer(
        updated,
        int(tenant["debt_alert_days"]) if tenant else 15,
        money(tenant["debt_alert_amount"]) if tenant else Decimal("20000.00"),
        tenant["name"] if tenant else "Mi Tiendita",
    )


async def get_customer_detail(ctx: AuthContext, customer_id: str) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2",
            customer_id,
            ctx.tenant_id,
        )
        if not cust:
            raise HTTPException(status_code=404, detail="Cliente no encontrado.")

        tenant = await conn.fetchrow(
            "SELECT name, debt_alert_days, debt_alert_amount FROM tenants WHERE id = $1",
            ctx.tenant_id,
        )
        lifetime = await conn.fetchval("SELECT COALESCE(SUM(total_amount),0) FROM sales WHERE tenant_id=$1 AND customer_id=$2 AND status='completed'",ctx.tenant_id,customer_id)
        ledger = await conn.fetch(
            """
            SELECT l.*, u.full_name AS actor_name
            FROM customer_ledger l
            LEFT JOIN users u ON u.id = l.created_by
            WHERE l.tenant_id = $1 AND l.customer_id = $2
            ORDER BY l.created_at DESC
            LIMIT 200
            """,
            ctx.tenant_id,
            customer_id,
        )

    return {
        "lifetime_purchases": money_float(lifetime),
        "customer": _enrich_customer(
            cust,
            int(tenant["debt_alert_days"]) if tenant else 15,
            money(tenant["debt_alert_amount"]) if tenant else Decimal("20000.00"),
            tenant["name"] if tenant else "Mi Tiendita",
        ),
        "ledger": [
            {
                "id": l["id"],
                "entry_type": l["entry_type"],
                "amount": money_float(l["amount"]),
                "signed_delta": money_float(l["signed_delta"]),
                "remaining_unpaid": money_float(l["remaining_unpaid"]),
                "balance_before": money_float(l["balance_before"]),
                "balance_after": money_float(l["balance_after"]),
                "payment_method": l["payment_method"],
                "cash_amount": money_float(l["cash_amount"]),
                "transfer_amount": money_float(l["transfer_amount"]),
                "wallet_amount": money_float(l["wallet_amount"]),
                "sale_id": l["sale_id"],
                "due_date": l.get("due_date") or "",
                "notes": l["notes"] or "",
                "actor_name": l.get("actor_name") or "",
                "created_at": l["created_at"],
            }
            for l in ledger
        ],
    }


async def register_customer_payment(
    ctx: AuthContext, customer_id: str, data: CustomerPaymentRequest
) -> dict[str, Any]:
    """
    Registra un cobro parcial o total de deuda de cliente (fiado):
    - NO duplica ventas (es un cobro financiero, no una nueva venta).
    - NUNCA elimina el saldo restante cuando el pago es parcial.
    - Imputa por antigüedad (FIFO) sobre cargos pendientes para actualizar alertas reales.
    - Ingresa el dinero efectivamente cobrado a Caja (efectivo y/o transferencia).
    """
    assert ctx.tenant_id is not None
    cash_amt = money(data.cash_amount)
    transfer_amt = money(data.transfer_amount)
    wallet_amt = money(data.wallet_amount)
    total_payment = money(cash_amt + transfer_amt + wallet_amt)

    if total_payment <= Decimal("0"):
        raise HTTPException(
            status_code=400,
            detail="El monto del cobro debe ser mayor a cero.",
        )

    idem_key = data.idempotency_key.strip()
    now = utc_now()
    db_mgr = get_db()

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        # Idempotencia contra cobros duplicados
        existing_entry = await conn.fetchrow(
            "SELECT * FROM customer_ledger WHERE tenant_id = $1 AND idempotency_key = $2",
            ctx.tenant_id,
            idem_key,
        )
        if existing_entry:
            return {
                "already_processed": True,
                "ledger_entry_id": existing_entry["id"],
                "amount_paid": money_float(existing_entry["amount"]),
                "balance_before": money_float(existing_entry["balance_before"]),
                "balance_after": money_float(existing_entry["balance_after"]),
            }

        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
            customer_id,
            ctx.tenant_id,
        )
        if not cust:
            raise HTTPException(status_code=404, detail="Cliente no encontrado.")

        bal_before = money(cust["current_balance"])
        if bal_before <= Decimal("0"):
            raise HTTPException(
                status_code=409,
                detail="Este cliente no registra deuda pendiente en este momento.",
            )
        if total_payment > bal_before:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El cobro ingresado (${money_float(total_payment):,.2f}) supera "
                    f"la deuda pendiente actual (${money_float(bal_before):,.2f})."
                ),
            )

        bal_after = money(bal_before - total_payment)

        # Imputación FIFO sobre los fiados abiertos más antiguos
        open_charges = await conn.fetch(
            """
            SELECT id, remaining_unpaid, created_at
            FROM customer_ledger
            WHERE tenant_id = $1 AND customer_id = $2 AND remaining_unpaid > 0
            ORDER BY created_at ASC, id ASC
            """,
            ctx.tenant_id,
            customer_id,
        )
        left_to_apply = total_payment
        for ch in open_charges:
            if left_to_apply <= Decimal("0"):
                break
            rem = money(ch["remaining_unpaid"])
            applied = min(rem, left_to_apply)
            new_rem = money(rem - applied)
            left_to_apply = money(left_to_apply - applied)
            await conn.execute(
                "UPDATE customer_ledger SET remaining_unpaid = $1 WHERE id = $2 AND tenant_id = $3",
                new_rem,
                ch["id"],
                ctx.tenant_id,
            )

        oldest_remaining = None
        if bal_after > Decimal("0"):
            oldest_remaining = await conn.fetchval(
                """
                SELECT MIN(created_at) FROM customer_ledger
                WHERE tenant_id = $1 AND customer_id = $2 AND remaining_unpaid > 0
                """,
                ctx.tenant_id,
                customer_id,
            )

        await conn.execute(
            """
            UPDATE customers
            SET current_balance = $1, oldest_unpaid_at = $2, updated_at = $3
            WHERE id = $4 AND tenant_id = $5
            """,
            bal_after,
            oldest_remaining,
            now,
            customer_id,
            ctx.tenant_id,
        )

        methods = [name for name, amt in (("efectivo",cash_amt),("transferencia",transfer_amt),("billetera",wallet_amt)) if amt > 0]
        pm = methods[0] if len(methods)==1 else "mixto"

        entry_id = new_id()
        await conn.execute(
            """
            INSERT INTO customer_ledger (
                id, tenant_id, customer_id, entry_type, idempotency_key,
                amount, signed_delta, remaining_unpaid, balance_before, balance_after,
                payment_method, cash_amount, transfer_amount,
                sale_id, notes, created_by, created_at, wallet_amount
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            """,
            entry_id,
            ctx.tenant_id,
            customer_id,
            "payment",
            idem_key,
            total_payment,
            -total_payment,
            Decimal("0.00"),
            bal_before,
            bal_after,
            pm,
            cash_amt,
            transfer_amt,
            None,
            data.notes.strip() or f"Cobro de cuenta corriente ({pm})",
            ctx.user_id,
            now,
            wallet_amt,
        )

        # Registrar ingreso en Caja / Transferencias como cobro de fiado (NO venta nueva)
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
            key_prefix="fiado-pay",
            idem_key=idem_key,
            movement_type="fiado_collection",
            direction="in",
            amounts={"efectivo": cash_amt, "transferencia": transfer_amt, "billetera": wallet_amt},
            category="Cobro de Fiados",
            reference_type="customer_payment",
            reference_id=entry_id,
            description=f"Cobro fiado - {cust['name']}",
            user_id=ctx.user_id,
            now=now,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="customer.payment",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "paid": str(total_payment),
                "balance_before": str(bal_before),
                "balance_after": str(bal_after),
            },
            ip_address=ctx.ip_address,
        )

    return {
        "already_processed": False,
        "ledger_entry_id": entry_id,
        "customer_id": customer_id,
        "customer_name": cust["name"],
        "amount_paid": money_float(total_payment),
        "balance_before": money_float(bal_before),
        "balance_after": money_float(bal_after),
    }
