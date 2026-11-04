from typing import Any

from app.services.cash import get_cash_status
from app.services.catalog import list_products_with_valuation
from app.services.customers import list_customers
from app.services.reports import (
    compute_tenant_monthly_analytics,
    get_financial_results,
)
from app.security import AuthContext


async def build_ayudante_briefing(ctx: AuthContext) -> dict[str, Any]:
    """
    El Ayudante de Mi Tiendita (Motor determinístico $0 + Modo lectura seguro):
    Analiza indicadores verificados del comercio sin alterar ningún registro financiero.
    """
    assert ctx.tenant_id is not None
    fin = await get_financial_results(ctx)
    cat = await list_products_with_valuation(ctx)
    cust = await list_customers(ctx)
    cash = await get_cash_status(ctx)
    analytics = await compute_tenant_monthly_analytics(ctx.tenant_id)

    eco = fin["economic_results"]
    cr = fin["cash_and_receivables"]
    val = cat["valuation"]

    # Detectar productos donde el costo alcanzó o superó el precio de venta (alerta anti-inflación!)
    margin_risk_products = [
        {
            "id": p["id"],
            "name": p["name"],
            "cost_price": p["cost_price"],
            "sale_price": p["sale_price"],
            "margin_pct": p["margin_pct"],
        }
        for p in cat["items"]
        if p["is_active"] and p["margin_pct"] < 15.0
    ]

    cards: list[dict[str, Any]] = []

    # Tarjeta 1: Explicación clara Ganancia vs Caja
    cards.append(
        {
            "type": "finanzas",
            "severity": "info",
            "title": "Claridad de tus números: Ganancia vs Plata en Mano",
            "message": (
                f"Vendiste por ${eco['sales_total']:,.2f}. Descontando lo que te costó esa mercadería "
                f"(${eco['historical_cogs']:,.2f}) y tus gastos operativos (${eco['operating_expenses']:,.2f}), "
                f"tu resultado operativo estimado es de ${eco['estimated_operating_result']:,.2f}. "
                f"En dinero real cobraste ${cr['total_money_effectively_collected']:,.2f} "
                f"(${cr['collected_in_physical_cash']:,.2f} en efectivo, ${cr['collected_by_transfer']:,.2f} por transferencia y ${cr['collected_by_wallet']:,.2f} por billetera), "
                f"y tenés ${cr['total_pending_to_collect_fiados']:,.2f} anotados en la calle por cobrar."
            ),
        }
    )

    # Tarjeta 2: Alertas de precios desactualizados frente a costos
    if margin_risk_products:
        names = ", ".join(p["name"] for p in margin_risk_products[:3])
        cards.append(
            {
                "type": "margen",
                "severity": "warning",
                "title": f"{len(margin_risk_products)} artículo(s) con margen muy bajo (<15%)",
                "message": (
                    f"Revisá el precio de venta de: {names}. Su costo de reposición está muy cerca "
                    "del precio de mostrador y podrías descapitalizarte al reponer."
                ),
                "requires_human_action": "Actualizar precio manualmente en Productos.",
            }
        )

    # Tarjeta 3: Reposición de stock mínimo
    if cat["low_stock_alerts"]:
        names = ", ".join(a["name"] for a in cat["low_stock_alerts"][:4])
        cards.append(
            {
                "type": "stock",
                "severity": "warning",
                "title": f"{len(cat['low_stock_alerts'])} producto(s) llegaron al stock mínimo",
                "message": f"Conviene anotar para el proveedor: {names}.",
                "requires_human_action": "Registrar compra cuando llegue el pedido.",
            }
        )

    # Tarjeta 4: Seguimiento de fiados vencidos
    if cust["alerts"]:
        names = ", ".join(
            f"{a['name']} (${a['current_balance']:,.0f})" for a in cust["alerts"][:3]
        )
        cards.append(
            {
                "type": "fiados",
                "severity": "danger",
                "title": f"{len(cust['alerts'])} cuenta(s) de fiado requieren recordatorio",
                "message": (
                    f"Saldos que superaron el plazo o tope configurado: {names}. "
                    "Podés enviarles un recordatorio amable por WhatsApp con un toque."
                ),
                "requires_human_action": "Confirmar envío manual del mensaje en Clientes y Fiados.",
            }
        )

    # Tarjeta 5: Mercadería inmovilizada
    imm = analytics["immobilized_stock"]
    if imm["items_count"] > 0 and eco["sales_count"] > 0:
        cards.append(
            {
                "type": "rotacion",
                "severity": "info",
                "title": f"Capital quieto en estantería: ${imm['total_immobilized_cost']:,.2f}",
                "message": (
                    f"Tenés {imm['items_count']} producto(s) con stock disponible que todavía no registraron ventas "
                    "en el período. Una oferta o combo puede ayudarte a convertirlos en efectivo."
                ),
            }
        )

    return {
        "assistant_name": "El Ayudante",
        "mode": "deterministic_verified",
        "llm_optional_configured": False,
        "safety_guarantee": (
            "El Ayudante opera en modo SOLO LECTURA sobre datos determinísticos verificados. "
            "No puede inventar ventas, modificar saldos ni alterar la caja."
        ),
        "cards": cards,
        "quick_metrics": {
            "physical_cash_expected": cash["global_balances"]["physical_cash_balance"],
            "transfer_balance": cash["global_balances"]["transfer_balance"],
            "wallet_balance": cash["global_balances"]["wallet_balance"],
            "inventory_cost_value": val["inventory_cost_value"],
            "pending_fiados": cr["total_pending_to_collect_fiados"],
            "health_score": analytics["business_health_indicators"]["health_score_0_100"],
        },
    }


