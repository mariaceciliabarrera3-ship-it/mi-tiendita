"""Restore a single tenant JSON export into an empty tenant, never overwrite."""
import json
from datetime import datetime
from decimal import Decimal
from fastapi import HTTPException
from app.config import new_id
from app.db import get_db
from app.security import log_audit

# FK order is deliberate. Whitelist avoids arbitrary table/column injection.
TABLES = ['products','customers','suppliers','cash_sessions','sales','sale_items',
          'product_price_history','inventory_movements','customer_ledger','purchases',
          'purchase_items','supplier_ledger','cash_movements','fiscal_profiles','fiscal_vouchers','monthly_analysis_reports']
REFS = {
 'sale_items': {'sale_id':'sales','product_id':'products'},
 'product_price_history': {'product_id':'products'},
 'inventory_movements': {'product_id':'products'},
 'sales': {'customer_id':'customers'},
 'customer_ledger': {'customer_id':'customers','sale_id':'sales'},
 'purchases': {'supplier_id':'suppliers'},
 'purchase_items': {'purchase_id':'purchases','product_id':'products'},
 'supplier_ledger': {'supplier_id':'suppliers','purchase_id':'purchases'},
 'cash_movements': {'cash_session_id':'cash_sessions'},
 'fiscal_vouchers': {'linked_sale_id':'sales','linked_purchase_id':'purchases'},
}
ACTORS={'created_by','changed_by','opened_by','closed_by','prepared_by'}

