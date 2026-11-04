# Arquitectura y seguridad (evaluación, no garantía)

## Arquitectura

Frontend HTML/CSS/JavaScript sin CDN y backend FastAPI con consultas SQL parametrizadas. El cliente habla con `/api/v1` mismo origen. PostgreSQL/asyncpg es la alternativa para persistencia de servidor; SQLite/aiosqlite es solo local/pruebas, con WAL y `BEGIN IMMEDIATE`. Migraciones en `migrations/<motor>/` y tabla de versiones/checksums. Los datos demo se generan explícitamente en `scripts/seed_demo.py` y tienen `is_demo`.

### Núcleo de datos

- `tenants`, `users`, `subscription_plans`, `subscription_payments`: administración del SaaS separada del comercio.
- `products`, `product_price_history`, `inventory_movements`: saldo y movimientos con entradas y salidas.
- `sales`, `sale_items`: totales y costo/precio congelados; `customer_ledger` para cargos y cobros; `customers.current_balance` es un saldo denormalizado.
- `suppliers`, `purchases`, `purchase_items`, `supplier_ledger`: compras, pagos y pasivos; `cash_sessions`, `cash_movements`: dinero por canal y turno.
- `fiscal_profiles`, `fiscal_vouchers`, `monthly_analysis_reports`, `audit_logs`: información opcional y auditoría.

Operaciones de venta, cobro y compra incluyen todas sus escrituras en una transacción DB. En PostgreSQL se bloquea la fila del comercio y luego productos/clientes para serializar las mutaciones; en SQLite se usa bloqueo de escritura `BEGIN IMMEDIATE`. Una clave idempotente única por tenant y tipo de tabla reduce duplicados con el mismo identificador. **No verifica que un reintento con misma clave tenga idéntico cuerpo**: responde con la primera operación. Hay que conservar la clave al reintentar y consultar el resultado; cambiarla solo al comenzar una operación nueva.

En PostgreSQL, RLS tiene `ENABLE` y `FORCE ROW LEVEL SECURITY` en tablas comerciales y configura `app.current_tenant_id` local a la transacción; la aplicación además filtra explícitamente `tenant_id`. Las tablas administrativas y de identidad no tienen RLS. El rol de la aplicación no debe tener `SUPERUSER` ni `BYPASSRLS`; comprobarlo en la plataforma elegida. No se han validado todas las relaciones cruzadas con FK compuestas. Una cuenta superadministradora tiene acceso privilegiado funcional y DB privilegiada puede saltarse protección; RLS no elimina necesidad de controlar infraestructura ni consentimiento.

## Amenazas y decisiones

Contraseñas Argon2id; JWT firmado HS256 y cookie HttpOnly, SameSite=Lax y Secure en `APP_ENV=production`; comprobación de usuario activo y versión de token en cada solicitud. Backend usa parámetros SQL. Mutaciones navegador con `Origin` presente deben coincidir con Host; no se habilita CORS general. Hay rol de solo lectura. La app no expone contraseñas en el backup del comercio. Se registran acciones sensibles principales en auditoría, aunque no está completa ni es inmutable a nivel motor. Ver la lista de brechas en `AUDITORIA_Y_PENDIENTES.md`; no constituye una certificación de seguridad.

No hay agente con permisos de escritura. El ayudante ejecuta reglas fijas de lectura sobre información del comercio; una respuesta depende de la calidad de los registros. Riesgo principal: inferencias engañosas por entradas incompletas; revisar y no ejecutar automáticamente. No hay costo marginal por API de IA. La alternativa convencional es mostrar alertas calculadas; esto es lo implementado.

## Respaldos y recuperación

La UI permite exportar datos estructurados JSON y planilla XLSX. El archivo contiene información sensible de clientes y transacciones; almacenar cifrado, con acceso restringido y rotación. No se incluyen hashes de contraseña, usuarios ni auditoría en el JSON. No constituye respaldo íntegro ni consistente de la instalación. Ahora existe una restauración transaccional de JSON hacia un comercio vacío con la misma moneda, pero no restaura usuarios, configuraciones ni infraestructura y no verifica todas las invariantes. Ver `AUDITORIA_Y_PENDIENTES.md` (sección 5).

Para PostgreSQL utilizar backup lógico completo con `pg_dump --format=custom --no-owner` contra una conexión directa segura, cifrarlo y almacenarlo en un destino independiente con autorización apropiada. Restaurar periódicamente en una base aislada con `pg_restore`, aplicar migraciones y verificar kárdex y saldos. Registrar RPO/RTO y probar todo el procedimiento antes de operar. El backup nativo/retención dependen del proveedor y plan; no se proporciona servicio automático ni almacenamiento externo gratuito garantizado. Respetar la legislación aplicable a datos personales.
