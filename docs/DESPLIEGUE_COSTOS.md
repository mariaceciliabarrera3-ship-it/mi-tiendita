# Despliegue experimental, costos y modelo comercial

## Prueba pública de costo inicial cero, no producción

1. Crear un proyecto PostgreSQL gratuito propio en Neon y un servicio web gratuito en Render con el repositorio GitHub que contenga este proyecto. Revisar en cada proveedor requisitos de registro, región, términos comerciales, límites y disponibilidad vigentes. Estas instrucciones son un procedimiento propuesto, no evidencia de que el despliegue se haya realizado.
2. Elegir Blueprint `render.yaml`. En Render configurar `DATABASE_URL` de Neon con TLS (`?sslmode=require`); nunca usar SQLite en Render: su sistema de archivos es efímero. Configurar `SUPERADMIN_EMAIL` (casilla propia, obligatoria en producción), las variables de correo (`MAIL_MODE=smtp`, `PUBLIC_BASE_URL`, `SMTP_*`), una contraseña aleatoria larga en `SUPERADMIN_PASSWORD` y `JWT_SECRET_KEY` aleatoria de al menos 32 caracteres; generar con `python -c 'import secrets;print(secrets.token_urlsafe(48))'`. No incluir secretos en Git. `APP_ENV=production` y `ENABLE_DEMO_SEED=false`. Usar HTTPS y rol SQL sin SUPERUSER ni BYPASSRLS. El esquema se migra al inicio: usar una base nueva y exclusiva y revisar permisos antes de aplicar.
3. Comprobar `/readyz`, registro, login, RLS con cuentas de ensayo, respaldos y restauración en entorno aislado. Monitorear cuota desde ambos tableros. Nunca compartir la consola o DB con comercios. En el plan gratuito Render no recomienda producción; esta versión además tiene brechas listadas en `AUDITORIA_Y_PENDIENTES.md`.
4. Para prevenir cargos inesperados: no habilitar facturación, no introducir tarjeta cuando no sea necesaria, no activar planes de consumo ni upgrade automático, verificar políticas actuales de los proveedores. Un corte por superar cuota es preferible a una factura inesperada si se exige estrictamente costo cero, pero no brinda continuidad comercial.

Render Free publicado: 750 horas por workspace/mes; duerme tras 15 minutos y puede tardar ~un minuto en reactivarse. El disco es efímero y no se puede agregar disco persistente gratis. Ancho de banda y builds tienen cuotas; con un método de pago podrían facturarse excedentes, sin método de pago pueden suspender servicios o builds. No recomendamos mecanismos de keep-alive que consuman cuota. Render Postgres Free caduca a los 30 días. Ver [documentación oficial](https://render.com/docs/free) y [precios](https://render.com/pricing).

Neon Free publica 100 CU-horas mensuales/proyecto, 1 GB de almacenamiento/proyecto (20 GB/cuenta), 5 GB de egress/proyecto y suspensión tras 5 minutos sin actividad. Sin SLA; límites sujetos a cambios. Llegar a la cuota puede interrumpir operaciones. No se activa un plan de pago automáticamente como parte de esta aplicación; comprobar en el panel y en condiciones vigentes. Fuentes: [planes oficiales](https://neon.com/docs/introduction/plans), [precios](https://neon.com/pricing). Valores consultados el 1 de noviembre de 2026.

## Costos de infraestructura vs precio al cliente

| Comercios | Infraestructura experimental Free | Hipótesis pagada ilustrativa* |
|---|---:|---:|
| Inicio | USD 0 mientras se respeten cuotas | desde USD 7 + consumo PostgreSQL |
| 10 | USD 0 solo si carga/almacenamiento caben | no medido; presupuesto inicial orientativo USD 22/mes |
| 50 | USD 0 solo si caben; interrupción posible | no medido; USD 22/mes no es garantía |
| 100 | USD 0 solo si caben; interrupción posible | no medido; USD 22/mes no es garantía |

\* Ejemplo aritmético, no cotización ni garantía: Render web pago desde USD 7/mes más gasto típico mostrado de Neon Launch USD 15/mes = USD 22/mes; adicional por tráfico, backup, impuestos, dominio, soporte y crecimiento. El consumo depende de operaciones, simultaneidad, datos y retención, no solo de cantidad de comercios. Los planes pagos pueden cobrar por uso: establecer límites y aprobación expresa antes de contratarlos. El costo cero posible para prueba no implica una solución confiable para 10, 50 ni 100 comercios reales.

Modelo comercial: cobrar mensualidad al comerciante (por transferencia inicialmente), registrar el pago desde administración y habilitar el servicio adicional de análisis con acuerdo explícito. Las mensualidades pertenecen a la propietaria SaaS, no a la caja de sus comercios. Precios del esquema son ejemplos en ARS a validar con entrevistas y costos reales; no garantizan rentabilidad, no tienen ajuste inflacionario automático ni representan cobro implementado. Falta facturación, vencimientos automáticos, conciliación bancaria y pasarela. Antes de cobrar suscripciones verificar legislación, consentimiento, impuestos y servicio posventa.

## Evolución priorizada

1. Cerrar brechas financieras, integridad y seguridad enumeradas; ingeniería de recuperación, pruebas adversariales, conciliación integral y auditoría independiente.
2. Completar UI móvil y pruebas de accesibilidad/navegador, controles de sesión, expiración, permisos y límites por plan, consentimiento verificable y backup restaurable.
3. Pruebas de carga reales, monitoreo y costos; después contratar capacidad y backups según ingresos y disponibilidad requerida.
4. Integraciones fiscales/pagos/WhatsApp solo con requisitos legales, homologación, credenciales y aprobación humana. IA agéntica no recomendada inicialmente: las reglas actuales resuelven alertas sin costo y sin modificar datos; una futura IA solo debería explicar métricas verificables, con consentimiento, límites, costo medido y sin acceso de escritura.
