"""SMTP delivery; in development only, writes tokens to an explicit local outbox."""
import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

async def send_action_email(address: str, purpose: str, token: str) -> None:
    base = os.getenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
    link = f'{base}/#action={purpose}&token={token}'
    mode = os.getenv('MAIL_MODE', 'disabled').lower()
    if mode == 'outbox' and os.getenv('APP_ENV', 'development') != 'production':
        path = Path(os.getenv('MAIL_OUTBOX_PATH', 'data/mail-outbox.log'))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf8') as f:
            f.write(f'{address}\t{purpose}\t{link}\n')
        return
    if mode != 'smtp':
        raise RuntimeError('El correo no está configurado. Configurá SMTP o el buzón local de desarrollo.')
    host=os.environ.get('SMTP_HOST',''); sender=os.environ.get('SMTP_FROM','')
    if not host or not sender or not base.startswith('https://'):
        raise RuntimeError('SMTP requiere servidor, remitente y PUBLIC_BASE_URL HTTPS.')
    msg=EmailMessage(); msg['From']=sender; msg['To']=address
    msg['Subject']='Mi Tiendita: confirmar correo' if purpose=='verify_email' else 'Mi Tiendita: recuperar contraseña'
    msg.set_content(f'Abrí este enlace para continuar (vence en 30 minutos):\n{link}\n\nSi no solicitaste esta acción, ignorá este mensaje.')
    def deliver():
        with smtplib.SMTP(host, int(os.getenv('SMTP_PORT','587')), timeout=10) as smtp:
            smtp.ehlo();smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
            user=os.getenv('SMTP_USER',''); password=os.getenv('SMTP_PASSWORD','')
            if user: smtp.login(user,password)
            smtp.send_message(msg)
    await asyncio.to_thread(deliver)
