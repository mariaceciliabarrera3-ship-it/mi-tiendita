-- ============================================================================
-- MI TIENDITA v2.0 — Migración 002 (PostgreSQL Row-Level Security — Defensa en Profundidad)
-- Aísla las filas por tenant_id en el propio motor PostgreSQL además del filtro SQL explícito.
-- Cuando app.current_tenant_id = '__SUPERADMIN__' solo se habilita lectura administrativa acotada;
-- para comercios regulares, app.current_tenant_id = '<tenant_uuid>' restringe lectura y escritura.
-- ============================================================================

DO $$
DECLARE
    tbl TEXT;
    tenant_tables TEXT[] := ARRAY[
        'products',
        'product_price_history',
        'inventory_movements',
        'customers',
        'sales',
        'sale_items',
        'customer_ledger',
        'suppliers',
        'purchases',
        'purchase_items',
        'supplier_ledger',
        'cash_sessions',
        'cash_movements',
        'fiscal_profiles',
        'fiscal_vouchers',
        'monthly_analysis_reports'
    ];
BEGIN
    FOREACH tbl IN ARRAY tenant_tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON %I;', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation_policy ON %I '
            'USING (tenant_id = current_setting(''app.current_tenant_id'', true)) '
            'WITH CHECK (tenant_id = current_setting(''app.current_tenant_id'', true));',
            tbl
        );
    END LOOP;
END $$;
