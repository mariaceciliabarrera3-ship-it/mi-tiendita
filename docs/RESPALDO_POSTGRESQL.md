# Respaldar y restaurar PostgreSQL integralmente

Esta es la recuperación de la **base completa**, no el JSON por comercio. `pg_dump` en formato custom captura un snapshot consistente de la base: todos los comercios, datos comerciales, esquema, auditoría, usuarios de Mi Tiendita y sus hashes de contraseña. `pg_restore` lo reconstruye. **No** incluye roles y contraseñas de PostgreSQL a nivel clúster, permisos/propietarios originales (`--no-owner --no-acl`), variables de entorno, clave JWT, secretos SMTP, configuración del hosting, archivos externos ni otros servicios. Guardá esos elementos por separado en un gestor de secretos; crear roles y permisos necesarios antes de la restauración. La app no guarda fotos de OCR como archivos permanentes. El JSON anterior tampoco reemplaza este respaldo.

## Punto crítico: RLS y privilegios
Las tablas tienen `FORCE ROW LEVEL SECURITY`. Un usuario común —incluso el dueño de la tabla— puede recibir `query would be affected by row-level security policy`. No desactives RLS ni uses un dump parcial como si fuera integral. Para la copia se requiere un rol dedicado con privilegios de lectura de todos los objetos **y capacidad de omitir RLS (`BYPASSRLS` o equivalente del proveedor)**, con acceso seguro y limitado. En PostgreSQL autogestionado, un administrador podría crear un rol `LOGIN BYPASSRLS`, conceder `CONNECT`, `USAGE` al esquema y `SELECT` en todas las tablas/secuencias y configurar privilegios predeterminados para objetos nuevos de cada propietario. La cuenta de la aplicación no debería tener BYPASSRLS. Algunos proveedores gestionados no permiten crear este rol: consultá su solución administrativa/backup integral y probá una restauración; no se validó con Neon. No asumir que Neon o Render Free proveen respaldo recuperable o retención suficiente.

## Frecuencia y custodia
- Ejecutá al menos diariamente y antes de una migración/actualización; acordá cuánto trabajo es aceptable perder. `pg_dump` es un snapshot: no cubre cambios posteriores. Para menos pérdida, aumentar frecuencia (no hay PITR en esta entrega).
- Hacé la tarea programada en una computadora/servidor confiable, externo al filesystem efímero del hosting; comprobá código de salida, tamaño y notificá fallos. Conservá copias cifradas, fuera del servidor y preferentemente en dos ubicaciones; por ejemplo 7 diarias y 4 semanales, según necesidad y obligaciones legales. Controlá acceso y costo de almacenamiento/egreso. No poner copias en GitHub ni ZIP de distribución.
- Las copias contienen datos privados y hashes: permisos 600, cifrado con herramienta confiable, clave guardada aparte, traslado seguro TLS. Una persona con acceso a la copia puede ver datos y atacar hashes offline. Simulá recuperación al menos mensualmente y tras cambios relevantes.

## Preparación y copia
1. Instalar cliente PostgreSQL: `pg_dump`, `pg_restore` y `psql` de versión **igual o mayor que el servidor** y compatibles con el destino. En Linux Debian, `sudo apt install postgresql-client`. Conseguir URL directa TLS con rol privilegiado de backup, no un pooler transaccional. Mantener secretos fuera de repositorios y logs. La URL puede ser visible a otros procesos locales; usá un host de confianza y, si es viable, autenticación `.pgpass` con permisos 600 sin contraseña en la URL.
2. En una terminal privada, definir la URL de backup real como variable `DATABASE_URL` (SSL apropiado; `sslmode=require` o verificación más estricta según proveedor). Ejemplo ilustrativo sin credenciales reales:

```sh
export DATABASE_URL='postgresql://rol_backup@servidor/base?sslmode=require'
mkdir -p backups; chmod 700 backups
scripts/backup_postgres.sh "backups/tiendita-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

El script no sobrescribe, limita permisos, verifica que el archivo pueda listarse y falla si `pg_dump` falla. Listar no sustituye restaurar. En cron hay que suministrar las variables de forma segura y supervisar el resultado; la app no agenda ni almacena backups automáticamente. Cifrar la copia terminada y probar la clave de recuperación.

## Restaurar sin tocar la base activa
1. Crear en PostgreSQL una **base nueva y vacía**, con rol apropiado propietario de sus objetos y versión compatible. Verificar nombre, servidor y costo del destino. No usar nunca la base activa; estas instrucciones no hacen limpieza destructiva ni sustituyen físicamente la base.
2. Definir `RESTORE_DATABASE_URL` con el destino vacío, acceso TLS, y mantener `DATABASE_URL` apuntando al origen (o no definida). Usar la copia descifrada en un directorio privado:

```sh
export RESTORE_DATABASE_URL='postgresql://rol_destino@servidor/base_nueva?sslmode=require'
scripts/restore_postgres.sh backups/archivo.dump RESTAURAR_EN_BASE_VACIA
```

El script comprueba que las URLs no sean idénticas y que no haya relaciones de usuario en el destino, y restaura en una sola transacción. URLs distintas pueden apuntar a la misma base por alias: **comprobalo manualmente**. La comprobación no es un bloqueo frente a otro proceso creando objetos simultáneamente: destiná una base exclusiva sin conexiones de terceros. Si falla, revisá versiones, permisos, extensiones y logs sin revelar secretos; no apuntes a la base original para reintentar.
3. Ejecutá una instancia de prueba con la URL nueva, la misma configuración y clave JWT guardada por separado (si se desea conservar sesiones), y sin activar semilla demo. Probá login de la propietaria SaaS y de al menos dos comercios, verificá productos, stock, ventas y aislamiento; compará totales e historial con la copia. Luego, solo cuando la verificación sea satisfactoria, planificá mantenimiento y cambio de conexión de producción, cuidando no perder escrituras ocurridas después de la copia. Revocar sesiones/rotar JWT si hubo exposición de secretos.

## Prueba hecha en esta entrega
Se crearon dos bases PostgreSQL 17 temporales y vacías: origen y destino. Dos comercios con un producto cada uno y usuarios con contraseñas de prueba; se ejecutó `pg_dump` con un rol dedicado `BYPASSRLS`, seguido de `pg_restore` como dueño de la base destino. Se compararon hashes de **todos los usuarios**, cantidad de comercios, login correcto e incorrecto, aislamiento y stock de ambos comercios. Un segundo intento de restauración a destino no vacío fue rechazado. Las bases y el archivo temporal se eliminaron al terminar. La prueba está en `tests/verify_postgres_backup.py`; exige dos bases con nombres fijos de prueba, un rol temporal `tiendita_backup_test` precreado con BYPASSRLS y tres URLs de entorno (`TEST_BACKUP_SOURCE_URL`, `TEST_BACKUP_DUMP_URL`, `TEST_BACKUP_RESTORE_URL`). No ejecutar contra datos reales. No se probó con proveedor remoto, claves reales, gestión automatizada, cifrado, ni todos los tipos de datos comerciales.
