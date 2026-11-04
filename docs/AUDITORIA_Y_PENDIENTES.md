# Auditoría consolidada y pendientes

Este documento **reemplaza** a `ESTADO_Y_RIESGOS.md`, `CAMBIOS_Y_PRUEBAS.md` y `TERCERA_ITERACION.md` (historial de las tres tandas de desarrollo). Lo que de ellos seguía vigente está incorporado acá. El proyecto es un solo cuerpo de código: no hay versiones paralelas.

## 0. Cómo se verificó esta revisión (leer primero)

- **Lectura completa** del backend (`app/`), migraciones (SQLite y PostgreSQL), scripts, Docker/Render, CI, docs y revisión del frontend en lo que toca a seguridad (escape de HTML, caché).
- **Lo que NO pude hacer:** ejecutar la suite de pruebas. El entorno de la auditoría no tenía red para instalar FastAPI/pydantic/asyncpg. Por eso **ningún cambio de esta revisión pasó por `pytest`**.
- Lo que sí verifiqué: compilación de todos los `.py`, un chequeo estático propio (nombres indefinidos e imports sin uso), importación de todos los módulos con dependencias simuladas, el helper de caja ejercitado con una conexión simulada, las 5 migraciones SQLite aplicadas en orden con SQLite real, y el OCR/Poppler/Tesseract reales contra PDFs de prueba (incluido el PDF de página gigante).
- **Primer paso obligatorio:** correr `pip install -r requirements-dev.txt && pytest -v` (el CI de GitHub ya lo hace) y, idealmente, repetir con `TEST_DATABASE_URL` apuntando a un PostgreSQL de prueba. Se agregaron 5 pruebas nuevas al final de `tests/test_core.py`; tampoco fueron ejecutadas. Si algo falla, el cambio sospechoso está en la sección 1.

## 1. Qué arreglé

