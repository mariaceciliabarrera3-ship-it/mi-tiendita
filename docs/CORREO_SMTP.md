# Activar correos de verificación y recuperación con Gmail

En la aplicación nueva no se entregó ninguna cuenta de correo configurada. Los enlaces vencen a los 30 minutos y se usan una sola vez. Esta guía usa Gmail como ejemplo, no implica envío ilimitado ni garantiza entregabilidad. Usá una casilla dedicada al servicio y controlá las cuotas y condiciones vigentes de Google. Google recomienda OAuth cuando existe; esta app usa SMTP con contraseña de aplicación, no implementa OAuth.

## Antes de empezar
1. Necesitás una cuenta Gmail dedicada y una dirección HTTPS pública y operativa para Mi Tiendita, por ejemplo `https://tutienda.ejemplo.com` (no uses `localhost`, ni una barra o ruta adicional).
2. Entrá a tu [cuenta de Google](https://myaccount.google.com/security), sección **Seguridad**, y activá la **Verificación en dos pasos** siguiendo las instrucciones de Google.
3. Abrí [Contraseñas de aplicaciones](https://myaccount.google.com/apppasswords), iniciá sesión de nuevo si lo pide, escribí `Mi Tiendita` y creá una contraseña. Google muestra un código de 16 caracteres: guardalo en un gestor de contraseñas. No lo pegues en GitHub ni lo compartas en capturas. La contraseña normal de Gmail **no** va en SMTP_PASSWORD.
4. Si no aparece la opción, Google informa que puede deberse a cuenta de empresa/escuela, protección avanzada o verificación solo con llaves de seguridad. Consultá a quien administre la cuenta o usá otro proveedor SMTP compatible con STARTTLS; no desactives la verificación en dos pasos.

## Configurar el servicio
En Render abrí tu servicio → **Environment** → agregá cada variable y guardá/redeployá. Reemplazá los ejemplos por tus datos reales:

| Nombre | Valor |
|---|---|
| `MAIL_MODE` | `smtp` |
| `PUBLIC_BASE_URL` | `https://tutienda.ejemplo.com` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `tucuenta@gmail.com` |
| `SMTP_FROM` | `tucuenta@gmail.com` |
| `SMTP_PASSWORD` | los 16 caracteres de la contraseña de aplicación, sin espacios |

No escribir comillas como parte del valor en el formulario web. Para ejecutar localmente, exportá esas mismas variables en el proceso (el proyecto no carga `.env` automáticamente) y usá una URL HTTPS accesible si verificás desde otro dispositivo. El puerto 587 usa STARTTLS. `MAIL_MODE=outbox` es solo desarrollo: guarda enlaces secretos en un archivo local y no manda correos.

## Probar de verdad
1. Registrá un comercio de prueba con una dirección de correo que controles. Debe llegar un mensaje con enlace; comprobá spam. Hacé clic y luego iniciá sesión.
2. Cerrá sesión, elegí **Recuperar contraseña**, abrí el enlace del correo y cambiá la clave. Verificá que la anterior falle y la nueva funcione.
3. No compartas los enlaces ni capturas con tokens. Si no llega el mensaje: confirmá variables, 2 pasos, contraseña de aplicación, HTTPS, puerto, spam, bloqueos de salida y logs del servidor sin publicar secretos. La recuperación devuelve un mensaje genérico aun si hubo un error de envío para no revelar cuentas; inspeccioná logs y probá con tu correo.
4. Google revoca las contraseñas de aplicación al cambiar la contraseña de Google; creá otra y actualizá el secreto. Si se filtra, revocala inmediatamente y reemplazala. No se probó un envío Gmail real en esta entrega: necesita credenciales del usuario y prueba de extremo a extremo en su hosting.

Fuentes: [contraseñas de aplicación de Google](https://support.google.com/accounts/answer/185833?hl=es), [datos SMTP Gmail](https://support.google.com/mail/answer/7104828?hl=es). Su disponibilidad y límites pueden cambiar.
