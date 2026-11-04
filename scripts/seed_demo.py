#!/usr/bin/env python3
"""
Semilla de Datos de Demostración — ESTRICTAMENTE SEPARADA DE PRODUCCIÓN.
Todos los comercios creados aquí llevan is_demo = TRUE y la etiqueta explícita '[DEMO]'.
Los comercios reales creados desde /api/v1/auth/register inician con inventario 100% vacío.
"""
import asyncio
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import money, new_id, utc_now
from app.db import DatabaseManager, get_db, set_db
from app.migrations import run_migrations
from app.schemas import (
    CashMovementCreateRequest,
    CashSessionOpenRequest,
    CustomerCreateRequest,
    CustomerPaymentRequest,
    ProductCreateRequest,
    PurchaseCreateRequest,
    PurchaseItemInput,
    SaleCreateRequest,
    SaleItemInput,
    SupplierCreateRequest,
)
from app.security import AuthContext, hash_password
from app.services import cash as cash_svc
from app.services import catalog as catalog_svc
from app.services import customers as customers_svc
from app.services import sales as sales_svc
from app.services import suppliers as suppliers_svc


async def ensure_demo_data(db_mgr: DatabaseManager | None = None) -> None:
    mgr = db_mgr or get_db()
    set_db(mgr)
    await mgr.connect()
    await run_migrations(mgr)

    async with mgr.connection() as conn:
        exists = await conn.fetchval(
            "SELECT id FROM tenants WHERE slug = $1", "almacen-don-mateo-demo"
        )
        if exists:
            return

    now = utc_now()
    t1_id = new_id()
    u1_id = new_id()
    u1_cashier_id = new_id()
    t2_id = new_id()
    u2_id = new_id()

    async with mgr.transaction() as conn:
        # Comercio Demo 1: Almacén Don Mateo [DEMO]
        await conn.execute(
            """
            INSERT INTO tenants (
                id, slug, name, business_type, owner_name, phone, email, address,
                currency, plan_id, subscription_status, subscription_expires_at,
                fiscal_module_enabled, monthly_analysis_enabled,
                debt_alert_days, debt_alert_amount, is_demo, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            """,
            t1_id,
            "almacen-don-mateo-demo",
            "Almacén Don Mateo [DEMO]",
            "Almacén de Barrio",
            "Mateo Sosa",
            "5493515550101",
            "demo.almacen@mitiendita.ar",
            "Av. Colón 1450, Córdoba",
            "ARS",
            "plan_pro",
            "active",
            now + timedelta(days=30),
            True,
            True,
            10,
            money("8000.00"),
            True,
            now,
            now,
        )
        await conn.execute(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                role, is_active, token_version, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            u1_id,
            t1_id,
            "demo.almacen@mitiendita.ar",
            "Mateo Sosa (Dueño Demo)",
            hash_password("Demo1234!"),
            "owner",
            True,
            1,
            now,
        )
        await conn.execute(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                role, is_active, token_version, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            u1_cashier_id,
            t1_id,
            "cajera.demo@mitiendita.ar",
            "Lucía Fernández (Cajera Demo)",
            hash_password("Demo1234!"),
            "cashier",
            True,
            1,
            now,
        )

        # Comercio Demo 2: Kiosco El Trébol [DEMO] (para mostrar aislamiento multitenant en vivo)
        await conn.execute(
            """
            INSERT INTO tenants (
                id, slug, name, business_type, owner_name, phone, email, address,
                currency, plan_id, subscription_status, subscription_expires_at,
                fiscal_module_enabled, monthly_analysis_enabled,
                debt_alert_days, debt_alert_amount, is_demo, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            """,
            t2_id,
            "kiosco-el-trebol-demo",
            "Kiosco El Trébol [DEMO]",
            "Kiosco",
            "Valeria Gómez",
            "5493515550202",
            "demo.kiosco@mitiendita.ar",
            "Bv. San Juan 420, Córdoba",
            "ARS",
            "plan_inicial",
            "active",
            now + timedelta(days=30),
            False,
            False,
            15,
            money("15000.00"),
            True,
            now,
            now,
        )
        await conn.execute(
            """
            INSERT INTO users (
                id, tenant_id, email, full_name, password_hash,
                role, is_active, token_version, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            u2_id,
            t2_id,
            "demo.kiosco@mitiendita.ar",
            "Valeria Gómez (Dueña Demo)",
            hash_password("Demo1234!"),
            "owner",
            True,
            1,
            now,
        )

    # Poblar operaciones reales mediante la capa determinística de servicios para Don Mateo [DEMO]
    ctx1 = AuthContext(
        user_id=u1_id,
        email="demo.almacen@mitiendita.ar",
        full_name="Mateo Sosa",
        role="owner",
        tenant_id=t1_id,
        tenant_name="Almacén Don Mateo [DEMO]",
        subscription_status="active",
        plan_id="plan_pro",
        fiscal_module_enabled=True,
        monthly_analysis_enabled=True,
    )

    await cash_svc.open_cash_session(
        ctx1,
        CashSessionOpenRequest(
            opening_cash_amount=Decimal("25000.00"),
            notes="Cambio inicial turno mañana [DEMO]",
        ),
    )

    demo_products = [
        ("Yerba Mate Selección 1kg", "ALM-001", "Almacén", "unidad", "18", "5", "2400", "3500"),
        ("Aceite Girasol 1.5L", "ALM-002", "Almacén", "unidad", "12", "4", "2100", "3100"),
        ("Leche Entera Larga Vida 1L", "LAC-001", "Lácteos", "unidad", "4", "6", "1050", "1500"),
        ("Queso Cremoso por horma", "FIA-001", "Fiambrería", "kg", "6.500", "2.000", "6200", "8900"),
        ("Gaseosa Cola 2.25L", "BEB-001", "Bebidas", "unidad", "20", "6", "1950", "2900"),
        ("Galletitas de Agua Pack x3", "GAL-001", "Panificados", "pack", "15", "5", "1100", "1700"),
        ("Azúcar Común 1kg", "ALM-003", "Almacén", "unidad", "3", "5", "850", "1300"),
        ("Detergente Concentrado 500ml", "LIM-001", "Limpieza", "unidad", "9", "3", "1300", "2050"),
    ]
    created_prods = []
    for name, sku, cat, unit, st, min_st, cost, price in demo_products:
        p = await catalog_svc.create_product(
            ctx1,
            ProductCreateRequest(
                name=name,
                sku=sku,
                category=cat,
                unit=unit,  # type: ignore[arg-type]
                stock_qty=Decimal(st),
                min_stock_qty=Decimal(min_st),
                cost_price=Decimal(cost),
                sale_price=Decimal(price),
                notes="Artículo de demostración",
            ),
        )
        created_prods.append(p)

    c1 = await customers_svc.create_customer(
        ctx1,
        CustomerCreateRequest(
            name="Rosa Quiroga (Vecina)",
            phone="5493514001122",
            address="Pasaje Aguaribay 88",
            credit_limit=Decimal("30000"),
        ),
    )
    c2 = await customers_svc.create_customer(
        ctx1,
        CustomerCreateRequest(
            name="Carlos Pereyra",
            phone="5493514003344",
            address="Av. Colón 1482",
            credit_limit=Decimal("25000"),
        ),
    )

    sup1 = await suppliers_svc.create_supplier(
        ctx1,
        SupplierCreateRequest(
            name="Distribuidora Mayorista Centro",
            tax_id="30-71234567-9",
            phone="5493516009988",
            address="Mercado Norte, Córdoba",
        ),
    )

    # Venta 1: Efectivo puro
    await sales_svc.create_sale(
        ctx1,
        SaleCreateRequest(
            idempotency_key="demo-sale-001",
            items=[
                SaleItemInput(product_id=created_prods[0]["id"], quantity=Decimal("2")),
                SaleItemInput(product_id=created_prods[4]["id"], quantity=Decimal("1")),
            ],
            paid_cash_amount=Decimal("9900.00"),
            notes="Venta mostrador efectivo",
        ),
    )

    # Venta 2: Pago combinado (Efectivo + Transferencia + Fiado con vencimiento vencido para mostrar alerta!)
    await sales_svc.create_sale(
        ctx1,
        SaleCreateRequest(
            idempotency_key="demo-sale-002",
            items=[
                SaleItemInput(product_id=created_prods[1]["id"], quantity=Decimal("2")),
                SaleItemInput(product_id=created_prods[3]["id"], quantity=Decimal("1.000")),
            ],
            paid_cash_amount=Decimal("5000.00"),
            paid_transfer_amount=Decimal("4100.00"),
            fiado_amount=Decimal("6000.00"),
            customer_id=c1["id"],
            due_date="2026-10-20",
            notes="Compra quincenal pago combinado",
        ),
    )

    # Venta 3: Fiado a Carlos + Cobro parcial posterior
    await sales_svc.create_sale(
        ctx1,
        SaleCreateRequest(
            idempotency_key="demo-sale-003",
            items=[
                SaleItemInput(product_id=created_prods[5]["id"], quantity=Decimal("3")),
                SaleItemInput(
                    description="Pan criollo recién horneado (suelto)",
                    quantity=Decimal("1"),
                    unit_price=Decimal("2400"),
                    manual_unit_cost=Decimal("1400"),
                ),
            ],
            fiado_amount=Decimal("7500.00"),
            customer_id=c2["id"],
        ),
    )
    await customers_svc.register_customer_payment(
        ctx1,
        c2["id"],
        CustomerPaymentRequest(
            idempotency_key="demo-pay-001",
            transfer_amount=Decimal("3000.00"),
            notes="Entrega a cuenta por transferencia",
        ),
    )

    # Compra a proveedor con parte pagada y parte pendiente
    await suppliers_svc.create_purchase(
        ctx1,
        PurchaseCreateRequest(
            idempotency_key="demo-purch-001",
            supplier_id=sup1["id"],
            invoice_number="FC-C-0002-00018450",
            items=[
                PurchaseItemInput(
                    product_id=created_prods[2]["id"],
                    quantity=Decimal("12"),
                    unit_cost=Decimal("1080"),
                    new_sale_price=Decimal("1550"),
                )
            ],
            paid_cash_amount=Decimal("6960.00"),
            pending_debt_amount=Decimal("6000.00"),
            update_product_costs=True,
            notes="Reposición lácteos",
        ),
    )

    # Gasto operativo real
    await cash_svc.register_manual_cash_movement(
        ctx1,
        CashMovementCreateRequest(
            idempotency_key="demo-exp-001",
            movement_type="operating_expense",
            channel="efectivo",
            amount=Decimal("3200.00"),
            category="Bolsas e Insumos",
            description="Compra de bolsas camiseta y rollos térmicos",
        ),
    )

    # Poblar 2 artículos aislados en Kiosco El Trébol [DEMO]
    ctx2 = AuthContext(
        user_id=u2_id,
        email="demo.kiosco@mitiendita.ar",
        full_name="Valeria Gómez",
        role="owner",
        tenant_id=t2_id,
        tenant_name="Kiosco El Trébol [DEMO]",
        subscription_status="active",
        plan_id="plan_inicial",
    )
    await catalog_svc.create_product(
        ctx2,
        ProductCreateRequest(
            name="Alfajor Triple Chocolate",
            sku="KIO-101",
            category="Golosinas",
            unit="unidad",
            stock_qty=Decimal("36"),
            min_stock_qty=Decimal("10"),
            cost_price=Decimal("750"),
            sale_price=Decimal("1200"),
        ),
    )


async def main() -> None:
    db_mgr = get_db()
    await ensure_demo_data(db_mgr)
    print("[Mi Tiendita] Datos [DEMO] creados y aislados exitosamente.")
    await db_mgr.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
