from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from app.config import money, money_float, new_id, utc_now
from app.db import DBConnectionWrapper, get_db
from app.schemas import (
    CashMovementCreateRequest,
    CashSessionCloseRequest,
    CashSessionOpenRequest,
)
from app.security import AuthContext, log_audit


CHANNELS = ("efectivo", "transferencia", "billetera")
_CHANNEL_SLUG = {"efectivo": "cash", "transferencia": "transfer", "billetera": "wallet"}


async def insert_cash_movement(
    conn: DBConnectionWrapper,
    *,
    tenant_id: str,
    session_id: str | None,
    idempotency_key: str,
    movement_type: str,
    channel: str,
    direction: str,
    amount: Decimal,
    category: str,
    reference_type: str,
    reference_id: str,
    description: str,
    user_id: str,
    now: Any,
) -> None:
    """Único punto de escritura en cash_movements (ventas, cobros, compras, pagos, caja)."""
    await conn.execute(
        """
        INSERT INTO cash_movements (
            id, tenant_id, cash_session_id, idempotency_key,
            movement_type, channel, direction, amount,
            category, reference_type, reference_id, description,
            created_by, created_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        """,
        new_id(),
        tenant_id,
        session_id,
        idempotency_key,
        movement_type,
        channel,
        direction,
        amount,
        category,
        reference_type,
        reference_id,
        description,
        user_id,
        now,
    )


async def record_channel_movements(
    conn: DBConnectionWrapper,
    *,
    tenant_id: str,
    session_id: str | None,
    key_prefix: str,
    idem_key: str,
    movement_type: str,
    direction: str,
    amounts: dict[str, Decimal],
    category: str,
    reference_type: str,
    reference_id: str,
    description: str,
    user_id: str,
    now: Any,
) -> None:
    """
    Registra un movimiento por cada canal con importe > 0 (efectivo, transferencia, billetera).
    La clave idempotente de cada fila es "<key_prefix>-<cash|transfer|wallet>-<idem_key>".
    """
    for channel in CHANNELS:
        amt = amounts.get(channel, Decimal("0.00"))
        if amt > Decimal("0"):
            await insert_cash_movement(
                conn,
                tenant_id=tenant_id,
                session_id=session_id,
                idempotency_key=f"{key_prefix}-{_CHANNEL_SLUG[channel]}-{idem_key}",
                movement_type=movement_type,
                channel=channel,
                direction=direction,
                amount=amt,
                category=category,
                reference_type=reference_type,
                reference_id=reference_id,
                description=f"{description} ({channel})",
                user_id=user_id,
                now=now,
            )


async def _compute_session_expected(
    conn: DBConnectionWrapper, tenant_id: str, session_row: dict[str, Any]
) -> dict[str, Decimal]:
    movs = await conn.fetch(
        """
        SELECT channel, direction, movement_type, amount
        FROM cash_movements
        WHERE tenant_id = $1 AND cash_session_id = $2
        """,
        tenant_id,
        session_row["id"],
    )
    opening = money(session_row["opening_cash_amount"])
    cash_in = Decimal("0.00")
    cash_out = Decimal("0.00")
    transfer_in = Decimal("0.00")
    transfer_out = Decimal("0.00")
    wallet_in = Decimal("0.00")
    wallet_out = Decimal("0.00")

    for m in movs:
        if m["movement_type"] == "opening":
            continue
        amt = money(m["amount"])
        if m["channel"] == "efectivo":
            if m["direction"] == "in":
                cash_in += amt
            else:
                cash_out += amt
        elif m["channel"] == "transferencia":
            if m["direction"] == "in": transfer_in += amt
            else: transfer_out += amt
        else:
            if m["direction"] == "in": wallet_in += amt
            else: wallet_out += amt

    expected_cash = money(opening + cash_in - cash_out)
    return {
        "opening": opening,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "expected_cash": expected_cash,
        "transfer_in": transfer_in,
        "transfer_out": transfer_out,
        "net_transfer": money(transfer_in - transfer_out),
        "wallet_in": wallet_in, "wallet_out": wallet_out,
    }


