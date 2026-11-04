-- ============================================================================
-- MI TIENDITA v2.0 — Migración 001 (PostgreSQL / Neon Serverless)
-- Esquema Definitivo Multitenant con Integridad Financiera y de Inventario
-- ============================================================================

CREATE TABLE IF NOT EXISTS subscription_plans (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    monthly_price_ars NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (monthly_price_ars >= 0),
    max_products INTEGER NOT NULL DEFAULT 500,
    max_users INTEGER NOT NULL DEFAULT 2,
    includes_fiscal_module BOOLEAN NOT NULL DEFAULT FALSE,
    includes_monthly_analysis BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL DEFAULT 'Almacén',
    owner_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'ARS',
    plan_id TEXT NOT NULL REFERENCES subscription_plans(id),
    subscription_status TEXT NOT NULL DEFAULT 'active' CHECK (subscription_status IN ('trial', 'active', 'past_due', 'suspended')),
    subscription_expires_at TIMESTAMPTZ,
    fiscal_module_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    monthly_analysis_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    debt_alert_days INTEGER NOT NULL DEFAULT 15 CHECK (debt_alert_days >= 1),
    debt_alert_amount NUMERIC(14,2) NOT NULL DEFAULT 20000.00 CHECK (debt_alert_amount >= 0),
    is_demo BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscription_payments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    plan_id TEXT NOT NULL REFERENCES subscription_plans(id),
    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
    includes_analysis_addon BOOLEAN NOT NULL DEFAULT FALSE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    recorded_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES tenants(id),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('superadmin', 'owner', 'cashier', 'viewer')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    token_version INTEGER NOT NULL DEFAULT 1,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    sku TEXT,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    unit TEXT NOT NULL DEFAULT 'unidad',
    stock_qty NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    min_stock_qty NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (min_stock_qty >= 0),
    cost_price NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (cost_price >= 0),
    sale_price NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (sale_price >= 0),
    notes TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id, is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_tenant_sku ON products(tenant_id, sku) WHERE sku IS NOT NULL AND sku <> '';

CREATE TABLE IF NOT EXISTS product_price_history (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    old_cost NUMERIC(14,2) NOT NULL,
    new_cost NUMERIC(14,2) NOT NULL,
    old_sale_price NUMERIC(14,2) NOT NULL,
    new_sale_price NUMERIC(14,2) NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    changed_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_price_history_product ON product_price_history(tenant_id, product_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inventory_movements (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    movement_type TEXT NOT NULL CHECK (movement_type IN (
        'initial_load', 'sale', 'purchase', 'adjustment_add', 'adjustment_sub',
        'loss', 'damaged', 'customer_return', 'supplier_return', 'authorized_correction', 'sale_void'
    )),
    qty_delta NUMERIC(14,3) NOT NULL,
    stock_before NUMERIC(14,3) NOT NULL,
    stock_after NUMERIC(14,3) NOT NULL CHECK (stock_after >= 0),
    unit_cost_snapshot NUMERIC(14,2) NOT NULL DEFAULT 0,
    reference_type TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_inv_mov_tenant_product ON inventory_movements(tenant_id, product_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    credit_limit NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (credit_limit >= 0),
    current_balance NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (current_balance >= 0),
    alert_days_override INTEGER,
    alert_amount_override NUMERIC(14,2),
    oldest_unpaid_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_customers_tenant ON customers(tenant_id, is_active);

CREATE TABLE IF NOT EXISTS sales (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    sale_number INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    customer_id TEXT REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'voided')),
    subtotal_amount NUMERIC(14,2) NOT NULL CHECK (subtotal_amount >= 0),
    discount_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    total_amount NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),
    total_cost_snapshot NUMERIC(14,2) NOT NULL DEFAULT 0,
    gross_profit_snapshot NUMERIC(14,2) NOT NULL DEFAULT 0,
    paid_cash_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (paid_cash_amount >= 0),
    paid_transfer_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (paid_transfer_amount >= 0),
    fiado_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (fiado_amount >= 0),
    payment_summary TEXT NOT NULL,
    has_missing_cost BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT NOT NULL DEFAULT '',
    voided_reason TEXT NOT NULL DEFAULT '',
    voided_at TIMESTAMPTZ,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, sale_number)
);
CREATE INDEX IF NOT EXISTS idx_sales_tenant_date ON sales(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS sale_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    sale_id TEXT NOT NULL REFERENCES sales(id),
    product_id TEXT REFERENCES products(id),
    description TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'unidad',
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit_price_snapshot NUMERIC(14,2) NOT NULL CHECK (unit_price_snapshot >= 0),
    unit_cost_snapshot NUMERIC(14,2) NOT NULL DEFAULT 0,
    cost_known BOOLEAN NOT NULL DEFAULT TRUE,
    line_subtotal NUMERIC(14,2) NOT NULL,
    line_cost_total NUMERIC(14,2) NOT NULL,
    line_gross_profit NUMERIC(14,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(tenant_id, sale_id);

CREATE TABLE IF NOT EXISTS customer_ledger (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('fiado_charge', 'payment', 'adjustment_charge', 'sale_void_reversal')),
    idempotency_key TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    signed_delta NUMERIC(14,2) NOT NULL,
    remaining_unpaid NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (remaining_unpaid >= 0),
    balance_before NUMERIC(14,2) NOT NULL,
    balance_after NUMERIC(14,2) NOT NULL CHECK (balance_after >= 0),
    payment_method TEXT NOT NULL DEFAULT '',
    cash_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (cash_amount >= 0),
    transfer_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (transfer_amount >= 0),
    sale_id TEXT REFERENCES sales(id),
    due_date TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_customer_ledger_cust ON customer_ledger(tenant_id, customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    tax_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    current_debt NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (current_debt >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_suppliers_tenant ON suppliers(tenant_id, is_active);

CREATE TABLE IF NOT EXISTS purchases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    purchase_number INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    invoice_number TEXT NOT NULL DEFAULT '',
    total_amount NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),
    paid_cash_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (paid_cash_amount >= 0),
    paid_transfer_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (paid_transfer_amount >= 0),
    pending_debt_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (pending_debt_amount >= 0),
    payment_status TEXT NOT NULL CHECK (payment_status IN ('paid', 'partial', 'pending')),
    updated_costs BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, purchase_number)
);
CREATE INDEX IF NOT EXISTS idx_purchases_tenant ON purchases(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS purchase_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    purchase_id TEXT NOT NULL REFERENCES purchases(id),
    product_id TEXT REFERENCES products(id),
    description TEXT NOT NULL,
    quantity NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit_cost NUMERIC(14,2) NOT NULL CHECK (unit_cost >= 0),
    new_sale_price NUMERIC(14,2),
    line_total NUMERIC(14,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_ledger (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('purchase_debt', 'payment', 'adjustment')),
    idempotency_key TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    signed_delta NUMERIC(14,2) NOT NULL,
    balance_before NUMERIC(14,2) NOT NULL,
    balance_after NUMERIC(14,2) NOT NULL CHECK (balance_after >= 0),
    payment_method TEXT NOT NULL DEFAULT '',
    cash_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    transfer_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    purchase_id TEXT REFERENCES purchases(id),
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS cash_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opening_cash_amount NUMERIC(14,2) NOT NULL CHECK (opening_cash_amount >= 0),
    expected_cash_amount NUMERIC(14,2),
    counted_cash_amount NUMERIC(14,2),
    cash_difference_amount NUMERIC(14,2),
    total_transfer_in NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_transfer_out NUMERIC(14,2) NOT NULL DEFAULT 0,
    opened_by TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_by TEXT,
    closed_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cash_sessions_tenant ON cash_sessions(tenant_id, status);

CREATE TABLE IF NOT EXISTS cash_movements (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    cash_session_id TEXT REFERENCES cash_sessions(id),
    idempotency_key TEXT NOT NULL,
    movement_type TEXT NOT NULL CHECK (movement_type IN (
        'opening', 'sale_income', 'fiado_collection', 'other_income',
        'operating_expense', 'purchase_payment', 'supplier_payment',
        'owner_withdrawal', 'cash_difference_adjustment', 'sale_void_outflow'
    )),
    channel TEXT NOT NULL CHECK (channel IN ('efectivo', 'transferencia')),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL DEFAULT 'General',
    reference_type TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_cash_mov_tenant_date ON cash_movements(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fiscal_profiles (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    legal_name TEXT NOT NULL DEFAULT '',
    cuit TEXT NOT NULL DEFAULT '',
    tax_condition TEXT NOT NULL DEFAULT 'no_inscripto' CHECK (tax_condition IN ('no_inscripto', 'monotributo', 'responsable_inscripto', 'exento')),
    monotributo_category TEXT NOT NULL DEFAULT '',
    gross_income_number TEXT NOT NULL DEFAULT '',
    point_of_sale INTEGER NOT NULL DEFAULT 1,
    fiscal_address TEXT NOT NULL DEFAULT '',
    accountant_name TEXT NOT NULL DEFAULT '',
    accountant_email TEXT NOT NULL DEFAULT '',
    default_vat_rate NUMERIC(5,2) NOT NULL DEFAULT 21.00,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fiscal_vouchers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    direction TEXT NOT NULL CHECK (direction IN ('emitido', 'recibido')),
    voucher_type TEXT NOT NULL CHECK (voucher_type IN ('factura_a', 'factura_b', 'factura_c', 'nota_credito', 'recibo', 'ticket_z')),
    point_of_sale INTEGER NOT NULL DEFAULT 1,
    voucher_number TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    counterparty_name TEXT NOT NULL DEFAULT 'Consumidor Final',
    counterparty_cuit TEXT NOT NULL DEFAULT '',
    net_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    vat_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    exempt_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_amount NUMERIC(14,2) NOT NULL,
    classification TEXT NOT NULL DEFAULT 'mercaderia',
    linked_sale_id TEXT REFERENCES sales(id),
    linked_purchase_id TEXT REFERENCES purchases(id),
    notes TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monthly_analysis_reports (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    period_month TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    metrics_json TEXT NOT NULL,
    executive_summary TEXT NOT NULL DEFAULT '',
    recommendations TEXT NOT NULL DEFAULT '',
    prepared_by TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, period_month)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    actor_user_id TEXT,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_date ON audit_logs(tenant_id, created_at DESC);
