import io
from typing import Any

from fastapi import HTTPException
from openpyxl import Workbook

from app.config import money, money_float, new_id, utc_now
from app.db import get_db
from app.schemas import FiscalProfileUpdateRequest, FiscalVoucherCreateRequest
from app.security import AuthContext, log_audit
from app.xlsx_safe import neutralize_formulas


LEGAL_DISCLAIMER = (
    "AVISO IMPORTANTE: Este módulo organiza comprobantes y clasificaciones administrativas "
    "para facilitar el trabajo con tu estudio contable. NO reemplaza el asesoramiento de un "
    "Contador Público matriculado ni constituye un emisor homologado de factura electrónica "
    "ante ARCA/AFIP sin integración expresa de certificados fiscales."
)


async def get_fiscal_module_data(ctx: AuthContext) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    tid = ctx.tenant_id

    async with db_mgr.connection(tenant_id=tid) as conn:
        tenant = await conn.fetchrow(
            "SELECT fiscal_module_enabled FROM tenants WHERE id = $1", tid
        )
        profile = await conn.fetchrow(
            "SELECT * FROM fiscal_profiles WHERE tenant_id = $1", tid
        )
        vouchers = await conn.fetch(
            "SELECT * FROM fiscal_vouchers WHERE tenant_id = $1 ORDER BY issue_date DESC, created_at DESC LIMIT 200",
            tid,
        )

    enabled = bool(tenant["fiscal_module_enabled"]) if tenant else False
    prof_dict = (
        {
            "legal_name": profile["legal_name"],
            "cuit": profile["cuit"],
            "tax_condition": profile["tax_condition"],
            "monotributo_category": profile["monotributo_category"],
            "gross_income_number": profile["gross_income_number"],
            "point_of_sale": int(profile["point_of_sale"]),
            "fiscal_address": profile["fiscal_address"],
            "accountant_name": profile["accountant_name"],
            "accountant_email": profile["accountant_email"],
            "default_vat_rate": money_float(profile["default_vat_rate"]),
        }
        if profile
        else {
            "legal_name": "",
            "cuit": "",
            "tax_condition": "no_inscripto",
            "monotributo_category": "",
            "gross_income_number": "",
            "point_of_sale": 1,
            "fiscal_address": "",
            "accountant_name": "",
            "accountant_email": "",
            "default_vat_rate": 21.0,
        }
    )

    total_emitido = sum(
        money_float(v["total_amount"]) for v in vouchers if v["direction"] == "emitido"
    )
    total_recibido = sum(
        money_float(v["total_amount"]) for v in vouchers if v["direction"] == "recibido"
    )
    vat_debito = sum(
        money_float(v["vat_amount"]) for v in vouchers if v["direction"] == "emitido"
    )
    vat_credito = sum(
        money_float(v["vat_amount"]) for v in vouchers if v["direction"] == "recibido"
    )

    return {
        "enabled": enabled,
        "disclaimer": LEGAL_DISCLAIMER,
        "profile": prof_dict,
        "summary": {
            "total_vouchers_emitted": round(total_emitido, 2),
            "total_vouchers_received": round(total_recibido, 2),
            "vat_debit_fiscal": round(vat_debito, 2),
            "vat_credit_fiscal": round(vat_credito, 2),
            "vat_technical_balance": round(vat_debito - vat_credito, 2),
        },
        "vouchers": [
            {
                "id": v["id"],
                "direction": v["direction"],
                "voucher_type": v["voucher_type"],
                "point_of_sale": int(v["point_of_sale"]),
                "voucher_number": v["voucher_number"],
                "issue_date": v["issue_date"],
                "counterparty_name": v["counterparty_name"],
                "counterparty_cuit": v["counterparty_cuit"],
                "net_amount": money_float(v["net_amount"]),
                "vat_amount": money_float(v["vat_amount"]),
                "exempt_amount": money_float(v["exempt_amount"]),
                "total_amount": money_float(v["total_amount"]),
                "classification": v["classification"],
                "notes": v["notes"] or "",
            }
            for v in vouchers
        ],
    }


async def toggle_fiscal_module(ctx: AuthContext, enabled: bool) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    async with db_mgr.transaction(tenant_id=ctx.tenant_id) as conn:
        await conn.execute(
            "UPDATE tenants SET fiscal_module_enabled = $1, updated_at = $2 WHERE id = $3",
            enabled,
            now,
            ctx.tenant_id,
        )
        await log_audit(
            conn,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="fiscal.toggle",
            entity_type="tenant",
            entity_id=ctx.tenant_id,
            details={"enabled": enabled},
            ip_address=ctx.ip_address,
        )
    return await get_fiscal_module_data(ctx)