async def get_cash_status(ctx: AuthContext) -> dict[str, Any]:
    """
    Devuelve el estado integral de Caja y Tesorería del comercio:
    - Efectivo físico teórico en cajón vs Transferencias recibidas/enviadas.
    - Apertura/Cierre activo y arqueos recientes.
    - JAMÁS mezcla fiados pendientes de cobro con dinero real.
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()

    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        open_sess = await conn.fetchrow(
            "SELECT * FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            ctx.tenant_id,
        )
        session_summary = None
        if open_sess:
            calc = await _compute_session_expected(conn, ctx.tenant_id, open_sess)
            session_summary = {
                "id": open_sess["id"],
                "status": open_sess["status"],
                "opened_at": open_sess["opened_at"],
                "opening_cash_amount": money_float(calc["opening"]),
                "session_cash_in": money_float(calc["cash_in"]),
                "session_cash_out": money_float(calc["cash_out"]),
                "expected_physical_cash": money_float(calc["expected_cash"]),
                "session_transfer_in": money_float(calc["transfer_in"]),
                "session_transfer_out": money_float(calc["transfer_out"]),
                "session_net_transfer": money_float(calc["net_transfer"]),
                "session_wallet_in": money_float(calc["wallet_in"]),
                "session_wallet_out": money_float(calc["wallet_out"]),
                "notes": open_sess["notes"] or "",
            }

        # Saldos acumulados globales por canal (efectivo físico vs transferencias)
        all_movs = await conn.fetch(
            "SELECT channel, direction, amount FROM cash_movements WHERE tenant_id = $1",
            ctx.tenant_id,
        )
        phys_in = Decimal("0.00")
        phys_out = Decimal("0.00")
        transf_in = Decimal("0.00")
        transf_out = Decimal("0.00")
        wallet_in = Decimal("0.00")
        wallet_out = Decimal("0.00")
        for m in all_movs:
            amt = money(m["amount"])
            if m["channel"] == "efectivo":
                if m["direction"] == "in":
                    phys_in += amt
                else:
                    phys_out += amt
            elif m["channel"] == "transferencia":
                if m["direction"] == "in": transf_in += amt
                else: transf_out += amt
            else:
                if m["direction"] == "in": wallet_in += amt
                else: wallet_out += amt

        recent_movs = await conn.fetch(
            """
            SELECT m.*, u.full_name AS actor_name
            FROM cash_movements m
            LEFT JOIN users u ON u.id = m.created_by
            WHERE m.tenant_id = $1
            ORDER BY m.created_at DESC
            LIMIT 100
            """,
            ctx.tenant_id,
        )
        past_sessions = await conn.fetch(
            """
            SELECT * FROM cash_sessions
            WHERE tenant_id = $1 AND status = 'closed'
            ORDER BY closed_at DESC
            LIMIT 20
            """,
            ctx.tenant_id,
        )

    return {
        "open_session": session_summary,
        "global_balances": {
            "physical_cash_balance": money_float(phys_in - phys_out),
            "transfer_balance": money_float(transf_in - transf_out),
            "wallet_balance": money_float(wallet_in - wallet_out),
            "wallet_received": money_float(wallet_in),
            "total_available_money": money_float(
                (phys_in - phys_out) + (transf_in - transf_out) + (wallet_in - wallet_out)
            ),
            "total_transfer_received": money_float(transf_in),
            "total_transfer_sent": money_float(transf_out),
        },
        "movements": [
            {
                "id": m["id"],
                "movement_type": m["movement_type"],
                "channel": m["channel"],
                "direction": m["direction"],
                "amount": money_float(m["amount"]),
                "category": m["category"],
                "description": m["description"],
                "actor_name": m.get("actor_name") or "",
                "created_at": m["created_at"],
            }
            for m in recent_movs
        ],
        "closed_sessions": [
            {
                "id": s["id"],
                "opened_at": s["opened_at"],
                "closed_at": s["closed_at"],
                "opening_cash_amount": money_float(s["opening_cash_amount"]),
                "expected_cash_amount": money_float(s["expected_cash_amount"]),
                "counted_cash_amount": money_float(s["counted_cash_amount"]),
                "cash_difference_amount": money_float(s["cash_difference_amount"]),
                "total_transfer_in": money_float(s["total_transfer_in"]),
                "notes": s["notes"] or "",
            }
            for s in past_sessions
        ],
    }


async def open_cash_session(
    ctx: AuthContext, data: CashSessionOpenRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    opening = money(data.opening_cash_amount)

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        existing_open = await conn.fetchval(
            "SELECT id FROM cash_sessions WHERE tenant_id = $1 AND status = 'open'",
            ctx.tenant_id,
        )
        if existing_open:
            raise HTTPException(
                status_code=409,
                detail="Ya hay una caja abierta en este momento. Cerrala antes de abrir un nuevo turno.",
            )

        sess_id = new_id()
        await conn.execute(
            """
            INSERT INTO cash_sessions (
                id, tenant_id, status, opening_cash_amount,
                opened_by, opened_at, notes
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)
            """,
            sess_id,
            ctx.tenant_id,
            "open",
            opening,
            ctx.user_id,
            now,
            data.notes.strip(),
        )

        # Conciliar el efectivo contado al abrir con el saldo acumulado anterior.
        # Se registra la diferencia como movimiento trazable, nunca como ingreso por ventas.
        prev = await conn.fetchval("SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE -amount END),0) FROM cash_movements WHERE tenant_id=$1 AND channel='efectivo'", ctx.tenant_id)
        delta = money(opening - money(prev))
        if delta != 0:
            await insert_cash_movement(
                conn,
                tenant_id=ctx.tenant_id,
                session_id=sess_id,
                idempotency_key=f"open-init-{sess_id}",
                movement_type="opening",
                channel="efectivo",
                direction="in" if delta > 0 else "out",
                amount=abs(delta),
                category="Apertura de caja",
                reference_type="cash_session",
                reference_id=sess_id,
                description=f"Conciliación al abrir: efectivo anterior {money(prev)}, contado {opening}",
                user_id=ctx.user_id,
                now=now,
            )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="cash.open",
            entity_type="cash_session",
            entity_id=sess_id,
            details={"opening_cash": str(opening)},
            ip_address=ctx.ip_address,
        )

    return await get_cash_status(ctx)


async def close_cash_session(
    ctx: AuthContext, data: CashSessionCloseRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    counted = money(data.counted_cash_amount)

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        sess = await conn.fetchrow(
            "SELECT * FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1 FOR UPDATE",
            ctx.tenant_id,
        )
        if not sess:
            raise HTTPException(
                status_code=404, detail="No hay ninguna caja abierta para cerrar."
            )

        calc = await _compute_session_expected(conn, ctx.tenant_id, sess)
        expected = calc["expected_cash"]
        diff = money(counted - expected)

        # Todo cierre con diferencia concilia el libro con el conteo físico.
        if diff != Decimal("0"):
            direction = "in" if diff > Decimal("0") else "out"
            abs_diff = abs(diff)
            await insert_cash_movement(
                conn,
                tenant_id=ctx.tenant_id,
                session_id=sess["id"],
                idempotency_key=f"close-diff-{sess['id']}",
                movement_type="cash_difference_adjustment",
                channel="efectivo",
                direction=direction,
                amount=abs_diff,
                category="Diferencia de Caja",
                reference_type="cash_session",
                reference_id=sess["id"],
                description=(
                    f"Sobrante de caja al cierre (+${money_float(abs_diff):,.2f})"
                    if diff > Decimal("0")
                    else f"Faltante de caja al cierre (-${money_float(abs_diff):,.2f})"
                ),
                user_id=ctx.user_id,
                now=now,
            )

        await conn.execute(
            """
            UPDATE cash_sessions
            SET status = 'closed',
                expected_cash_amount = $1,
                counted_cash_amount = $2,
                cash_difference_amount = $3,
                total_transfer_in = $4,
                total_transfer_out = $5,
                closed_by = $6,
                closed_at = $7,
                notes = $8
            WHERE id = $9 AND tenant_id = $10
            """,
            expected,
            counted,
            diff,
            calc["transfer_in"],
            calc["transfer_out"],
            ctx.user_id,
            now,
            (data.notes.strip() or sess["notes"] or ""),
            sess["id"],
            ctx.tenant_id,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="cash.close",
            entity_type="cash_session",
            entity_id=sess["id"],
            details={
                "expected": str(expected),
                "counted": str(counted),
                "difference": str(diff),
            },
            ip_address=ctx.ip_address,
        )

    return {
        "session_id": sess["id"],
        "expected_cash_amount": money_float(expected),
        "counted_cash_amount": money_float(counted),
        "cash_difference_amount": money_float(diff),
        "total_transfer_in": money_float(calc["transfer_in"]),
    }


async def register_manual_cash_movement(
    ctx: AuthContext, data: CashMovementCreateRequest
) -> dict[str, Any]:
    """Registra ingresos extra, gastos operativos o retiros personales del dueño."""
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    amt = money(data.amount)
    idem_key = data.idempotency_key.strip()
    direction = "in" if data.movement_type == "other_income" else "out"

    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM cash_movements WHERE tenant_id = $1 AND idempotency_key = $2",
            ctx.tenant_id,
            idem_key,
        )
        if existing:
            return {
                "id": existing["id"],
                "already_processed": True,
                "amount": money_float(existing["amount"]),
            }

        open_session_id = await conn.fetchval(
            "SELECT id FROM cash_sessions WHERE tenant_id = $1 AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
            ctx.tenant_id,
        )
        if not open_session_id:
            raise HTTPException(status_code=409, detail="Abrí un turno de caja antes de registrar movimientos de dinero.")
        mov_id = new_id()
        await insert_cash_movement(
            conn,
            tenant_id=ctx.tenant_id,
            session_id=open_session_id,
            idempotency_key=idem_key,
            movement_type=data.movement_type,
            channel=data.channel,
            direction=direction,
            amount=amt,
            category=data.category.strip() or "General",
            reference_type="manual_cash",
            reference_id=mov_id,
            description=data.description.strip(),
            user_id=ctx.user_id,
            now=now,
        )

        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action=f"cash.{data.movement_type}",
            entity_type="cash_movement",
            entity_id=mov_id,
            details={
                "channel": data.channel,
                "direction": direction,
                "amount": str(amt),
                "category": data.category,
            },
            ip_address=ctx.ip_address,
        )

    return {
        "id": mov_id,
        "already_processed": False,
        "movement_type": data.movement_type,
        "channel": data.channel,
        "direction": direction,
        "amount": money_float(amt),
    }
