"""Pruebas de integración HTTP y transacciones, sin datos comerciales reales."""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app.db import DatabaseManager, set_db
from app.main import app

@pytest.fixture(autouse=True)
def mail_stub(monkeypatch):
    global SENT
    import app.auth_actions as actions
    async def fake_mail(address, purpose, token):
        SENT[(address,purpose)] = token
    SENT = {}
    monkeypatch.setattr(actions, "send_action_email", fake_mail)

@pytest.fixture
def client(tmp_path):
    # TEST_DATABASE_URL permite ejecutar el mismo conjunto sobre PostgreSQL dedicado y vacío.
    url=os.environ.get('TEST_DATABASE_URL',f'sqlite:///{tmp_path}/test.db')
    set_db(DatabaseManager(url))
    with TestClient(app, raise_server_exceptions=True, client=(uuid.uuid4().hex, 50000)) as c:
        yield c


def register(c, suffix=None, open_cash=True):
    suffix=(suffix+'-' if suffix else '')+uuid.uuid4().hex[:12]
    email=suffix+'@ejemplo.test'
    r=c.post('/api/v1/auth/register',json={'store_name':'Negocio '+suffix,'owner_name':'Ana Dueña','email':email,'password':'password-seguro-123'})
    assert r.status_code==200,r.text
    assert r.json()['status']=='verification_pending'
    token=SENT[(email,'verify_email')]
    assert c.post('/api/v1/auth/verify-email',json={'token':token}).status_code==200
    login=c.post('/api/v1/auth/login',json={'email':email,'password':'password-seguro-123'})
    assert login.status_code==200,login.text
    h={'Authorization':'Bearer '+login.json()['access_token']}
    if open_cash:
        assert c.post('/api/v1/cash/open',headers=h,json={'opening_cash_amount':0}).status_code==201
    return h

def post(c,path,data,h):
    r=c.post('/api/v1'+path,json=data,headers=h)
    assert r.status_code in (200,201),r.text
    return r.json()

def get(c,path,h):
    r=c.get('/api/v1'+path,headers=h);assert r.status_code==200,r.text
    return r.json()

def product(c,h,stock=10,cost=500,price=800):
    return post(c,'/products',{'name':'Producto de prueba','stock_qty':stock,'min_stock_qty':2,'cost_price':cost,'sale_price':price},h)

def sale(c,h,pid,key,quantity,cash,transfer=0,fiado=0,customer=None):
    return post(c,'/sales',{'idempotency_key':key,'items':[{'product_id':pid,'quantity':quantity}], 'paid_cash_amount':cash,'paid_transfer_amount':transfer,'fiado_amount':fiado,'customer_id':customer},h)

def test_A_venta_simple_y_D_costo_historico(client):
    h=register(client);p=product(client,h);s=sale(client,h,p['id'],'A-sale-123',2,1600)
    assert (s['total_amount'],s['total_cost_snapshot'],s['gross_profit_snapshot'])==(1600,1000,600)
    assert get(client,'/products',h)['items'][0]['stock_qty']==8
    r=client.patch('/api/v1/products/'+p['id'],json={'cost_price':700},headers=h);assert r.status_code==200,r.text
    assert get(client,'/sales/'+s['id'],h)['gross_profit_snapshot']==600
    assert get(client,'/products/'+p['id']+'/history',h)['stock_matches_kardex']

def test_B_C_G_pago_combinado_parcial_consistencia(client):
    h=register(client);p=product(client,h,stock=10,cost=5000,price=10000)
    cu=post(client,'/customers',{'name':'Cliente A'},h)
    sale(client,h,p['id'],'B-sale-123',1,4000,3000,3000,cu['id'])
    detail=get(client,'/customers/'+cu['id'],h);assert detail['customer']['current_balance']==3000
    payment={'idempotency_key':'C-pago-123','cash_amount':1000,'transfer_amount':0}
    a=post(client,'/customers/'+cu['id']+'/payments',payment,h);assert a['balance_after']==2000
    b=post(client,'/customers/'+cu['id']+'/payments',payment,h);assert b['already_processed'] and b['balance_after']==2000
    f=get(client,'/reports/financial',h)
    assert f['economic_results']['sales_total']==10000
    assert f['cash_and_receivables']['money_collected_in_sales']==7000
    assert f['cash_and_receivables']['fiado_collections_received']==1000
    assert f['cash_and_receivables']['total_pending_to_collect_fiados']==2000
    assert get(client,'/reports/verify-consistency',h)['is_mathematically_consistent']