async def restore_empty_tenant(ctx, payload):
    if not isinstance(payload,dict) or payload.get('backup_format')!='mi_tiendita_v2_json' or not isinstance(payload.get('tables'),dict):
        raise HTTPException(400,'Formato de respaldo no reconocido.')
    source=payload.get('tenant') or {}; old_tid=source.get('id'); data=payload['tables']
    if not old_tid or set(data)!=set(TABLES): raise HTTPException(400,'Respaldo incompleto o desconocido.')
    if any(not isinstance(data[t],list) for t in TABLES): raise HTTPException(400,'Tablas inválidas.')
    if sum(len(data[t]) for t in TABLES)>50000: raise HTTPException(413,'Demasiados registros para esta restauración.')
    mapping={t:{} for t in TABLES}
    for t in TABLES:
        for row in data[t]:
            if not isinstance(row,dict) or row.get('tenant_id')!=old_tid: raise HTTPException(400,'El respaldo mezcla comercios o contiene filas inválidas.')
            if t!='fiscal_profiles':
                old=row.get('id')
                if not isinstance(old,str) or not old or old in mapping[t]: raise HTTPException(400,'Identificador inválido o duplicado.')
                mapping[t][old]=new_id()
    for t, refs in REFS.items():
        for row in data[t]:
            for col, target in refs.items():
                v=row.get(col)
                if v and v not in mapping[target]: raise HTTPException(400,f'Referencia {t}.{col} fuera del respaldo.')

    async with get_db().transaction(tenant_id=ctx.tenant_id) as c:
        current_currency = await c.fetchval("SELECT currency FROM tenants WHERE id=$1", ctx.tenant_id)
        if not source.get("currency") or source["currency"] != current_currency:
            raise HTTPException(400, "La moneda del respaldo debe coincidir con la moneda del comercio destino; no se convierte automáticamente.")
        for t in TABLES:
            if await c.fetchval(f'SELECT COUNT(*) FROM {t} WHERE tenant_id=$1',ctx.tenant_id):
                raise HTTPException(409,'La restauración solo está permitida en un comercio sin datos comerciales. No se sobrescribe información.')
        for t in TABLES:
            if c.backend=='postgres':
                cols=await c.fetch("SELECT column_name AS name,data_type AS type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=$1",t)
            else:
                cols=await c.fetch(f'PRAGMA table_info({t})')
            types={r['name']:str(r['type']).lower() for r in cols}
            for original in data[t]:
                if not set(original)<=set(types): raise HTTPException(400,f'Columnas desconocidas en {t}.')
                r=dict(original);r['tenant_id']=ctx.tenant_id
                if t!='fiscal_profiles':r['id']=mapping[t][r['id']]
                for col,target in REFS.get(t,{}).items():
                    if r.get(col): r[col]=mapping[target][r[col]]
                for col in ACTORS:
                    if col in r and r[col]:r[col]=ctx.user_id
                # Identifiers in polymorphic references may point to business rows.
                if t in ('inventory_movements','cash_movements') and r.get('reference_id'):
                    old=r['reference_id']; candidates=[m[old] for m in mapping.values() if old in m]
                    r['reference_id']=candidates[0] if len(candidates)==1 else ''
                if t=='monthly_analysis_reports':
                    # Remap exact IDs within the structured analysis report.
                    flat={old:new for table in mapping.values() for old,new in table.items()}
                    flat[old_tid]=ctx.tenant_id
                    def remap(v):
                        if isinstance(v,str): return flat.get(v,v)
                        if isinstance(v,list): return [remap(x) for x in v]
                        if isinstance(v,dict): return {k:remap(x) for k,x in v.items()}
                        return v
                    try:
                        r['metrics_json']=json.dumps(remap(json.loads(r['metrics_json'])),ensure_ascii=False)
                    except (ValueError,TypeError,KeyError):
                        raise HTTPException(400,'Informe mensual inválido en el respaldo.')
                for col,val in list(r.items()):
                    typ=types[col]
                    if val is None:continue
                    if c.backend=='postgres':
                        if 'timestamp' in typ and isinstance(val,str): r[col]=datetime.fromisoformat(val.replace('Z','+00:00'))
                        elif typ=='numeric':r[col]=Decimal(str(val))
                        elif typ=='boolean':r[col]=bool(val)
                names=list(r); args=[r[k] for k in names]
                placeholders=','.join(f'${i+1}' for i in range(len(args)))
                await c.execute(f"INSERT INTO {t} ({','.join(names)}) VALUES ({placeholders})",*args)
        # Validate denormalized balances, stock and accounting equation before commit.
        for table,ledger,idfield,balance,delta in [('products','inventory_movements','product_id','stock_qty','qty_delta'),('customers','customer_ledger','customer_id','current_balance','signed_delta'),('suppliers','supplier_ledger','supplier_id','current_debt','signed_delta')]:
            rows=await c.fetch(f'SELECT id,{balance} AS balance FROM {table} WHERE tenant_id=$1',ctx.tenant_id)
            for row in rows:
                total=await c.fetchval(f'SELECT COALESCE(SUM({delta}),0) FROM {ledger} WHERE tenant_id=$1 AND {idfield}=$2',ctx.tenant_id,row['id'])
                if abs(Decimal(str(total))-Decimal(str(row['balance'])))>Decimal('0.00001'):
                    raise HTTPException(400,f'El respaldo no concilia: {table}.')
        sales=await c.fetch('SELECT total_amount,paid_cash_amount,paid_transfer_amount,paid_wallet_amount,fiado_amount FROM sales WHERE tenant_id=$1',ctx.tenant_id)
        for sale in sales:
            if Decimal(str(sale['total_amount'])) != sum(Decimal(str(sale[k])) for k in ('paid_cash_amount','paid_transfer_amount','paid_wallet_amount','fiado_amount')):
                raise HTTPException(400,'El respaldo contiene una venta con pagos inconsistentes.')
        await log_audit(c,tenant_id=ctx.tenant_id,actor_user_id=ctx.user_id,actor_role=ctx.role,action='backup.restore',entity_type='tenant',entity_id=ctx.tenant_id,details={'source_tenant':old_tid},ip_address=ctx.ip_address)
    return {'status':'restored','counts':{t:len(data[t]) for t in TABLES}}
