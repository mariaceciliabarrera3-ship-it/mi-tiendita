ALTER TABLE sales ADD COLUMN paid_wallet_amount NUMERIC NOT NULL DEFAULT 0 CHECK (paid_wallet_amount >= 0);
CREATE TABLE cash_movements_v2 (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    cash_session_id TEXT REFERENCES cash_sessions(id),
    idempotency_key TEXT NOT NULL,
    movement_type TEXT NOT NULL CHECK (movement_type IN (
        'opening', 'sale_income', 'fiado_collection', 'other_income',
        'operating_expense', 'purchase_payment', 'supplier_payment',
        'owner_withdrawal', 'cash_difference_adjustment', 'sale_void_outflow'
    )),
    channel TEXT NOT NULL CHECK (channel IN ('efectivo', 'transferencia', 'billetera')),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount NUMERIC NOT NULL CHECK (amount > 0),
    category TEXT NOT NULL DEFAULT 'General',
    reference_type TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);
INSERT INTO cash_movements_v2 SELECT * FROM cash_movements;
DROP TABLE cash_movements;
ALTER TABLE cash_movements_v2 RENAME TO cash_movements;
CREATE INDEX idx_cash_mov_tenant_date ON cash_movements(tenant_id, created_at DESC);