async def upsert_fiscal_profile(
    ctx: AuthContext, data: FiscalProfileUpdateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    tid = ctx.tenant_id

    async with db_mgr.transaction(tenant_id=tid) as conn:
        await conn.execute(
            "UPDATE tenants SET fiscal_module_enabled = $1, updated_at = $2 WHERE id = $3",
            True,
            now,
            tid,
        )
        exists = await conn.fetchval(
            "SELECT tenant_id FROM fiscal_profiles WHERE tenant_id = $1", tid
        )
        if exists:
            await conn.execute(
                """
                UPDATE fiscal_profiles
                SET legal_name = $1, cuit = $2, tax_condition = $3,
                    monotributo_category = $4, gross_income_number = $5,
                    point_of_sale = $6, fiscal_address = $7,
                    accountant_name = $8, accountant_email = $9,
                    default_vat_rate = $10, updated_at = $11
                WHERE tenant_id = $12
                """,
                data.legal_name.strip(),
                data.cuit.strip(),
                data.tax_condition,
                data.monotributo_category.strip().upper(),
                data.gross_income_number.strip(),
                data.point_of_sale,
                data.fiscal_address.strip(),
                data.accountant_name.strip(),
                data.accountant_email.strip(),
                money(data.default_vat_rate),
                now,
                tid,
            )
        else:
            await conn.execute(
                """
                INSERT INTO fiscal_profiles (
                    tenant_id, legal_name, cuit, tax_condition,
                    monotributo_category, gross_income_number, point_of_sale,
                    fiscal_address, accountant_name, accountant_email,
                    default_vat_rate, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                tid,
                data.legal_name.strip(),
                data.cuit.strip(),
                data.tax_condition,
                data.monotributo_category.strip().upper(),
                data.gross_income_number.strip(),
                data.point_of_sale,
                data.fiscal_address.strip(),
                data.accountant_name.strip(),
                data.accountant_email.strip(),
                money(data.default_vat_rate),
                now,
            )

        await log_audit(
            conn,
            tenant_id=tid,
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
            action="fiscal.profile_update",
            entity_type="fiscal_profile",
            entity_id=tid,
            details={"tax_condition": data.tax_condition, "cuit": data.cuit},
            ip_address=ctx.ip_address,
        )

    return await get_fiscal_module_data(ctx)


async def create_fiscal_voucher(
    ctx: AuthContext, data: FiscalVoucherCreateRequest
) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    db_mgr = get_db()
    now = utc_now()
    tid = ctx.tenant_id
    vid = new_id()

    async with db_mgr.transaction(tenant_id=tid) as conn:
        enabled = await conn.fetchval(
            "SELECT fiscal_module_enabled FROM tenants WHERE id = $1", tid
        )
        if not bool(enabled):
            raise HTTPException(
                status_code=400,
                detail="Activá primero el Módulo Contable e Impositivo Opcional para registrar comprobantes.",
            )

        if data.linked_sale_id and not await conn.fetchval("SELECT id FROM sales WHERE id = $1 AND tenant_id = $2", data.linked_sale_id, tid):
            raise HTTPException(status_code=404, detail="Venta vinculada no encontrada en este comercio.")
        if data.linked_purchase_id and not await conn.fetchval("SELECT id FROM purchases WHERE id = $1 AND tenant_id = $2", data.linked_purchase_id, tid):
            raise HTTPException(status_code=404, detail="Compra vinculada no encontrada en este comercio.")
        if money(data.net_amount + data.vat_amount + data.exempt_amount) != money(data.total_amount):
            raise HTTPException(status_code=400, detail="Neto, IVA y exento deben sumar el total.")
        await conn.execute(
            """
            INSERT INTO fiscal_vouchers (
                id, tenant_id, direction, voucher_type, point_of_sale,
                voucher_number, issue_date, counterparty_name, counterparty_cuit,
                net_amount, vat_amount, exempt_amount, total_amount,
                classification, linked_sale_id, linked_purchase_id,
                notes, created_by, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            """,
            vid,
            tid,
            data.direction,
            data.voucher_type,
            data.point_of_sale,
            data.voucher_number.strip(),
            data.issue_date.strip(),
            data.counterparty_name.strip(),
            data.counterparty_cuit.strip(),
            money(data.net_amount),
            money(data.vat_amount),
            money(data.exempt_amount),
            money(data.total_amount),
            data.classification.strip(),
            data.linked_sale_id,
            data.linked_purchase_id,
            data.notes.strip(),
            ctx.user_id,
            now,
        )

    return await get_fiscal_module_data(ctx)


async def export_accountant_xlsx(ctx: AuthContext) -> bytes:
    """Exporta reporte estructurado de comprobantes y resumen comercial para el Contador."""
    fisc = await get_fiscal_module_data(ctx)
    wb = Workbook()
    ws = wb.active
    ws.title = "Libro_Comprobantes"
    ws.append([LEGAL_DISCLAIMER])
    ws.append(
        [
            "Dirección",
            "Tipo",
            "Pto Venta",
            "Número",
            "Fecha",
            "Contraparte",
            "CUIT",
            "Neto Gravado",
            "IVA",
            "Exento",
            "Total",
            "Clasificación",
        ]
    )
    for v in fisc["vouchers"]:
        ws.append(
            [
                v["direction"],
                v["voucher_type"],
                v["point_of_sale"],
                v["voucher_number"],
                v["issue_date"],
                v["counterparty_name"],
                v["counterparty_cuit"],
                v["net_amount"],
                v["vat_amount"],
                v["exempt_amount"],
                v["total_amount"],
                v["classification"],
            ]
        )
    neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
