"""Prueba manual destructiva SOLO con dos bases NUEVAS de prueba backup_test_src y backup_test_dst.
Crear previamente ambas vacías. Ejecutar desde raíz: python -m tests.verify_postgres_backup
O python tests/verify_postgres_backup.py con PYTHONPATH=.
"""
import os, subprocess, tempfile
from urllib.parse import urlparse
from pathlib import Path
from fastapi.testclient import TestClient
from app.db import DatabaseManager, set_db
from app.main import app
import app.auth_actions as actions

src=os.environ['TEST_BACKUP_SOURCE_URL']; dst=os.environ['TEST_BACKUP_RESTORE_URL']; dump_url=os.environ['TEST_BACKUP_DUMP_URL']
assert urlparse(dump_url).path.lstrip('/')=='backup_test_src'
assert src!=dst and urlparse(src).path.lstrip('/')=='backup_test_src' and urlparse(dst).path.lstrip('/')=='backup_test_dst', 'Usar exclusivamente bases de prueba designadas'
def sql(url,q):
    return subprocess.check_output(['psql',url,'-v','ON_ERROR_STOP=1','-At','-c',q],text=True).strip()
for u in (src,dst):
    assert sql(u,"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")=='0', 'Las dos bases deben estar vacías'
sent={}
async def mail(address,purpose,token):sent[(address,purpose)]=token
actions.send_action_email=mail
pw='ClaveDePrueba_123'; emails=['backupuno@example.test','backupdos@example.test']
set_db(DatabaseManager(src))
with TestClient(app) as c:
    for email in emails:
        r=c.post('/api/v1/auth/register',json={'store_name':'Prueba Backup '+email,'owner_name':'Titular Prueba','email':email,'password':pw});assert r.status_code==200,r.text
        assert c.post('/api/v1/auth/verify-email',json={'token':sent[(email,'verify_email')]}).status_code==200
        r=c.post('/api/v1/auth/login',json={'email':email,'password':pw});assert r.status_code==200,r.text
        h={'Authorization':'Bearer '+r.json()['access_token']}
        r=c.post('/api/v1/products',headers=h,json={'name':'Producto '+email,'stock_qty':7,'cost_price':3,'sale_price':5});assert r.status_code==201,r.text
hashes=sql(src,"SELECT email || ':' || password_hash FROM users ORDER BY email")
sql(src, 'GRANT USAGE ON SCHEMA public TO tiendita_backup_test; GRANT SELECT ON ALL TABLES IN SCHEMA public TO tiendita_backup_test; GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO tiendita_backup_test')
env=dict(os.environ,DATABASE_URL=dump_url,RESTORE_DATABASE_URL=dst)
with tempfile.TemporaryDirectory() as folder:
    file=str(Path(folder)/'backup.dump')
    subprocess.run(['scripts/backup_postgres.sh',file],env=env,check=True)
    subprocess.run(['scripts/restore_postgres.sh',file,'RESTAURAR_EN_BASE_VACIA'],env=env,check=True)
    # A second restore must refuse to overwrite the now-populated destination.
    assert subprocess.run(['scripts/restore_postgres.sh',file,'RESTAURAR_EN_BASE_VACIA'],env=env).returncode!=0
assert hashes==sql(dst,"SELECT email || ':' || password_hash FROM users ORDER BY email")
assert sql(src,'SELECT count(*) FROM tenants')==sql(dst,'SELECT count(*) FROM tenants')
set_db(DatabaseManager(dst))
with TestClient(app) as c:
    for email in emails:
        assert c.post('/api/v1/auth/login',json={'email':email,'password':'contraseña incorrecta'}).status_code==401
        r=c.post('/api/v1/auth/login',json={'email':email,'password':pw});assert r.status_code==200,r.text
        h={'Authorization':'Bearer '+r.json()['access_token']}
        r=c.get('/api/v1/products',headers=h);assert r.status_code==200,r.text
        assert len(r.json()['items'])==1 and r.json()['items'][0]['name']=='Producto '+email and r.json()['items'][0]['stock_qty']==7
print('OK: backup, restore, hashes iguales, dos usuarios y contraseñas, aislamiento, productos y rechazo de base no vacía')
