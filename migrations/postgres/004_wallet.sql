ALTER TABLE sales ADD COLUMN paid_wallet_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (paid_wallet_amount >= 0);
ALTER TABLE cash_movements DROP CONSTRAINT cash_movements_channel_check;
ALTER TABLE cash_movements ADD CONSTRAINT cash_movements_channel_check CHECK (channel IN ('efectivo','transferencia','billetera'));
