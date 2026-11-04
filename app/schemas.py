from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=150)
    password: str = Field(..., min_length=4, max_length=128)


class RegisterTenantRequest(BaseModel):
    store_name: str = Field(..., min_length=2, max_length=120)
    business_type: str = Field(default="Almacén", max_length=60)
    owner_name: str = Field(..., min_length=2, max_length=100)
    email: str = Field(..., min_length=5, max_length=150)
    password: str = Field(..., min_length=8, max_length=128)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=200)
    currency: str = Field(default="ARS", pattern=r"^[A-Z]{3}$")
    timezone: str = Field(default="America/Argentina/Buenos_Aires", max_length=80)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, v):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try: ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError): raise ValueError("Zona horaria IANA inválida")
        return v

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Ingresá un correo electrónico válido.")
        return v


class CreateStaffUserRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    email: str = Field(..., min_length=5, max_length=150)
    password: str = Field(..., min_length=8, max_length=128)
    role: Literal["cashier", "viewer"] = "cashier"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class UpdateTenantSettingsRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    business_type: str | None = Field(default=None, max_length=60)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=200)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone: str | None = Field(default=None, max_length=80)
    debt_alert_days: int | None = Field(default=None, ge=1, le=365)
    debt_alert_amount: Decimal | None = Field(default=None, ge=0)
    fiscal_module_enabled: bool | None = None


class ProductCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=140)
    sku: str | None = Field(default=None, max_length=60)
    category: str = Field(default="General", max_length=80)
    unit: Literal["unidad", "kg", "g", "litro", "pack", "docena", "metro"] = "unidad"
    stock_qty: Decimal = Field(default=Decimal("0"), ge=0)
    min_stock_qty: Decimal = Field(default=Decimal("0"), ge=0)
    cost_price: Decimal = Field(..., ge=0)
    sale_price: Decimal = Field(..., ge=0)
    notes: str = Field(default="", max_length=500)


class ProductUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=140)
    sku: str | None = Field(default=None, max_length=60)
    category: str | None = Field(default=None, max_length=80)
    unit: Literal["unidad", "kg", "g", "litro", "pack", "docena", "metro"] | None = None
    min_stock_qty: Decimal | None = Field(default=None, ge=0)
    cost_price: Decimal | None = Field(default=None, ge=0)
    sale_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    price_change_reason: str = Field(default="Actualización manual", max_length=200)


class StockMovementRequest(BaseModel):
    movement_type: Literal[
        "adjustment_add",
        "adjustment_sub",
        "loss",
        "damaged",
        "customer_return",
        "supplier_return",
        "authorized_correction",
    ]
    quantity: Decimal = Field(..., gt=0)
    notes: str = Field(default="", max_length=300)


class SaleItemInput(BaseModel):
    product_id: str | None = None
    description: str | None = Field(default=None, max_length=160)
    unit: str = Field(default="unidad", max_length=20)
    quantity: Decimal = Field(..., gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    manual_unit_cost: Decimal | None = Field(default=None, ge=0)


class SaleCreateRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=120)
    items: list[SaleItemInput] = Field(..., min_length=1)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_cash_amount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_transfer_amount: Decimal = Field(default=Decimal("0"), ge=0)
    fiado_amount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_wallet_amount: Decimal = Field(default=Decimal("0"), ge=0)
    customer_id: str | None = None
    due_date: str | None = Field(default=None, max_length=20)
    notes: str = Field(default="", max_length=400)


class SaleVoidRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


class CustomerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=400)
    credit_limit: Decimal = Field(default=Decimal("0"), ge=0)
    alert_days_override: int | None = Field(default=None, ge=1, le=365)
    alert_amount_override: Decimal | None = Field(default=None, ge=0)


class CustomerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=400)
    credit_limit: Decimal | None = Field(default=None, ge=0)
    alert_days_override: int | None = Field(default=None, ge=1, le=365)
    alert_amount_override: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None


class CustomerPaymentRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=120)
    cash_amount: Decimal = Field(default=Decimal("0"), ge=0)
    transfer_amount: Decimal = Field(default=Decimal("0"), ge=0)
    wallet_amount: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str = Field(default="", max_length=300)


class SupplierCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    tax_id: str = Field(default="", max_length=30)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=400)


class PurchaseItemInput(BaseModel):
    product_id: str | None = None
    description: str | None = Field(default=None, max_length=160)
    quantity: Decimal = Field(..., gt=0)
    unit_cost: Decimal = Field(..., ge=0)
    new_sale_price: Decimal | None = Field(default=None, ge=0)


class PurchaseCreateRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=120)
    supplier_id: str
    invoice_number: str = Field(default="", max_length=60)
    items: list[PurchaseItemInput] = Field(..., min_length=1)
    paid_cash_amount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_transfer_amount: Decimal = Field(default=Decimal("0"), ge=0)
    paid_wallet_amount: Decimal = Field(default=Decimal("0"), ge=0)
    pending_debt_amount: Decimal = Field(default=Decimal("0"), ge=0)
    update_product_costs: bool = True
    notes: str = Field(default="", max_length=400)


class SupplierPaymentRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=120)
    cash_amount: Decimal = Field(default=Decimal("0"), ge=0)
    transfer_amount: Decimal = Field(default=Decimal("0"), ge=0)
    wallet_amount: Decimal = Field(default=Decimal("0"), ge=0)
    notes: str = Field(default="", max_length=300)


class CashSessionOpenRequest(BaseModel):
    opening_cash_amount: Decimal = Field(..., ge=0)
    notes: str = Field(default="", max_length=300)


class CashSessionCloseRequest(BaseModel):
    counted_cash_amount: Decimal = Field(..., ge=0)
    register_difference_adjustment: bool = False
    notes: str = Field(default="", max_length=300)


class CashMovementCreateRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=4, max_length=120)
    movement_type: Literal["other_income", "operating_expense", "owner_withdrawal"]
    channel: Literal["efectivo", "transferencia", "billetera"] = "efectivo"
    amount: Decimal = Field(..., gt=0)
    category: str = Field(default="General", max_length=80)
    description: str = Field(..., min_length=2, max_length=240)


class FiscalProfileUpdateRequest(BaseModel):
    legal_name: str = Field(default="", max_length=140)
    cuit: str = Field(default="", max_length=20)
    tax_condition: Literal[
        "no_inscripto", "monotributo", "responsable_inscripto", "exento"
    ] = "no_inscripto"
    monotributo_category: str = Field(default="", max_length=10)
    gross_income_number: str = Field(default="", max_length=40)
    point_of_sale: int = Field(default=1, ge=1, le=99999)
    fiscal_address: str = Field(default="", max_length=200)
    accountant_name: str = Field(default="", max_length=120)
    accountant_email: str = Field(default="", max_length=120)
    default_vat_rate: Decimal = Field(default=Decimal("21.00"), ge=0, le=100)


class FiscalVoucherCreateRequest(BaseModel):
    direction: Literal["emitido", "recibido"]
    voucher_type: Literal[
        "factura_a", "factura_b", "factura_c", "nota_credito", "recibo", "ticket_z"
    ]
    point_of_sale: int = Field(default=1, ge=1, le=99999)
    voucher_number: str = Field(..., min_length=1, max_length=40)
    issue_date: str = Field(..., min_length=8, max_length=20)
    counterparty_name: str = Field(default="Consumidor Final", max_length=140)
    counterparty_cuit: str = Field(default="", max_length=20)
    net_amount: Decimal = Field(default=Decimal("0"), ge=0)
    vat_amount: Decimal = Field(default=Decimal("0"), ge=0)
    exempt_amount: Decimal = Field(default=Decimal("0"), ge=0)
    total_amount: Decimal = Field(..., gt=0)
    classification: str = Field(default="mercaderia", max_length=60)
    linked_sale_id: str | None = None
    linked_purchase_id: str | None = None
    notes: str = Field(default="", max_length=300)


class AyudanteQuestionRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)


class AdminTenantUpdateRequest(BaseModel):
    plan_id: str | None = None
    subscription_status: Literal["trial", "active", "past_due", "suspended"] | None = None
    subscription_expires_at: str | None = None
    fiscal_module_enabled: bool | None = None
    monthly_analysis_enabled: bool | None = None


class AdminSubscriptionPaymentRequest(BaseModel):
    plan_id: str
    amount: Decimal = Field(..., ge=0)
    includes_analysis_addon: bool = False
    period_start: str = Field(..., min_length=8, max_length=20)
    period_end: str = Field(..., min_length=8, max_length=20)
    payment_method: Literal["transferencia", "mercadopago", "efectivo", "bonificado"] = (
        "transferencia"
    )
    reference: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=300)


class AdminMonthlyReportRequest(BaseModel):
    period_month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    executive_summary: str = Field(default="", max_length=4000)
    recommendations: str = Field(default="", max_length=4000)
    publish: bool = False


class EmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=150)

class VerifyRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)

class ResetRequest(VerifyRequest):
    password: str = Field(min_length=8, max_length=128)

class AdminServiceChangeRequest(BaseModel):
    confirmation: Literal["CONFIRMAR"]
    reason: str = Field(..., min_length=3, max_length=300)