def test_E_aislamiento_tenant(client):
    h1=register(client);h2=register(client);p=product(client,h1)
    assert not get(client,'/products',h2)['items']
    assert client.patch('/api/v1/products/'+p['id'],headers=h2,json={'sale_price':1}).status_code==404
    assert client.get('/api/v1/products/'+p['id']+'/history',headers=h2).status_code==404
    assert client.post('/api/v1/sales',headers=h2,json={'idempotency_key':'cross-test','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}).status_code==404

def test_F_concurrencia_stock(client):
    h=register(client);p=product(client,h,stock=1)
    barrier=threading.Barrier(2)
    def req(i):
        barrier.wait();return client.post('/api/v1/sales',headers=h,json={'idempotency_key':f'race-{i}-unique','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}).status_code
    with ThreadPoolExecutor(max_workers=2) as ex: results=list(ex.map(req,range(2)))
    assert sorted(results)==[201,409],results
    assert get(client,'/products',h)['items'][0]['stock_qty']==0

def test_idempotencia_reintentos_y_rechazo_pago_erroneo(client):
    h=register(client);p=product(client,h)
    body={'idempotency_key':'retry-sale-test','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}
    a=post(client,'/sales',body,h);b=post(client,'/sales',body,h);assert a['id']==b['id']
    assert get(client,'/products',h)['items'][0]['stock_qty']==9
    body['idempotency_key']='bad-total-sale';body['paid_cash_amount']=200
    assert client.post('/api/v1/sales',headers=h,json=body).status_code==400
    assert get(client,'/products',h)['items'][0]['stock_qty']==9

def test_H_persistencia_reinicio(tmp_path):
    path=tmp_path/'restart.db';url=f'sqlite:///{path}';set_db(DatabaseManager(url))
    with TestClient(app) as c:
        h=register(c);p=product(c,h)
    set_db(DatabaseManager(url))
    with TestClient(app) as c:
        assert get(c,'/products',h)['items'][0]['id']==p['id']

def test_compra_pago_y_stock(client):
    h=register(client);p=product(client,h);sup=post(client,'/suppliers',{'name':'Proveedor Uno'},h)
    post(client,'/suppliers/purchases',{'idempotency_key':'compratest01','supplier_id':sup['id'],'items':[{'product_id':p['id'],'quantity':2,'unit_cost':600}],'paid_cash_amount':200,'pending_debt_amount':1000},h)
    assert get(client,'/products',h)['items'][0]['stock_qty']==12
    out=post(client,f"/suppliers/{sup['id']}/payments",{'idempotency_key':'proveedorpay01','cash_amount':400},h)
    assert out['debt_after']==600
    assert get(client,'/reports/verify-consistency',h)['is_mathematically_consistent']

def test_fiado_cobrado_impide_anulacion_incompleta(client):
    h=register(client);p=product(client,h,price=3000);cu=post(client,'/customers',{'name':'Cliente'},h)
    s=sale(client,h,p['id'],'fiado-sale001',1,0,0,3000,cu['id'])
    post(client,f"/customers/{cu['id']}/payments",{'idempotency_key':'pay-partial01','cash_amount':1000},h)
    r=client.post('/api/v1/sales/'+s['id']+'/void',json={'reason':'error en venta'},headers=h)
    assert r.status_code==409

def test_origen_y_rol(client):
    h=register(client)
    assert client.post('/api/v1/products',headers={**h,'origin':'https://attacker.example'},json={'name':'x','cost_price':1,'sale_price':2}).status_code==403
    assert client.get('/api/v1/admin/overview',headers=h).status_code==403

def test_registration_rate_limit_persisted(client):
    # Invalid duplicate attempts still consume registration budget.
    h=register(client, 'limit1')
    body={'store_name':'Tienda','owner_name':'Una persona','email':get(client,'/auth/me',h)['user']['email'],'password':'password-seguro-123'}
    for _ in range(4): assert client.post('/api/v1/auth/register',json=body).status_code==409
    assert client.post('/api/v1/auth/register',json=body).status_code==429

def test_verify_reset_and_logout_revoke(client):
    email=uuid.uuid4().hex+'@ejemplo.test'; pw='password-seguro-123'
    body={'store_name':'Seguridad','owner_name':'Ana','email':email,'password':pw}
    assert client.post('/api/v1/auth/register',json=body).json()['status']=='verification_pending'
    assert client.post('/api/v1/auth/login',json={'email':email,'password':pw}).status_code==403
    token=SENT[(email,'verify_email')]
    assert client.post('/api/v1/auth/verify-email',json={'token':token}).status_code==200
    assert client.post('/api/v1/auth/verify-email',json={'token':token}).status_code==400
    r=client.post('/api/v1/auth/login',json={'email':email,'password':pw}); old=r.json()['access_token']
    assert client.post('/api/v1/auth/logout',headers={'Authorization':'Bearer '+old}).status_code==200
    assert client.get('/api/v1/auth/me',headers={'Authorization':'Bearer '+old}).status_code==401
    assert client.post('/api/v1/auth/forgot-password',json={'email':email}).status_code==200
    reset=SENT[(email,'reset_password')]
    assert client.post('/api/v1/auth/reset-password',json={'token':reset,'password':'nueva-clave-123'}).status_code==200
    assert client.post('/api/v1/auth/reset-password',json={'token':reset,'password':'otra-clave-123'}).status_code==400
    assert client.post('/api/v1/auth/login',json={'email':email,'password':pw}).status_code==401
    assert client.post('/api/v1/auth/login',json={'email':email,'password':'nueva-clave-123'}).status_code==200

def test_caja_cerrada_rollback_y_conciliacion(client):
    h=register(client,open_cash=False); p=product(client,h)
    body={'idempotency_key':'closed-cash-1','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}
    r=client.post('/api/v1/sales',headers=h,json=body);assert r.status_code==409,r.text
    assert get(client,'/products',h)['items'][0]['stock_qty']==10
    assert client.post('/api/v1/cash/movements',headers=h,json={'idempotency_key':'closed-expense','movement_type':'operating_expense','channel':'efectivo','amount':10,'description':'Gasto'}).status_code==409
    post(client,'/cash/open',{'opening_cash_amount':100},h)
    post(client,'/sales',body,h)
    close=post(client,'/cash/close',{'counted_cash_amount':850},h)
    assert close['expected_cash_amount']==900 and close['cash_difference_amount']==-50
    assert get(client,'/cash',h)['global_balances']['physical_cash_balance']==850
    post(client,'/cash/open',{'opening_cash_amount':200},h)
    state=get(client,'/cash',h)
    assert state['global_balances']['physical_cash_balance']==200
    assert state['open_session']['expected_physical_cash']==200

def test_wallet_sales_and_reports(client):
    h=register(client);p=product(client,h,price=1000)
    s=post(client,'/sales',{'idempotency_key':'wallet-sale-123','items':[{'product_id':p['id'],'quantity':1}], 'paid_cash_amount':200,'paid_transfer_amount':300,'paid_wallet_amount':500},h)
    assert s['paid_wallet_amount']==500 and s['money_received_amount']==1000
    cash=get(client,'/cash',h); assert cash['global_balances']['wallet_balance']==500 and cash['global_balances']['transfer_balance']==300
    f=get(client,'/reports/financial',h);assert f['cash_and_receivables']['collected_by_wallet']==500 and f['cash_and_receivables']['collected_by_transfer']==300
    assert get(client,'/reports/verify-consistency',h)['is_mathematically_consistent']

def test_restore_backup_empty_and_isolation(client):
    a=register(client,'source');p=product(client,a);sale(client,a,p['id'],'restore-sale-1',1,800)
    backup=client.get('/api/v1/reports/backup.json',headers=a);assert backup.status_code==200
    b=register(client,'target',open_cash=False)
    res=client.post('/api/v1/reports/restore.json',headers=b,files={'file':('backup.json',backup.content,'application/json')})
    assert res.status_code==200,res.text
    products=get(client,'/products',b)['items'];assert len(products)==1 and products[0]['stock_qty']==9 and products[0]['id']!=p['id']
    assert get(client,'/reports/verify-consistency',b)['is_mathematically_consistent']
    assert client.post('/api/v1/reports/restore.json',headers=b,files={'file':('backup.json',backup.content,'application/json')}).status_code==409

def test_locale_timezone_client_total_and_free_plan(client):
    from app.services.reports import _extract_ym
    assert _extract_ym('2026-03-01T01:00:00+00:00','America/New_York')=='2026-02'
    h=register(client);me=get(client,'/auth/me',h);assert me['tenant']['currency']=='ARS'
    assert client.patch('/api/v1/auth/tenant-settings',headers=h,json={'currency':'EUR','timezone':'Europe/Madrid','debt_alert_days':7}).status_code==200
    m=get(client,'/auth/me',h)['tenant']; assert m['currency']=='EUR' and m['timezone']=='Europe/Madrid' and m['debt_alert_days']==7
    p=product(client,h);cu=post(client,'/customers',{'name':'Comprador'},h);sale(client,h,p['id'],'history-client-1',1,800,customer=cu['id'])
    assert get(client,'/customers/'+cu['id'],h)['lifetime_purchases']==800
    a=client.post('/api/v1/auth/login',json={'email':'propietaria@mitiendita.ar','password':'MiTienditaAdmin2026!'});assert a.status_code==200
    admin={'Authorization':'Bearer '+a.json()['access_token']}
    overview=get(client,'/admin/overview',admin);assert 'plan_gratuito' in [p['id'] for p in overview['plans']]
    assert m['plan_id']!='plan_gratuito'

def test_two_real_cashiers_simultaneous(client):
    owner=register(client);p=product(client,owner,stock=1)
    tenant=get(client,'/auth/me',owner)['tenant']['id']
    login=client.post('/api/v1/auth/login',json={'email':'propietaria@mitiendita.ar','password':'MiTienditaAdmin2026!'}).json()
    admin={'Authorization':'Bearer '+login['access_token']}
    assert client.patch('/api/v1/admin/tenants/'+tenant,headers=admin,json={'plan_id':'plan_pro'}).status_code==200
    tokens=[]
    for i in range(2):
        email=f'cajero{i}-{uuid.uuid4().hex}@ejemplo.test';post(client,'/auth/staff',{'full_name':f'Cajero {i}','email':email,'password':'clave-segura-123','role':'cashier'},owner)
        r=client.post('/api/v1/auth/login',json={'email':email,'password':'clave-segura-123'});assert r.status_code==200
        tokens.append(r.json()['access_token'])
    assert tokens[0]!=tokens[1]
    barrier=threading.Barrier(2)
    def do(i):
        barrier.wait()
        return client.post('/api/v1/sales',headers={'Authorization':'Bearer '+tokens[i]},json={'idempotency_key':f'cashier-race-{i}','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(do,range(2)))
    assert sorted(results)==[201,409]
    assert get(client,'/products',owner)['items'][0]['stock_qty']==0
    assert len(get(client,'/sales',owner))==1

def test_wallet_fiado_y_proveedores_y_compra_multiple(client):
    h=register(client);p=product(client,h,price=1000);q=product(client,h,price=2000)
    customer=post(client,'/customers',{'name':'Cliente'},h)
    sale(client,h,p['id'],'fiado-wallet-sale',1,0,0,1000,customer['id'])
    out=post(client,f"/customers/{customer['id']}/payments",{'idempotency_key':'wallet-collection','wallet_amount':400},h)
    assert out['balance_after']==600
    supplier=post(client,'/suppliers',{'name':'Proveedor'},h)
    purchase=post(client,'/suppliers/purchases',{'idempotency_key':'multi-buy-wallet', 'supplier_id':supplier['id'],'items':[{'product_id':p['id'],'quantity':2,'unit_cost':100},{'product_id':q['id'],'quantity':1,'unit_cost':300}], 'paid_wallet_amount':200,'pending_debt_amount':300},h)
    assert purchase['total_amount']==500 and purchase['paid_wallet_amount']==200
    out=post(client,f"/suppliers/{supplier['id']}/payments",{'idempotency_key':'supplier-wallet-pay','wallet_amount':100},h)
    assert out['debt_after']==200
    cash=get(client,'/cash',h);assert cash['global_balances']['wallet_balance']==100 and cash['global_balances']['transfer_balance']==0
    assert get(client,'/reports/verify-consistency',h)['is_mathematically_consistent']

def test_restore_tampered_fails_atomically(client):
    import json
    a=register(client);p=product(client,a)
    backup=client.get('/api/v1/reports/backup.json',headers=a).json()
    backup['tables']['products'][0]['stock_qty']=999
    b=register(client,open_cash=False)
    r=client.post('/api/v1/reports/restore.json',headers=b,files={'file':('backup.json',json.dumps(backup),'application/json')})
    assert r.status_code==400
    assert get(client,'/products',b)['items']==[]

def test_two_cashiers_both_sell_when_stock_allows(client):
    owner=register(client);p=product(client,owner,stock=2)
    tenant=get(client,'/auth/me',owner)['tenant']['id']
    login=client.post('/api/v1/auth/login',json={'email':'propietaria@mitiendita.ar','password':'MiTienditaAdmin2026!'}).json()
    admin={'Authorization':'Bearer '+login['access_token']}
    assert client.patch('/api/v1/admin/tenants/'+tenant,headers=admin,json={'plan_id':'plan_pro'}).status_code==200
    tokens=[]
    for i in range(2):
        email=f'caja-{uuid.uuid4().hex}@ejemplo.test'
        post(client,'/auth/staff',{'full_name':f'Cajero {i}','email':email,'password':'clave-segura-123','role':'cashier'},owner)
        tokens.append(client.post('/api/v1/auth/login',json={'email':email,'password':'clave-segura-123'}).json()['access_token'])
    barrier=threading.Barrier(2)
    def run(i):
        barrier.wait()
        return client.post('/api/v1/sales',headers={'Authorization':'Bearer '+tokens[i]},json={'idempotency_key':f'cajeros-dos-{i}','items':[{'product_id':p['id'],'quantity':1}],'paid_cash_amount':800}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
    assert sorted(results)==[201,201]
    assert len(get(client,'/sales',owner))==2 and get(client,'/products',owner)['items'][0]['stock_qty']==0

def test_registration_rate_limit_survives_restart(tmp_path):
    path=tmp_path/'rate.db';url=f'sqlite:///{path}'; email=uuid.uuid4().hex+'@ejemplo.test'
    body={'store_name':'Tienda','owner_name':'Dueño','email':email,'password':'password-seguro-123'}
    set_db(DatabaseManager(url))
    with TestClient(app,client=('restart-ip',1)) as c:
        assert c.post('/api/v1/auth/register',json=body).status_code==200
        for _ in range(4):assert c.post('/api/v1/auth/register',json=body).status_code==409
    set_db(DatabaseManager(url))
    with TestClient(app,client=('restart-ip',1)) as c:
        assert c.post('/api/v1/auth/register',json=body).status_code==429

def test_mora_ultima_fecha_de_pago():
    from datetime import timedelta
    from decimal import Decimal
    from app.config import utc_now
    from app.services.customers import _enrich_customer
    now=utc_now();row={'id':'1','name':'Ana','current_balance':100,'credit_limit':0,'oldest_unpaid_at':(now-timedelta(days=30)).isoformat(),'is_active':True}
    assert _enrich_customer(row,10,Decimal('0'),'Tienda',last_payment_at=(now-timedelta(days=2)).isoformat())['needs_reminder']==False
    assert _enrich_customer(row,10,Decimal('0'),'Tienda',last_payment_at=(now-timedelta(days=12)).isoformat())['needs_reminder']==True

def test_fiado_y_pagos_proveedor_rechazados_sin_turno(client):
    h=register(client,open_cash=False);p=product(client,h,price=1000)
    cu=post(client,'/customers',{'name':'Cliente'},h)
    sale(client,h,p['id'],'fiado-sin-turno',1,0,0,1000,cu['id'])
    r=client.post(f"/api/v1/customers/{cu['id']}/payments",headers=h,json={'idempotency_key':'cobro-sin-turno','cash_amount':200})
    assert r.status_code==409 and get(client,'/customers/'+cu['id'],h)['customer']['current_balance']==1000
    sup=post(client,'/suppliers',{'name':'Proveedor'},h)
    r=client.post('/api/v1/suppliers/purchases',headers=h,json={'idempotency_key':'compra-sin-turno','supplier_id':sup['id'],'items':[{'product_id':p['id'],'quantity':1,'unit_cost':100}],'paid_wallet_amount':100})
    assert r.status_code==409 and get(client,'/products',h)['items'][0]['stock_qty']==9

def test_wallet_void_and_currency_lock(client):
    h=register(client);p=product(client,h,price=1000)
    s=post(client,'/sales',{'idempotency_key':'wallet-void-test','items':[{'product_id':p['id'],'quantity':1}],'paid_wallet_amount':1000},h)
    assert post(client,'/sales/'+s['id']+'/void',{'reason':'error de prueba'},h)['status']=='voided'
    assert get(client,'/cash',h)['global_balances']['wallet_balance']==0
    assert get(client,'/reports/verify-consistency',h)['is_mathematically_consistent']
    assert client.patch('/api/v1/auth/tenant-settings',headers=h,json={'currency':'USD'}).status_code==409

def test_currency_at_registration_and_invalid_timezone(client):
    email=uuid.uuid4().hex+'@ejemplo.test'
    body={'store_name':'Prueba','owner_name':'Ana','email':email,'password':'clave-segura-123','currency':'USD','timezone':'America/New_York'}
    assert client.post('/api/v1/auth/register',json={**body,'timezone':'bad-zone'}).status_code==422
    assert client.post('/api/v1/auth/register',json=body).status_code==200
    assert client.post('/api/v1/auth/verify-email',json={'token':SENT[(email,'verify_email')]}).status_code==200
    r=client.post('/api/v1/auth/login',json={'email':email,'password':body['password']});h={'Authorization':'Bearer '+r.json()['access_token']}
    tenant=get(client,'/auth/me',h)['tenant'];assert tenant['currency']=='USD' and tenant['timezone']=='America/New_York' and not tenant['fiscal_module_enabled']

def test_mail_outbox_development_only(tmp_path, monkeypatch):
    import asyncio
    from app.mail import send_action_email
    monkeypatch.setenv('MAIL_MODE','outbox');monkeypatch.setenv('APP_ENV','development')
    monkeypatch.setenv('MAIL_OUTBOX_PATH',str(tmp_path/'outbox.log'))
    monkeypatch.setenv('PUBLIC_BASE_URL','http://127.0.0.1:8000')
    asyncio.run(send_action_email('prueba@ejemplo.test','verify_email','test-token-only'))
    assert '#action=verify_email&token=test-token-only' in (tmp_path/'outbox.log').read_text()
    monkeypatch.setenv('APP_ENV','production')
    with pytest.raises(RuntimeError):asyncio.run(send_action_email('prueba@ejemplo.test','verify_email','test-token-only'))

def test_verification_token_expires(client, monkeypatch):
    import app.auth_actions as actions
    t=[1700000000]
    monkeypatch.setattr(actions.time,'time',lambda:t[0])
    email=uuid.uuid4().hex+'@ejemplo.test'
    r=client.post('/api/v1/auth/register',json={'store_name':'Tienda','owner_name':'Ana','email':email,'password':'clave-segura-123'})
    assert r.status_code==200
    token=SENT[(email,'verify_email')];t[0]+=1801
    assert client.post('/api/v1/auth/verify-email',json={'token':token}).status_code==400

def test_restore_currency_mismatch_rejected(client):
    a=register(client);product(client,a)
    backup=client.get('/api/v1/reports/backup.json',headers=a).content
    b=register(client,open_cash=False)
    assert client.patch('/api/v1/auth/tenant-settings',headers=b,json={'currency':'USD'}).status_code==200
    r=client.post('/api/v1/reports/restore.json',headers=b,files={'file':('backup.json',backup,'application/json')})
    assert r.status_code==400 and get(client,'/products',b)['items']==[]

def test_corte_manual_exclusivo_historial_y_pago_no_reactiva(client):
    h=register(client); p=product(client,h)
    me=get(client,'/auth/me',h); tid=me['user']['tenant_id']; plan=me['user']['plan_id']
    from app.config import get_settings
    cfg=get_settings()
    r=client.post('/api/v1/auth/login',json={'email':cfg.superadmin_email,'password':cfg.superadmin_password});assert r.status_code==200,r.text
    admin={'Authorization':'Bearer '+r.json()['access_token']}
    base='/api/v1/admin/tenants/'+tid
    assert client.patch(base,headers=admin,json={'subscription_expires_at':'2001-01-01'}).status_code==200
    assert client.get('/api/v1/products',headers=h).status_code==200 # expiry alone never cuts
    assert client.patch(base,headers=admin,json={'subscription_status':'suspended'}).status_code==400
    assert client.post(base+'/service/suspend',headers=h,json={'confirmation':'CONFIRMAR','reason':'prueba'}).status_code==403
    # Even an additional superadmin account is not the configured SaaS owner.
    from app.security import AuthContext, require_superadmin
    app.dependency_overrides[require_superadmin] = lambda: AuthContext('other','another@example.test','Otro','superadmin',None)
    try:
        assert client.post(base+'/service/suspend',headers=admin,json={'confirmation':'CONFIRMAR','reason':'prueba'}).status_code==403
    finally:
        app.dependency_overrides.pop(require_superadmin,None)
    assert client.post(base+'/service/suspend',headers=admin,json={'confirmation':'NO','reason':'prueba'}).status_code==422
    assert client.post(base+'/service/suspend',headers=admin,json={'confirmation':'CONFIRMAR','reason':'prueba manual'}).status_code==200
    assert client.get('/api/v1/products',headers=h).status_code==403
    assert client.get('/api/v1/reports/backup.json',headers=h).status_code==200
    assert client.patch(base,headers=admin,json={'subscription_status':'active'}).status_code==400
    pay={'plan_id':plan,'amount':1,'period_start':'2026-01-01','period_end':'2026-02-01'}
    assert client.post(base+'/payments',headers=admin,json=pay).status_code==201
    assert client.get('/api/v1/products',headers=h).status_code==403
    hist=client.get(base+'/service-history',headers=admin);assert hist.status_code==200,hist.text
    assert hist.json()[0]['actor_email']==cfg.superadmin_email and hist.json()[0]['details']['reason']=='prueba manual'
    assert client.get(base+'/service-history',headers=h).status_code==403
    assert client.post(base+'/service/reactivate',headers=admin,json={'confirmation':'CONFIRMAR','reason':'prueba reactivación'}).status_code==200
    assert client.get('/api/v1/products',headers=h).status_code==200
    assert len(client.get(base+'/service-history',headers=admin).json())==2


def test_ocr_imagen_real_revision_sin_cambios(client):
    import io, shutil
    from PIL import Image, ImageDraw, ImageFont
    if not all(shutil.which(x) for x in ('tesseract','pdftotext','pdfinfo','pdftoppm')): pytest.skip('Dependencias OCR nativas no instaladas')
    h=register(client);p=product(client,h,stock=10)
    im=Image.new('RGB',(1600,400),'white'); draw=ImageDraw.Draw(im)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',42)
    for y,name,q,price,total in [(30,'AZUCAR', '2','500.00','1000.00'),(110,'LECHE','3','200.00','600.00')]:
        for x,t in [(40,name),(600,q),(850,price),(1200,total)]: draw.text((x,y),t,fill='black',font=font)
    buf=io.BytesIO();im.save(buf,format='PNG')
    r=client.post('/api/v1/suppliers/document-extraction',headers=h,files={'file':('remito.png',buf.getvalue(),'image/png')})
    assert r.status_code==200,r.text
    data=r.json();assert 'AZUCAR' in data['text'] and 'LECHE' in data['text'],data
    assert len(data['items'])>=2,data
    assert get(client,'/products',h)['items'][0]['stock_qty']==10
    assert client.post('/api/v1/suppliers/document-extraction',headers=h,files={'file':('x.txt',b'bad','text/plain')}).status_code==415
    assert client.post('/api/v1/suppliers/document-extraction',headers=h,files={'file':('x.png',b'x'*(8*1024*1024+1),'image/png')}).status_code==413
    buf=io.BytesIO();im.save(buf,format='PDF',resolution=150)
    r=client.post('/api/v1/suppliers/document-extraction',headers=h,files={'file':('scan.pdf',buf.getvalue(),'application/pdf')})
    assert r.status_code==200,r.text
    assert r.json()['method']=='OCR PDF escaneado' and 'AZUCAR' in r.json()['text']
    assert get(client,'/products',h)['items'][0]['stock_qty']==10


def test_pdf_texto_y_compra_revisada(client):
    import shutil
    if not all(shutil.which(x) for x in ('tesseract','pdftotext','pdfinfo','pdftoppm')): pytest.skip('Dependencias OCR no instaladas')
    # PDF sintético real con texto posicionado, sin imagen y sin librerías extra.
    stream=b'BT /F1 18 Tf 40 750 Td (AZUCAR) Tj 220 0 Td (2) Tj 90 0 Td (500.00) Tj 90 0 Td (1000.00) Tj ET\nBT /F1 18 Tf 40 710 Td (LECHE) Tj 220 0 Td (3) Tj 90 0 Td (200.00) Tj 90 0 Td (600.00) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    pdf=b'%PDF-1.4\n';offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(pdf));pdf+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(pdf);pdf+=f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for off in offsets[1:]:pdf+=f'{off:010d} 00000 n \n'.encode()
    pdf+=f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    h=register(client);p1=product(client,h,stock=0);p2=post(client,'/products',{'name':'Otro','stock_qty':0,'cost_price':10,'sale_price':20},h)
    r=client.post('/api/v1/suppliers/document-extraction',headers=h,files={'file':('texto.pdf',pdf,'application/pdf')})
    assert r.status_code==200,r.text
    d=r.json();assert d['method']=='texto PDF' and len(d['items'])==2,d
    assert [r['quantity'] for r in d['items']]==[2,3] and [r['unit_price'] for r in d['items']]==[500,200]
    assert [x['stock_qty'] for x in get(client,'/products',h)['items']]==[0,0]
    sup=post(client,'/suppliers',{'name':'Proveedor PDF'},h)
    # Equivalent to a human linking the two reviewed suggestions and explicitly confirming.
    items=[{'product_id':p['id'],'quantity':row['quantity'],'unit_cost':row['unit_price']} for p,row in zip((p1,p2),d['items'])]
    purchase=post(client,'/suppliers/purchases',{'idempotency_key':'pdf-compra-12345','supplier_id':sup['id'],'items':items,'paid_cash_amount':0,'pending_debt_amount':1600},h)
    assert purchase['total_amount']==1600
    stocks={x['id']:x['stock_qty'] for x in get(client,'/products',h)['items']}
    assert stocks[p1['id']]==2 and stocks[p2['id']]==3


def test_smtp_construye_enlace_en_linea_separada(monkeypatch):
    import asyncio
    import app.mail as mail
    captured={}
    class FakeSMTP:
        def __init__(self,host,port,timeout): captured.update(host=host,port=port)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def ehlo(self): pass
        def starttls(self,context): captured['tls']=True
        def login(self,user,password):captured.update(user=user,password=password)
        def send_message(self,message):captured['message']=message
    monkeypatch.setattr(mail.smtplib,'SMTP',FakeSMTP)
    for k,v in {'MAIL_MODE':'smtp','PUBLIC_BASE_URL':'https://ejemplo.test','SMTP_HOST':'smtp.gmail.com','SMTP_PORT':'587','SMTP_USER':'cuenta@example.test','SMTP_FROM':'cuenta@example.test','SMTP_PASSWORD':'password-app-test'}.items(): monkeypatch.setenv(k,v)
    asyncio.run(mail.send_action_email('destino@example.test','verify_email','token-prueba'))
    body=captured['message'].get_content()
    assert captured['tls'] and captured['port']==587 and captured['user']=='cuenta@example.test'
    assert '\nhttps://ejemplo.test/#action=verify_email&token=token-prueba\n' in body


def test_ocr_cantidad_primero():
    from app.services.ocr import parse
    result=parse('2  LECHE ENTERA       200,00      400,00')
    assert result[0]['description']=='LECHE ENTERA' and result[0]['quantity']==2 and result[0]['unit_price']==200


# --- Pruebas agregadas por la auditoría consolidada (ver docs/AUDITORIA_Y_PENDIENTES.md) ---

def test_panel_admin_completo_exclusivo_de_la_propietaria(client):
    h = register(client)
    me = get(client, '/auth/me', h); tid = me['user']['tenant_id']; plan = me['user']['plan_id']
    from app.security import AuthContext, require_superadmin
    base = '/api/v1/admin/tenants/' + tid
    app.dependency_overrides[require_superadmin] = lambda: AuthContext('otro', 'otro@ejemplo.test', 'Otro', 'superadmin', None)
    try:
        pay = {'plan_id': plan, 'amount': 1, 'period_start': '2026-01-01', 'period_end': '2026-02-01'}
        assert client.get('/api/v1/admin/overview', headers=h).status_code == 403
        assert client.patch(base, headers=h, json={'subscription_expires_at': '2001-01-01'}).status_code == 403
        assert client.post(base + '/payments', headers=h, json=pay).status_code == 403
        assert client.get(base + '/service-history', headers=h).status_code == 403
    finally:
        app.dependency_overrides.pop(require_superadmin, None)


def test_no_se_desactiva_cliente_con_deuda(client):
    h = register(client); p = product(client, h, price=3000)
    cu = post(client, '/customers', {'name': 'Cliente deudor'}, h)
    sale(client, h, p['id'], 'DEUDA-sale-1', 1, 0, 0, 3000, cu['id'])
    r = client.patch('/api/v1/customers/' + cu['id'], json={'is_active': False}, headers=h)
    assert r.status_code == 409, r.text
    post(client, '/customers/' + cu['id'] + '/payments', {'idempotency_key': 'DEUDA-pago-1', 'cash_amount': 3000, 'transfer_amount': 0}, h)
    r = client.patch('/api/v1/customers/' + cu['id'], json={'is_active': False}, headers=h)
    assert r.status_code == 200, r.text


def test_login_con_correo_sin_confirmar_da_mensaje_claro(client):
    email = 'sinconfirmar-' + uuid.uuid4().hex[:10] + '@ejemplo.test'
    r = client.post('/api/v1/auth/register', json={'store_name': 'Negocio X', 'owner_name': 'Ana', 'email': email, 'password': 'password-seguro-123'})
    assert r.status_code == 200, r.text
    r = client.post('/api/v1/auth/login', json={'email': email, 'password': 'password-seguro-123'})
    assert r.status_code == 403 and 'confirmaste' in r.json()['detail']


def test_pago_de_suscripcion_con_plan_inexistente_rechazado(client):
    h = register(client)
    tid = get(client, '/auth/me', h)['user']['tenant_id']
    from app.config import get_settings
    cfg = get_settings()
    r = client.post('/api/v1/auth/login', json={'email': cfg.superadmin_email, 'password': cfg.superadmin_password}); assert r.status_code == 200, r.text
    admin = {'Authorization': 'Bearer ' + r.json()['access_token']}
    pay = {'plan_id': 'no-existe', 'amount': 1, 'period_start': '2026-01-01', 'period_end': '2026-02-01'}
    assert client.post('/api/v1/admin/tenants/' + tid + '/payments', headers=admin, json=pay).status_code == 400


def test_ip_real_detras_de_proxy(monkeypatch):
    from types import SimpleNamespace
    from app.security import client_ip
    req = SimpleNamespace(headers={'x-forwarded-for': '9.9.9.9, 203.0.113.7'}, client=SimpleNamespace(host='10.0.0.1'))
    monkeypatch.delenv('TRUSTED_PROXY_HOPS', raising=False)
    assert client_ip(req) == '10.0.0.1'          # sin proxy de confianza se ignora el encabezado
    monkeypatch.setenv('TRUSTED_PROXY_HOPS', '1')
    assert client_ip(req) == '203.0.113.7'       # lo agrega el proxy; lo de la izquierda lo inventa el cliente
