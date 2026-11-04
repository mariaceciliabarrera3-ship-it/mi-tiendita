import logging
import hashlib
import secrets
import time
from fastapi import HTTPException
from app.db import get_db
from app.mail import send_action_email
from app.security import hash_password

async def limit(scope: str, identifier: str, maximum: int, seconds: int) -> None:
    now=int(time.time()); k=hashlib.sha256((scope+':'+identifier).encode()).hexdigest()
    db=get_db()
    async with db.transaction() as c:
        row=await c.fetchrow('''INSERT INTO auth_rate_limits(rate_key,window_start,attempts) VALUES($1,$2,1)
          ON CONFLICT(rate_key) DO UPDATE SET
          attempts=CASE WHEN auth_rate_limits.window_start <= $3 THEN 1 ELSE auth_rate_limits.attempts+1 END,
          window_start=CASE WHEN auth_rate_limits.window_start <= $3 THEN $2 ELSE auth_rate_limits.window_start END
          RETURNING attempts''',k,now,now-seconds)
    if int(row['attempts'])>maximum:
        raise HTTPException(429,'Demasiados intentos. Volvé a intentar más tarde.')

async def issue(user_id: str, email: str, purpose: str) -> None:
    raw=secrets.token_urlsafe(32); digest=hashlib.sha256(raw.encode()).hexdigest(); now=int(time.time())
    async with get_db().transaction() as c:
        # Invalidate prior links for the same purpose.
        await c.execute('UPDATE auth_action_tokens SET used_at=$1 WHERE user_id=$2 AND purpose=$3 AND used_at IS NULL',now,user_id,purpose)
        await c.execute('INSERT INTO auth_action_tokens(token_hash,user_id,purpose,expires_at,created_at) VALUES($1,$2,$3,$4,$5)',digest,user_id,purpose,now+1800,now)
    try:
        await send_action_email(email,purpose,raw)
    except Exception as exc:
        # Error type only: never log addresses, tokens, server responses or SMTP credentials.
        logging.getLogger(__name__).error('Fallo de envío de correo (%s)', type(exc).__name__)
        # Link cannot be delivered; invalidate it.
        async with get_db().transaction() as c:
            await c.execute('UPDATE auth_action_tokens SET used_at=$1 WHERE token_hash=$2',now,digest)
        raise HTTPException(503,'No se pudo enviar el correo. Configurá el servicio de correo y volvé a solicitarlo.')

async def consume(token: str, purpose: str, new_password: str | None = None) -> None:
    if len(token)>512: raise HTTPException(400,'Enlace inválido o vencido.')
    digest=hashlib.sha256(token.encode()).hexdigest(); now=int(time.time())
    async with get_db().transaction() as c:
        row=await c.fetchrow('SELECT * FROM auth_action_tokens WHERE token_hash=$1 FOR UPDATE',digest)
        if not row or row['purpose']!=purpose or row['used_at'] is not None or row['expires_at']<=now:
            raise HTTPException(400,'Enlace inválido, usado o vencido.')
        if purpose=='verify_email':
            user=await c.fetchrow('SELECT is_active,email_verified FROM users WHERE id=$1 FOR UPDATE',row['user_id'])
            if not user or bool(user['email_verified']):
                raise HTTPException(400,'Enlace inválido, usado o vencido.')
            await c.execute('UPDATE users SET is_active=$1, email_verified=$1 WHERE id=$2',True,row['user_id'])
        else:
            await c.execute('UPDATE users SET password_hash=$1, token_version=token_version+1 WHERE id=$2',hash_password(new_password),row['user_id'])
        await c.execute('UPDATE auth_action_tokens SET used_at=$1 WHERE user_id=$2 AND purpose=$3 AND used_at IS NULL',now,row['user_id'],purpose)