async def answer_ayudante_question(ctx: AuthContext, question: str) -> dict[str, Any]:
    """
    Responde consultas cotidianas del comerciante usando cálculo determinístico exacto ($0).
    """
    assert ctx.tenant_id is not None
    fin = await get_financial_results(ctx)
    cat = await list_products_with_valuation(ctx)
    cust = await list_customers(ctx)
    cash = await get_cash_status(ctx)
    analytics = await compute_tenant_monthly_analytics(ctx.tenant_id)

    eco = fin["economic_results"]
    cr = fin["cash_and_receivables"]
    q_low = question.strip().lower()

    # Respuesta determinística exacta garantizada ($0 costo, sin generación de texto por modelo)
    if any(w in q_low for w in ("ganancia", "gane", "gané", "resultado", "rentab")):
        answer = (
            f"Tu facturación total registrada es de **${eco['sales_total']:,.2f}** en {eco['sales_count']} venta(s). "
            f"Restando el costo histórico de esa mercadería (**${eco['historical_cogs']:,.2f}**) te queda una "
            f"**ganancia bruta de ${eco['gross_profit']:,.2f}** ({eco['gross_margin_pct']}% de margen). "
            f"Descontando gastos operativos (**${eco['operating_expenses']:,.2f}**), tu **resultado operativo estimado es ${eco['estimated_operating_result']:,.2f}**."
        )
        if eco["estimation_explanations"]:
            answer += " Nota: " + " ".join(eco["estimation_explanations"])
    elif any(w in q_low for w in ("fiado", "debe", "deuda", "calle", "cobrar")):
        top = cust["items"][:3]
        who = (
            ", ".join(f"{c['name']} (${c['current_balance']:,.2f})" for c in top if c["current_balance"] > 0)
            or "ningún cliente con deuda activa"
        )
        answer = (
            f"Tenés **${cr['total_pending_to_collect_fiados']:,.2f}** pendientes de cobro en total "
            f"entre {cust['summary']['debtors_count']} cliente(s), con {cust['summary']['alerts_count']} aviso(s) vencido(s). "
            f"Mayores saldos: {who}."
        )
    elif any(w in q_low for w in ("caja", "efectivo", "plata", "transferencia", "cajon", "cajón")):
        gb = cash["global_balances"]
        answer = (
            f"Según todos tus movimientos registrados, deberías tener **${gb['physical_cash_balance']:,.2f}** en efectivo físico "
            f"un saldo neto por transferencias de **${gb['transfer_balance']:,.2f}** y por billeteras **${gb['wallet_balance']:,.2f}** "
            f"(total disponible: **${gb['total_available_money']:,.2f}**). "
            f"Recordá que los ${cr['total_pending_to_collect_fiados']:,.2f} de fiados están aparte hasta que se cobren."
        )
    elif any(w in q_low for w in ("stock", "reponer", "falta", "inventario", "mercaderia", "mercadería")):
        val = cat["valuation"]
        alerts = cat["low_stock_alerts"]
        low_txt = (
            ", ".join(f"{a['name']} (quedan {a['stock_qty']} {a['unit']})" for a in alerts[:5])
            if alerts
            else "ningún producto en alerta mínima"
        )
        answer = (
            f"Tenés {val['active_products_count']} productos activos valorizados en **${val['inventory_cost_value']:,.2f}** a costo "
            f"(**${val['inventory_sale_value']:,.2f}** a precio de venta, ganancia potencial **${val['potential_gross_profit']:,.2f}**). "
            f"En alerta de reposición ({len(alerts)}): {low_txt}."
        )
    elif any(w in q_low for w in ("mas vendido", "más vendido", "rotacion", "rotación", "clavad")):
        top_s = analytics["top_selling_products"][:3]
        top_txt = (
            ", ".join(f"{p['name']} ({p['units_sold']} u.)" for p in top_s)
            or "aún sin ventas registradas"
        )
        imm = analytics["immobilized_stock"]
        answer = (
            f"Tus productos más vendidos son: **{top_txt}**. "
            f"Además tenés {imm['items_count']} artículo(s) sin rotación reciente por un costo inmovilizado de **${imm['total_immobilized_cost']:,.2f}**."
        )
    else:
        gb = cash["global_balances"]
        answer = (
            f"Resumen verificado de tu negocio hoy: "
            f"Ventas acumuladas **${eco['sales_total']:,.2f}** | Ganancia bruta **${eco['gross_profit']:,.2f}** | "
            f"Efectivo físico esperado **${gb['physical_cash_balance']:,.2f}** | Transferencias **${gb['transfer_balance']:,.2f}** | "
            f"Fiados por cobrar **${cr['total_pending_to_collect_fiados']:,.2f}** | "
            f"Alertas de stock mínimo: **{cat['valuation']['low_stock_count']}**."
        )

    return {
        "assistant_name": "El Ayudante",
        "engine": "deterministic_local_zero_cost",
        "question": question,
        "answer": answer,
    }
