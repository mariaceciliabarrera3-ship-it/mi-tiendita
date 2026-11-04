#!/usr/bin/env bash
set -euo pipefail
: "${RESTORE_DATABASE_URL:?Configurá RESTORE_DATABASE_URL para una base VACÍA y separada}"
if [[ "$RESTORE_DATABASE_URL" != postgresql://* && "$RESTORE_DATABASE_URL" != postgres://* ]]; then echo 'Se requiere destino PostgreSQL' >&2; exit 1; fi
if [[ $# != 2 || "$2" != RESTAURAR_EN_BASE_VACIA || ! -f "$1" ]]; then echo 'Uso: restore_postgres.sh archivo.dump RESTAURAR_EN_BASE_VACIA' >&2; exit 1; fi
if [[ -n "${DATABASE_URL:-}" && "$DATABASE_URL" == "$RESTORE_DATABASE_URL" ]]; then echo 'Origen y destino son iguales. Abortando.' >&2; exit 1; fi
pg_restore --list "$1" >/dev/null
count=$(psql "$RESTORE_DATABASE_URL" -v ON_ERROR_STOP=1 -At -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")
if [[ "$count" != 0 ]]; then echo 'La base de destino no está vacía. Abortando sin borrar nada.' >&2; exit 1; fi
pg_restore --exit-on-error --single-transaction --no-owner --no-acl --dbname="$RESTORE_DATABASE_URL" "$1"
echo 'Copia restaurada. Verificá datos e inicio de sesión antes de cambiar la aplicación de base.'
