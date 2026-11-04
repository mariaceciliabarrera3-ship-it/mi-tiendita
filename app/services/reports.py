import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any

from openpyxl import Workbook

from app.config import money, money_float, qty, qty_float, utc_now
from app.db import get_db
from app.security import AuthContext
from app.xlsx_safe import neutralize_formulas


def _extract_ym(iso_str: Any, zone: str = "UTC") -> str:
    try:
        d = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo(zone)).strftime("%Y-%m")
    except (ValueError, TypeError):
        return ""


def _prev_month(ym: str) -> str:
    try:
        year, month = map(int, ym.split("-"))
        if month == 1:
            return f"{year - 1:04d}-12"
        return f"{year:04d}-{month - 1:02d}"
    except Exception:
        return ""


async def get_financial_results(
    ctx: AuthContext, period_month: str = ""
) -> dict[str, Any]:
    """
    Diferencia con rigor contable y lenguaje llano:
    1) Resultado económico devengado (Ventas, Costo histórico CMV, Ganancia bruta, Gastos, Resultado operativo estimado).
    2) Dinero efectivamente recibido vs Dinero pendiente de cobro (Fiados).
    3) Advertencias transparentes cuando faltan datos (ej. costos manuales omitidos o gastos sin cargar).
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()

    async with db_mgr.connection(tenant_id=ctx.tenant_id) as conn:
        zone = await conn.fetchval("SELECT timezone FROM tenants WHERE id=$1", ctx.tenant_id) or "UTC"
        sales_rows = await conn.fetch(
            "SELECT * FROM sales WHERE tenant_id = $1 AND status = 'completed'",
            ctx.tenant_id,
        )
        cash_rows = await conn.fetch(
            "SELECT * FROM cash_movements WHERE tenant_id = $1",
            ctx.tenant_id,
        )
        pending_fiados_val = await conn.fetchval(
            "SELECT COALESCE(SUM(current_balance), 0) FROM customers WHERE tenant_id = $1 AND is_active = $2",
            ctx.tenant_id,
            True,
        )
        pending_suppliers_val = await conn.fetchval(
            "SELECT COALESCE(SUM(current_debt), 0) FROM suppliers WHERE tenant_id = $1 AND is_active = $2",
            ctx.tenant_id,
            True,
        )

    if period_month:
        sales_filtered = [
            s for s in sales_rows if _extract_ym(s["created_at"], zone) == period_month
        ]
        cash_filtered = [
            c for c in cash_rows if _extract_ym(c["created_at"], zone) == period_month
        ]
    else:
        sales_filtered = sales_rows
        cash_filtered = cash_rows

    sales_total = Decimal("0.00")
    cogs_historical = Decimal("0.00")
    gross_profit = Decimal("0.00")
    sales_cash_in = Decimal("0.00")
    sales_transfer_in = Decimal("0.00")
    sales_wallet_in = Decimal("0.00")
    sales_fiado_granted = Decimal("0.00")
    sales_with_missing_cost = 0

    for s in sales_filtered:
        sales_total += money(s["total_amount"])
        cogs_historical += money(s["total_cost_snapshot"])
        gross_profit += money(s["gross_profit_snapshot"])
        sales_cash_in += money(s["paid_cash_amount"])
        sales_transfer_in += money(s["paid_transfer_amount"])
        sales_wallet_in += money(s["paid_wallet_amount"])
        sales_fiado_granted += money(s["fiado_amount"])
        if bool(s["has_missing_cost"]):
            sales_with_missing_cost += 1

    operating_expenses = Decimal("0.00")
    owner_withdrawals = Decimal("0.00")
    fiado_collections = Decimal("0.00")
    other_incomes = Decimal("0.00")
    supplier_and_purchase_outflows = Decimal("0.00")
    collected_cash_channel = Decimal("0.00")
    collected_transfer_channel = Decimal("0.00")
    collected_wallet_channel = Decimal("0.00")

    for m in cash_filtered:
        amt = money(m["amount"])
        mtype = m["movement_type"]
        if mtype == "operating_expense":
            operating_expenses += amt
        elif mtype == "owner_withdrawal":
            owner_withdrawals += amt
        elif mtype == "fiado_collection":
            fiado_collections += amt
        elif mtype == "other_income":
            other_incomes += amt
        elif mtype in ("purchase_payment", "supplier_payment"):
            supplier_and_purchase_outflows += amt

        if m["direction"] == "in" and mtype in (
            "sale_income",
            "fiado_collection",
            "other_income",
        ):
            if m["channel"] == "efectivo":
                collected_cash_channel += amt
            elif m["channel"] == "transferencia":
                collected_transfer_channel += amt
            else:
                collected_wallet_channel += amt
        elif m["direction"] == "out" and mtype == "sale_void_outflow":
            if m["channel"] == "efectivo":
                collected_cash_channel -= amt
            elif m["channel"] == "transferencia":
                collected_transfer_channel -= amt
            else:
                collected_wallet_channel -= amt

    estimated_operating_result = money(gross_profit - operating_expenses)
    money_collected_from_sales = money(sales_cash_in + sales_transfer_in + sales_wallet_in)
    total_money_collected = money(
        collected_cash_channel + collected_transfer_channel + collected_wallet_channel
    )
    pending_fiados = money(pending_fiados_val)
    pending_suppliers = money(pending_suppliers_val)

    warnings: list[str] = []
    is_estimated = False
    if sales_with_missing_cost > 0:
        is_estimated = True
        warnings.append(
            f"Hay {sales_with_missing_cost} venta(s) con artículos manuales rápidos donde no se indicó el costo de compra. "
            "La ganancia bruta y el resultado operativo son estimados por exceso hasta registrar dichos costos."
        )
    if len(sales_filtered) > 0 and operating_expenses == Decimal("0.00"):
        is_estimated = True
        warnings.append(
            "Aún no registraste gastos operativos (luz, alquiler, fletes, bolsas) en el período. "
            "El resultado operativo coincide provisoriamente con la ganancia bruta."
        )

    gross_margin_pct = (
        float((gross_profit / sales_total * Decimal("100")).quantize(Decimal("0.01")))
        if sales_total > Decimal("0")
        else 0.0
    )

    return {
        "period": period_month or "historico_completo",
        "economic_results": {
            "sales_count": len(sales_filtered),
            "sales_total": money_float(sales_total),
            "historical_cogs": money_float(cogs_historical),
            "gross_profit": money_float(gross_profit),
            "gross_margin_pct": gross_margin_pct,
            "operating_expenses": money_float(operating_expenses),
            "estimated_operating_result": money_float(estimated_operating_result),
            "owner_withdrawals_info": money_float(owner_withdrawals),
            "is_estimated": is_estimated,
            "estimation_explanations": warnings,
        },
        "cash_and_receivables": {
            "money_collected_in_sales": money_float(money_collected_from_sales),
            "fiado_collections_received": money_float(fiado_collections),
            "other_incomes_received": money_float(other_incomes),
            "total_money_effectively_collected": money_float(total_money_collected),
            "collected_in_physical_cash": money_float(collected_cash_channel),
            "collected_by_transfer": money_float(collected_transfer_channel),
            "collected_by_wallet": money_float(collected_wallet_channel),
            "supplier_and_merchandise_payments": money_float(
                supplier_and_purchase_outflows
            ),
            "fiado_granted_in_period": money_float(sales_fiado_granted),
            "total_pending_to_collect_fiados": money_float(pending_fiados),
            "total_pending_supplier_debt": money_float(pending_suppliers),
        },
    }


async def verify_financial_consistency(ctx: AuthContext) -> dict[str, Any]:
    """
    Prueba matemática integral (Escenario G):
    Verifica que ventas, cobros, pagos, saldos de clientes, deudas con proveedores,
    movimientos de caja y kárdex de inventario coincidan al centavo.
    """
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    tid = ctx.tenant_id
    issues: list[str] = []

    async with db_mgr.connection(tenant_id=tid) as conn:
        # 1. Cada venta completada: total == efectivo + transferencia + fiado, y ganancia == total - costo
        sales = await conn.fetch(
            "SELECT * FROM sales WHERE tenant_id = $1", tid
        )
        for s in sales:
            tot = money(s["total_amount"])
            split_sum = money(
                money(s["paid_cash_amount"])
                + money(s["paid_transfer_amount"])
                + money(s["paid_wallet_amount"])
                + money(s["fiado_amount"])
            )
            if tot != split_sum:
                issues.append(
                    f"Venta #{s['sale_number']}: total {tot} != suma pagos {split_sum}"
                )
            gp = money(s["gross_profit_snapshot"])
            expected_gp = money(tot - money(s["total_cost_snapshot"]))
            if gp != expected_gp:
                issues.append(
                    f"Venta #{s['sale_number']}: ganancia {gp} != esperada {expected_gp}"
                )

        # 2. Clientes: saldo actual == suma exacta de signed_delta en customer_ledger
        customers = await conn.fetch(
            "SELECT id, name, current_balance FROM customers WHERE tenant_id = $1",
            tid,
        )
        for c in customers:
            ledger_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(signed_delta), 0) FROM customer_ledger WHERE tenant_id = $1 AND customer_id = $2",
                tid,
                c["id"],
            )
            if money(c["current_balance"]) != money(ledger_sum):
                issues.append(
                    f"Cliente '{c['name']}': saldo {money(c['current_balance'])} != libro mayor {money(ledger_sum)}"
                )

        # 3. Proveedores: deuda actual == suma exacta de signed_delta en supplier_ledger
        suppliers = await conn.fetch(
            "SELECT id, name, current_debt FROM suppliers WHERE tenant_id = $1",
            tid,
        )
        for sup in suppliers:
            sup_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(signed_delta), 0) FROM supplier_ledger WHERE tenant_id = $1 AND supplier_id = $2",
                tid,
                sup["id"],
            )
            if money(sup["current_debt"]) != money(sup_sum):
                issues.append(
                    f"Proveedor '{sup['name']}': deuda {money(sup['current_debt'])} != libro mayor {money(sup_sum)}"
                )

        # 4. Productos: stock actual == suma exacta de qty_delta en inventory_movements
        products = await conn.fetch(
            "SELECT id, name, stock_qty FROM products WHERE tenant_id = $1",
            tid,
        )
        for p in products:
            k_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(qty_delta), 0) FROM inventory_movements WHERE tenant_id = $1 AND product_id = $2",
                tid,
                p["id"],
            )
            if qty(p["stock_qty"]) != qty(k_sum):
                issues.append(
                    f"Producto '{p['name']}': stock {qty(p['stock_qty'])} != kárdex {qty(k_sum)}"
                )

        # 5. Conciliación Ventas cobradas vs Movimientos de caja por ventas
        active_sales_cash = sum(
            (
                money(s["paid_cash_amount"])
                for s in sales
                if s["status"] == "completed"
            ),
            Decimal("0.00"),
        )
        active_sales_transfer = sum(
            (
                money(s["paid_transfer_amount"])
                for s in sales
                if s["status"] == "completed"
            ),
            Decimal("0.00"),
        )
        movs = await conn.fetch(
            "SELECT * FROM cash_movements WHERE tenant_id = $1", tid
        )
        active_sales_wallet = sum((money(s["paid_wallet_amount"]) for s in sales if s["status"] == "completed"), Decimal("0.00"))
        net_sale_wallet_mov = Decimal("0.00")
        net_sale_cash_mov = Decimal("0.00")
        net_sale_transfer_mov = Decimal("0.00")
        for m in movs:
            amt = money(m["amount"])
            if m["movement_type"] == "sale_income":
                if m["channel"] == "efectivo":
                    net_sale_cash_mov += amt
                elif m["channel"] == "transferencia":
                    net_sale_transfer_mov += amt
                else:
                    net_sale_wallet_mov += amt
            elif m["movement_type"] == "sale_void_outflow":
                if m["channel"] == "efectivo":
                    net_sale_cash_mov -= amt
                elif m["channel"] == "transferencia":
                    net_sale_transfer_mov -= amt
                else:
                    net_sale_wallet_mov -= amt

        if active_sales_wallet != net_sale_wallet_mov:
            issues.append(f"Descalce billetera ventas ({active_sales_wallet}) vs caja ({net_sale_wallet_mov})")
        if active_sales_cash != net_sale_cash_mov:
            issues.append(
                f"Descalce efectivo ventas ({active_sales_cash}) vs caja ({net_sale_cash_mov})"
            )
        if active_sales_transfer != net_sale_transfer_mov:
            issues.append(
                f"Descalce transferencias ventas ({active_sales_transfer}) vs caja ({net_sale_transfer_mov})"
            )

    return {
        "is_mathematically_consistent": len(issues) == 0,
        "sales_checked": len(sales),
        "customers_checked": len(customers),
        "suppliers_checked": len(suppliers),
        "products_checked": len(products),
        "cash_movements_checked": len(movs),
        "issues": issues,
    }


async def compute_tenant_monthly_analytics(
    tenant_id: str, target_month: str | None = None
) -> dict[str, Any]:
    """
    Calcula de manera determinística, reproducible y verificable los 13 indicadores
    del Servicio de Análisis Mensual para UN comercio específico (aislado por tenant_id).
    """
    db_mgr = get_db()
    async with db_mgr.connection(tenant_id=tenant_id) as conn:
        zone = await conn.fetchval("SELECT timezone FROM tenants WHERE id=$1", tenant_id) or "UTC"
        ym = target_month or utc_now().astimezone(ZoneInfo(zone)).strftime("%Y-%m")
        prev_ym = _prev_month(ym)
        products = await conn.fetch(
            "SELECT * FROM products WHERE tenant_id = $1", tenant_id
        )
        sales = await conn.fetch(
            "SELECT * FROM sales WHERE tenant_id = $1 AND status = 'completed'",
            tenant_id,
        )
        sale_items = await conn.fetch(
            """
            SELECT si.*, s.created_at AS sale_date, s.status AS sale_status
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id AND s.tenant_id = si.tenant_id
            WHERE si.tenant_id = $1 AND s.status = 'completed'
            """,
            tenant_id,
        )
        price_hist = await conn.fetch(
            """
            SELECT h.*, p.name AS product_name
            FROM product_price_history h
            JOIN products p ON p.id = h.product_id AND p.tenant_id = h.tenant_id
            WHERE h.tenant_id = $1
            ORDER BY h.created_at DESC
            LIMIT 100
            """,
            tenant_id,
        )
        cash_movs = await conn.fetch(
            "SELECT * FROM cash_movements WHERE tenant_id = $1", tenant_id
        )
        customers = await conn.fetch(
            "SELECT * FROM customers WHERE tenant_id = $1 AND is_active = $2",
            tenant_id,
            True,
        )
        cust_ledger = await conn.fetch(
            "SELECT * FROM customer_ledger WHERE tenant_id = $1", tenant_id
        )

    # 1. Evolución mensual de ventas
    monthly_buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "sales": Decimal("0.00"),
            "profit": Decimal("0.00"),
            "count": Decimal("0"),
        }
    )
    for s in sales:
        m_key = _extract_ym(s["created_at"], zone)
        monthly_buckets[m_key]["sales"] += money(s["total_amount"])
        monthly_buckets[m_key]["profit"] += money(s["gross_profit_snapshot"])
        monthly_buckets[m_key]["count"] += Decimal("1")

    monthly_evolution = [
        {
            "month": k,
            "sales_total": money_float(v["sales"]),
            "gross_profit": money_float(v["profit"]),
            "sales_count": int(v["count"]),
        }
        for k, v in sorted(monthly_buckets.items())
    ]

    # Filtrar ítems del período analizado (o global si no hay ventas en ym aún y se pide panorama)
    month_items = [it for it in sale_items if _extract_ym(it["sale_date"], zone) == ym]
    items_for_ranking = month_items

    prod_stats: dict[str, dict[str, Any]] = {}
    for p in products:
        prod_stats[p["id"]] = {
            "product_id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "stock_qty": qty(p["stock_qty"]),
            "cost_price": money(p["cost_price"]),
            "sale_price": money(p["sale_price"]),
            "units_sold": Decimal("0.000"),
            "revenue": Decimal("0.00"),
            "profit": Decimal("0.00"),
        }

    for it in items_for_ranking:
        pid = it["product_id"] or f"manual:{it['description']}"
        if pid not in prod_stats:
            prod_stats[pid] = {
                "product_id": pid,
                "name": it["description"],
                "category": "Venta Manual",
                "stock_qty": Decimal("0.000"),
                "cost_price": money(it["unit_cost_snapshot"]),
                "sale_price": money(it["unit_price_snapshot"]),
                "units_sold": Decimal("0.000"),
                "revenue": Decimal("0.00"),
                "profit": Decimal("0.00"),
            }
        prod_stats[pid]["units_sold"] += qty(it["quantity"])
        prod_stats[pid]["revenue"] += money(it["line_subtotal"])
        prod_stats[pid]["profit"] += money(it["line_gross_profit"])

    stats_list = list(prod_stats.values())

    # 2. Productos más vendidos
    top_selling = [
        {
            "product_id": x["product_id"],
            "name": x["name"],
            "units_sold": qty_float(x["units_sold"]),
            "revenue": money_float(x["revenue"]),
            "gross_profit": money_float(x["profit"]),
        }
        for x in sorted(
            [s for s in stats_list if s["units_sold"] > Decimal("0")],
            key=lambda i: (i["units_sold"], i["revenue"]),
            reverse=True,
        )[:10]
    ]

    # 3. Productos con menor rotación (catalogados activos)
    catalog_only = [
        s for s in stats_list if not str(s["product_id"]).startswith("manual:")
    ]
    lowest_rotation = [
        {
            "product_id": x["product_id"],
            "name": x["name"],
            "units_sold": qty_float(x["units_sold"]),
            "current_stock": qty_float(x["stock_qty"]),
            "capital_at_cost": money_float(money(x["stock_qty"] * x["cost_price"])),
        }
        for x in sorted(catalog_only, key=lambda i: (i["units_sold"], -i["stock_qty"]))[:10]
    ]

    # 4. Ganancias por producto
    profit_by_product = [
        {
            "product_id": x["product_id"],
            "name": x["name"],
            "gross_profit": money_float(x["profit"]),
            "revenue": money_float(x["revenue"]),
            "margin_pct": (
                float((x["profit"] / x["revenue"] * Decimal("100")).quantize(Decimal("0.01")))
                if x["revenue"] > Decimal("0")
                else 0.0
            ),
        }
        for x in sorted(
            [s for s in stats_list if s["revenue"] > Decimal("0")],
            key=lambda i: i["profit"],
            reverse=True,
        )[:10]
    ]

    # 5. Evolución de costos
    cost_changes = [
        {
            "product_name": h["product_name"],
            "old_cost": money_float(h["old_cost"]),
            "new_cost": money_float(h["new_cost"]),
            "variation_pct": (
                float(
                    (
                        (money(h["new_cost"]) - money(h["old_cost"]))
                        / money(h["old_cost"])
                        * Decimal("100")
                    ).quantize(Decimal("0.01"))
                )
                if money(h["old_cost"]) > Decimal("0")
                else 0.0
            ),
            "reason": h["reason"],
            "created_at": h["created_at"],
        }
        for h in price_hist
        if money(h["new_cost"]) != money(h["old_cost"])
    ][:15]

    # 6. Margen comercial y 7. Stock inmovilizado
    immobilized_items = [
        {
            "product_id": x["product_id"],
            "name": x["name"],
            "stock_qty": qty_float(x["stock_qty"]),
            "immobilized_cost_value": money_float(
                money(x["stock_qty"] * x["cost_price"])
            ),
        }
        for x in catalog_only
        if x["stock_qty"] > Decimal("0") and x["units_sold"] == Decimal("0")
    ]
    immobilized_total_cost = sum(
        (Decimal(str(i["immobilized_cost_value"])) for i in immobilized_items),
        Decimal("0.00"),
    )

    # 8. Gastos por categoría
    expenses_by_cat: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for m in cash_movs:
        if m["movement_type"] == "operating_expense":
            if _extract_ym(m["created_at"], zone) == ym:
                expenses_by_cat[m["category"]] += money(m["amount"])

    # 9 & 10. Fiados acumulados y Clientes con mayores saldos pendientes
    total_fiados_pending = sum(
        (money(c["current_balance"]) for c in customers), Decimal("0.00")
    )
    top_debtors = [
        {
            "customer_id": c["id"],
            "name": c["name"],
            "current_balance": money_float(c["current_balance"]),
            "oldest_unpaid_at": c.get("oldest_unpaid_at"),
        }
        for c in sorted(
            [c for c in customers if money(c["current_balance"]) > Decimal("0")],
            key=lambda x: money(x["current_balance"]),
            reverse=True,
        )[:10]
    ]

    # 11. Comportamiento de cobros
    total_fiado_granted_ever = sum(
        (
            money(l["amount"])
            for l in cust_ledger
            if l["entry_type"] == "fiado_charge"
        ),
        Decimal("0.00"),
    )
    total_fiado_collected_ever = sum(
        (money(l["amount"]) for l in cust_ledger if l["entry_type"] == "payment"),
        Decimal("0.00"),
    )
    recovery_rate_pct = (
        float(
            (
                total_fiado_collected_ever
                / total_fiado_granted_ever
                * Decimal("100")
            ).quantize(Decimal("0.01"))
        )
        if total_fiado_granted_ever > Decimal("0")
        else 100.0
    )

    # 12. Comparación entre períodos (ym vs prev_ym)
    curr_bucket = monthly_buckets.get(
        ym,
        {"sales": Decimal("0.00"), "profit": Decimal("0.00"), "count": Decimal("0")},
    )
    prev_bucket = monthly_buckets.get(
        prev_ym,
        {"sales": Decimal("0.00"), "profit": Decimal("0.00"), "count": Decimal("0")},
    )
    sales_growth_pct = (
        float(
            (
                (curr_bucket["sales"] - prev_bucket["sales"])
                / prev_bucket["sales"]
                * Decimal("100")
            ).quantize(Decimal("0.01"))
        )
        if prev_bucket["sales"] > Decimal("0")
        else 0.0
    )

    # 13. Indicadores de salud del negocio (reproducibles y verificables)
    all_sales_sum = sum((money(s["total_amount"]) for s in sales), Decimal("0.00"))
    all_profit_sum = sum(
        (money(s["gross_profit_snapshot"]) for s in sales), Decimal("0.00")
    )
    real_margin_pct = (
        float((all_profit_sum / all_sales_sum * Decimal("100")).quantize(Decimal("0.01")))
        if all_sales_sum > Decimal("0")
        else 0.0
    )
    fiado_exposure_pct = (
        float(
            (total_fiados_pending / all_sales_sum * Decimal("100")).quantize(
                Decimal("0.01")
            )
        )
        if all_sales_sum > Decimal("0")
        else 0.0
    )

    # Puntaje determinístico transparente sobre 100
    score = 70
    if real_margin_pct >= 25.0:
        score += 10
    elif real_margin_pct < 12.0 and all_sales_sum > Decimal("0"):
        score -= 10
    if recovery_rate_pct >= 80.0:
        score += 10
    elif recovery_rate_pct < 50.0 and total_fiado_granted_ever > Decimal("0"):
        score -= 15
    if len(immobilized_items) == 0 and len(catalog_only) > 0:
        score += 10
    score = max(0, min(100, score))

    return {
        "period_analyzed": ym,
        "previous_period": prev_ym,
        "monthly_sales_evolution": monthly_evolution,
        "top_selling_products": top_selling,
        "lowest_rotation_products": lowest_rotation,
        "profit_by_product": profit_by_product,
        "cost_evolution": cost_changes,
        "commercial_margin": {
            "realized_gross_margin_pct": real_margin_pct,
            "total_sales_evaluated": money_float(all_sales_sum),
            "total_gross_profit_evaluated": money_float(all_profit_sum),
        },
        "immobilized_stock": {
            "items_count": len(immobilized_items),
            "total_immobilized_cost": money_float(immobilized_total_cost),
            "items": immobilized_items[:15],
        },
        "expenses_breakdown": [
            {"category": k, "amount": money_float(v)}
            for k, v in sorted(
                expenses_by_cat.items(), key=lambda item: item[1], reverse=True
            )
        ],
        "accumulated_fiados": money_float(total_fiados_pending),
        "top_debtor_customers": top_debtors,
        "collection_behavior": {
            "total_fiado_granted": money_float(total_fiado_granted_ever),
            "total_fiado_collected": money_float(total_fiado_collected_ever),
            "recovery_rate_pct": recovery_rate_pct,
        },
        "period_comparison": {
            "current_month": ym,
            "current_month_sales": money_float(curr_bucket["sales"]),
            "current_month_profit": money_float(curr_bucket["profit"]),
            "previous_month": prev_ym,
            "previous_month_sales": money_float(prev_bucket["sales"]),
            "previous_month_profit": money_float(prev_bucket["profit"]),
            "sales_variation_pct": sales_growth_pct,
        },
        "business_health_indicators": {
            "health_score_0_100": score,
            "status_label": (
                "Saludable"
                if score >= 75
                else ("Atención Moderada" if score >= 50 else "Riesgo Operativo")
            ),
            "realized_margin_pct": real_margin_pct,
            "fiado_recovery_rate_pct": recovery_rate_pct,
            "fiado_over_sales_exposure_pct": fiado_exposure_pct,
            "immobilized_capital_ars": money_float(immobilized_total_cost),
        },
    }


async def export_tenant_backup_json(ctx: AuthContext) -> dict[str, Any]:
    """Exporta respaldo íntegro, portable y estructurado de TODOS los datos del comercio."""
    assert ctx.tenant_id is not None
    tid = ctx.tenant_id
    db_mgr = get_db()

    tables = [
        "products",
        "product_price_history",
        "inventory_movements",
        "customers",
        "customer_ledger",
        "sales",
        "sale_items",
        "suppliers",
        "purchases",
        "purchase_items",
        "supplier_ledger",
        "cash_sessions",
        "cash_movements",
        "fiscal_profiles",
        "fiscal_vouchers",
        "monthly_analysis_reports",
    ]
    data_dump: dict[str, Any] = {}
    async with db_mgr.connection(tenant_id=tid) as conn:
        tenant_row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tid)
        for tbl in tables:
            rows = await conn.fetch(
                f"SELECT * FROM {tbl} WHERE tenant_id = $1", tid
            )
            data_dump[tbl] = json.loads(
                json.dumps(rows, ensure_ascii=False, default=str)
            )

    return {
        "backup_format": "mi_tiendita_v2_json",
        "exported_at": utc_now().isoformat(),
        "tenant": json.loads(json.dumps(tenant_row, ensure_ascii=False, default=str)),
        "tables": data_dump,
    }


async def export_tenant_excel_xlsx(ctx: AuthContext) -> bytes:
    """Genera libro Excel (.xlsx) completo con Inventario, Ventas, Fiados, Caja y Compras."""
    backup = await export_tenant_backup_json(ctx)
    wb = Workbook()

    ws_prod = wb.active
    ws_prod.title = "Inventario"
    ws_prod.append(
        [
            "Código",
            "Nombre",
            "Categoría",
            "Unidad",
            "Stock",
            "Stock Mínimo",
            "Costo",
            "Precio Venta",
        ]
    )
    for p in backup["tables"]["products"]:
        ws_prod.append(
            [
                p.get("sku") or "",
                p["name"],
                p["category"],
                p["unit"],
                float(p["stock_qty"]),
                float(p["min_stock_qty"]),
                float(p["cost_price"]),
                float(p["sale_price"]),
            ]
        )

    ws_sales = wb.create_sheet("Ventas")
    ws_sales.append(
        [
            "Nro Venta",
            "Estado",
            "Fecha",
            "Total",
            "Costo Histórico",
            "Ganancia Bruta",
            "Efectivo",
            "Transferencia",
            "Billetera virtual",
            "Fiado",
        ]
    )
    for s in backup["tables"]["sales"]:
        ws_sales.append(
            [
                s["sale_number"],
                s["status"],
                s["created_at"],
                float(s["total_amount"]),
                float(s["total_cost_snapshot"]),
                float(s["gross_profit_snapshot"]),
                float(s["paid_cash_amount"]),
                float(s["paid_transfer_amount"]),
                float(s["paid_wallet_amount"]),
                float(s["fiado_amount"]),
            ]
        )

    ws_cust = wb.create_sheet("Clientes_Fiados")
    ws_cust.append(["Nombre", "Teléfono", "Dirección", "Deuda Pendiente"])
    for c in backup["tables"]["customers"]:
        ws_cust.append(
            [
                c["name"],
                c.get("phone") or "",
                c.get("address") or "",
                float(c["current_balance"]),
            ]
        )

    ws_cash = wb.create_sheet("Movimientos_Caja")
    ws_cash.append(["Fecha", "Tipo", "Canal", "Dirección", "Categoría", "Importe", "Detalle"])
    for m in backup["tables"]["cash_movements"]:
        ws_cash.append(
            [
                m["created_at"],
                m["movement_type"],
                m["channel"],
                m["direction"],
                m["category"],
                float(m["amount"]),
                m["description"],
            ]
        )

    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