### Seguridad
| # | Cambio | Archivos |
|---|---|---|
| S1 | **Todo** el panel `/admin` exige ahora la cuenta propietaria (rol `superadmin` **y** correo == `SUPERADMIN_EMAIL`). Antes solo cortar/reactivar lo exigía; cambiar plan, registrar pagos y leer historial aceptaban cualquier fila con rol superadmin. | `routers/saas_admin.py` |
| S2 | En producción la app **no arranca** si no se define `SUPERADMIN_EMAIL` explícito. Esa casilla recupera la contraseña de la cuenta que corta comercios; no puede ser un valor por defecto de un dominio ajeno. | `config.py`, `render.yaml` |
| S3 | IP real del cliente detrás del proxy de Render (`TRUSTED_PROXY_HOPS=1`, se toma la entrada de la derecha de `X-Forwarded-For`). Antes todos los usuarios compartían la IP del proxy (inferido del código: uvicorn solo confía en proxies locales): el límite "5 registros/hora" y "10 recuperaciones/hora" era **global**, y la auditoría guardaba la IP del proxy. | `security.py` (`client_ip`), `routers/auth.py` |
| S4 | Login: se iguala el tiempo de respuesta cuando el correo no existe (no se pueden enumerar cuentas midiendo demora); mensaje claro si el correo no fue confirmado (antes decía "usuario desactivado"). | `security.py`, `routers/auth.py` |
| S5 | Encabezados: `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, HSTS en producción, `Cache-Control: no-store` en `/api/`. CSRF: también se rechaza `Sec-Fetch-Site: cross-site` cuando no viene `Origin`. | `main.py` |
| S6 | **No se puede desactivar un cliente con deuda**: desaparecía de listados y del total de fiados, ocultando plata a cobrar. | `services/customers.py` |
| S7 | Pago de suscripción con plan inexistente: 400 en vez de error 500 por clave foránea. | `services/saas_admin.py` |
| S8 | PDF liviano con página gigante ya no puede agotar la memoria al rasterizar (tope de 20 megapíxeles a 150 dpi, probado con Poppler real). OCR: una extracción simultánea por defecto (`OCR_MAX_CONCURRENT`); antes 2, riesgoso en 512 MB. | `services/ocr.py`, `routers/suppliers.py` |
| S9 | Importación CSV/XLSX acotada a 2.000 filas (cada fila es una transacción; un archivo de 5 MB podía tener cientos de miles). | `services/catalog.py` |
| S10 | Al arrancar en producción se registra una **advertencia** si el rol de la base es superusuario o `BYPASSRLS` (en ese caso RLS no protege nada) y si falta configuración de correo. No frena el servicio. | `main.py` |

### Despliegue / base de datos
| # | Cambio |
|---|---|
| D1 | Migraciones PostgreSQL con `pg_advisory_xact_lock`: en un deploy de Render la instancia nueva arranca con la vieja viva; dos procesos migrando a la vez podían pisarse. |
| D2 | Las migraciones corrían **dos veces** en cada arranque (`scripts/migrate.py` en el `CMD` y de nuevo en el `lifespan`). Ahora corren una sola vez, en `lifespan`. `scripts/migrate.py` queda como herramienta manual. |
| D3 | Dockerfile: base fijada a `python:3.12-slim-bookworm` (antes flotante), usuario sin privilegios, `OMP_THREAD_LIMIT=1` (Tesseract no satura la CPU), sin `.pyc`. |
| D4 | `render.yaml`: faltaban las variables de correo (sin ellas **nadie puede registrarse ni recuperar contraseña**); se agregaron `MAIL_MODE`, `PUBLIC_BASE_URL`, `SMTP_*`, `TRUSTED_PROXY_HOPS`, `OCR_MAX_CONCURRENT`. `SUPERADMIN_EMAIL` pasó a `sync: false` (la dueña debe cargarla). |
| D5 | `.dockerignore` excluye `tests/`, `docs/`, `.github/`; `requirements.txt` quedó solo con dependencias de ejecución (`pytest` y `httpx` pasaron a `requirements-dev.txt`; `pytest-asyncio` no se usaba). |

### Código muerto, duplicado e inconsistencias entre tandas
| # | Cambio |
|---|---|
| C1 | **Movimientos de caja:** el `INSERT INTO cash_movements` estaba copiado 15 veces en tres estilos distintos (ventas, anulaciones, cobros, compras, pagos, apertura, cierre, manual). Ahora hay un único punto de escritura (`insert_cash_movement` / `record_channel_movements` en `services/cash.py`). Se conservaron **idénticas las claves idempotentes** (`sale-cash-…`, `void-…`, `fiado-pay-…`, `purch-…`, `sup-pay-…`). Se unificaron categorías (antes "Cobro de Fiados" y "Cobro de fiados" convivían) y las descripciones pasaron a "<concepto> (<canal>)". |
| C2 | La neutralización de fórmulas Excel estaba copiada 3 veces: ahora `app/xlsx_safe.py`. |
| C3 | Dos mecanismos de IP (`request.client.host` con fallbacks distintos `""`, `"local"`, `"unknown"`) → uno solo (`client_ip`). |
| C4 | Eliminados: router de webhooks (WhatsApp/Mercado Pago; no hacía nada y era superficie pública), configuración Groq y `httpx` en el Ayudante (nunca se usaba), `to_iso`, `CORSMiddleware` importado sin usar, `ALLOWED_ORIGINS`/`APP_HOST`/`APP_PORT`/`LOG_LEVEL` en `.env.example` (nadie las leía), `if (True) and …` residual en clientes y proveedores, chequeo de suspensión duplicado e inalcanzable en `require_tenant_write`, parámetros e imports sin uso. |
| C5 | Cookie de sesión: duración tomada de `ACCESS_TOKEN_EXPIRE_MINUTES` (estaba fija en 720 minutos aparte del token). |
| C6 | Del ZIP entregado se retiraron: 3 ZIP anidados (versiones viejas), `.sudo_as_admin_successful`, y **`data/mi_tiendita.db`** (base SQLite de desarrollo con datos demo y la cuenta de administración con la clave por defecto). |

## 2. Lo que encontré y NO toqué (y por qué)

| Hallazgo | Por qué no lo toqué |
|---|---|
| **RLS cubre las tablas comerciales pero no `users`, `tenants`, `audit_logs`, `auth_*`, `subscription_*`**; las claves foráneas no son compuestas (`tenant_id` + id), así que la base no impide por sí sola una referencia cruzada entre comercios; solo la impide el código. | Requiere rediseño de políticas (el login consulta `users` sin comercio) y no puedo probarlo sin PostgreSQL. Las consultas de negocio sí filtran siempre por `tenant_id`; los tests de aislamiento pasaban. |
| **RLS puede estar inactiva en Neon.** Recuerdo (sin poder verificarlo acá) que los roles creados desde la consola de Neon tienen `BYPASSRLS`. Si es así, RLS no hace nada. | Dependencia de la cuenta real. Ya agregué el aviso de arranque (S10): mirá el log del primer deploy. Si aparece, crear un rol de aplicación sin `BYPASSRLS` ni superusuario. |
| **Dos sistemas de límite de intentos:** login usa un contador en memoria (se pierde al reiniciar, ignora ataques distribuidos); registro/recuperación usan contador persistente en base. | Unificarlos cambia la semántica (el de login cuenta solo fallos) y no pude correr los tests. Con una sola instancia y 40 comercios es tolerable. |
| **Dinero como `float` en la API JSON y SQLite con afinidad REAL.** PostgreSQL usa `NUMERIC` y el servidor calcula con `Decimal` (correcto); el redondeo a 2 decimales mitiga. | Cambiar a centavos enteros toca API y frontend. Con ARS y dos decimales no es bloqueante, pero **SQLite no debe usarse con datos reales** (la app ya lo impide en producción). |
| Ganancia por producto usa importes antes del descuento global; la ganancia global sí lo descuenta. | Es un criterio de reporte documentado, no un error de saldo. Decidir con la dueña. |
| Idempotencia: una clave repetida devuelve la operación previa **sin comparar el cuerpo**; el frontend pierde la clave si se recarga la página tras perder la respuesta (podría duplicar una venta). | Requiere cambio de frontend (guardar la operación pendiente). Va al trabajo del rediseño. |
| **Permisos finos:** un cajero puede cambiar precios y costos de productos, poner precio unitario libre y descuento en una venta. Solo el titular puede anular ventas y hacer correcciones de stock. | Decisión de producto, no un bug. Definir una matriz de permisos antes de vender. |
| Límites de plan parciales (solo al alta); el titular puede activar el módulo fiscal sin importar el plan; el alta de personal no toma el bloqueo del comercio (carrera mínima). | Requiere definir reglas comerciales. |
| Numeración de migraciones desalineada entre backends (PostgreSQL 001–006, SQLite 001–005, porque RLS solo existe en PostgreSQL). **No se unificaron en una sola** migración. | No puedo probar PostgreSQL acá, y reescribir el esquema inicial sin poder ejecutarlo en PostgreSQL es el tipo de riesgo que pediste evitar. Como nunca se desplegó, es una mejora segura **si se prueba**: ver sección 4. |
| Cuenta propietaria: sin MFA; recuperación de contraseña por correo (quien controle esa casilla controla el SaaS); si se cambia `SUPERADMIN_EMAIL`, la fila anterior conserva rol superadmin (queda sin poder de corte, pero revisarla). | Se mitiga con casilla propia y verificación en dos pasos en el correo. |
| Registro: responder 409 por correo existente permite saber si un correo está registrado; un correo pendiente de confirmar bloquea a su dueño real. | Compromiso habitual de UX; riesgo bajo con venta persona a persona. |
| OCR: no hay límite de memoria/CPU por subproceso (RLIMIT) ni aislamiento; Tesseract con `RLIMIT_AS` suele fallar y no pude probarlo en Render. | Ya se acotó tamaño de página, concurrencia y tiempo. Revisar siempre contra el remito (la app ya lo advierte). |
| Dependencias sin archivo de bloqueo (rangos). | No pude generarlo sin red. Generar con `pip-compile` o `uv lock` en una máquina con red y fijar versiones. |
| `compute_tenant_monthly_analytics` y el backup JSON leen tablas completas. | Aceptable para comercios chicos; medir antes de escalar. |
| Módulo fiscal solo con campos argentinos; no emite comprobantes electrónicos. | Alcance del producto. |
| Endpoint público `/api/v1/auth/demo-accounts`: en producción devuelve lista vacía; lo usa el frontend. | No romper el login actual. |
| Frontend: `innerHTML` con `esc()` en todos los datos de usuario (revisado por muestreo, no exhaustivo); sin CSP de scripts porque la UI usa `onclick` en línea. | El rediseño debe eliminar handlers en línea y permitir una CSP estricta. |

## 3. Revisión de Docker y `render.yaml` (análisis estático)

**Lo que estaba bien:** instalación de Tesseract (`spa`, `eng`) y Poppler por `apt`, sin shell en los subprocesos, `PORT` respetado, `/healthz` como chequeo, `statement_cache_size=0` para el pooler de Neon, migraciones en transacción.

**Riesgos de despliegue que persisten (no verificables sin Render):**
1. `plan: free` duerme tras ~15 min y tiene 512 MB: para comercios reales no es aceptable (arranque en frío de ~1 min, y un cajero lo sufre). Pasar a `starter`. **Decisión de costo tuya; no lo cambié.**
2. `region: oregon` lejos de una base Neon en São Paulo (`sa-east-1`) suma ~150–200 ms **por consulta**; un pedido hace varias. Elegir la región de Render más cercana a la base o mover la base.
3. Neon suspende a los 5 min; la primera consulta tras la inactividad puede despertar la base o encontrar una conexión vieja en el pool. **Probar tras 10 minutos sin tráfico.**
4. El `apt-get` del build necesita red del builder de Render y suma tiempo (~1–2 min). Si el build falla acá, es el primer lugar donde mirar.
5. `TRUSTED_PROXY_HOPS=1` asume un proxy delante. Verificar tras el primer deploy que la IP de la auditoría (`audit_logs.ip_address`) sea la del cliente y no la del proxy; si hubiera un segundo proxy, subir a 2.
6. Memoria: Argon2 + Tesseract en 512 MB. Con `OCR_MAX_CONCURRENT=1` debería alcanzar, pero no está medido.

## 4. Pendiente antes de cargar datos reales de un comercio

### Bloqueantes
1. **Correr la suite** (SQLite y PostgreSQL) y corregir lo que falle por los cambios de esta revisión.
2. **Primer deploy real en Render + Neon** (nunca se hizo) y recorrer: registro → correo → login → abrir caja → vender → fiado → cobro → compra → cierre; OCR con un remito real; `/readyz`.
3. **Correo real de punta a punta** (SMTP): sin él nadie puede registrarse ni recuperar contraseña. Nunca se probó contra un proveedor real.
4. **Rol de base sin `BYPASSRLS`** confirmado (sin la advertencia en el log) o decisión consciente de depender solo del filtro por `tenant_id`.
5. **Restaurar un backup en un entorno aislado** (`docs/RESPALDO_POSTGRESQL.md`) y definir backups automáticos y su retención. Hoy no hay ninguno automático.
6. **Plan de pago en Render** (punto 3.1) y casilla del propietario SaaS con verificación en dos pasos.
7. **Aspectos legales:** términos y condiciones, política de privacidad, consentimiento para el análisis mensual y la licencia (el repositorio está bajo AGPL-3.0: revisar con un abogado qué implica ofrecerlo como servicio).

### Recomendados (primeras semanas)
- Matriz de permisos (qué puede hacer un cajero con precios, descuentos y stock).
- Conservar la operación pendiente en el navegador para que una recarga no duplique una venta.
- Archivo de bloqueo de dependencias y revisión de actualizaciones.
- Prueba de carga corta y prueba en celulares reales (nunca se hicieron).
- Decidir si se unifica la numeración de migraciones (solo con PostgreSQL de prueba disponible) y el límite de intentos de login persistente.

## 5. Limitaciones funcionales vigentes (de las tres tandas)

- Sin conciliación contra extractos bancarios o de billeteras; la caja concilia solo efectivo contado vs. esperado.
- Anular una venta cuyo fiado ya fue cobrado en parte está bloqueado (requiere reembolso manual).
- La PWA solo cachea la interfaz: **no se puede operar sin conexión**.
- No hay corte automático por vencimiento o impago: el corte y la reactivación son siempre manuales, con motivo y confirmación, y quedan en el historial (`audit_logs`). Un pago de suscripción no reactiva solo.
- La restauración de JSON solo entra en un comercio vacío de la misma moneda; no restaura usuarios ni configuración.
- Los informes no son contabilidad; los montos y fechas están pensados para ARS y zona horaria configurable.
- Los recordatorios por WhatsApp son enlaces `wa.me` de envío manual.
