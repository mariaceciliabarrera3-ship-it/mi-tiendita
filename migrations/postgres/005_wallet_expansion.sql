ALTER TABLE customer_ledger ADD COLUMN wallet_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(wallet_amount >= 0);
ALTER TABLE supplier_ledger ADD COLUMN wallet_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(wallet_amount >= 0);
ALTER TABLE purchases ADD COLUMN paid_wallet_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(paid_wallet_amount >= 0);
