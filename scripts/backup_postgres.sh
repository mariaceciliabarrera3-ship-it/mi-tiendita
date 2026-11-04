#!/usr/bin/env bash
set -euo pipefail
: "${DATABASE_URL:?Configurá DATABASE_URL con conexión PostgreSQL directa}"
if [[ "${DATABASE_URL}" != postgresql://* && "${DATABASE_URL}" != postgres://* ]]; then echo 'Se requiere DATABASE_URL PostgreSQL' >&2; exit 1; fi
if [[ $# != 1 || -e "$1" ]]; then echo 'Uso: backup_postgres.sh archivo-nuevo.dump (no sobrescribe)' >&2; exit 1; fi
umask 077
out=$1
tmp="${out}.partial.$$"
trap 'rm -f "$tmp"' EXIT
pg_dump --format=custom --no-owner --no-acl --file="$tmp" -- "$DATABASE_URL"
pg_restore --list "$tmp" >/dev/null
# hard-link fails if the destination was created meanwhile; never overwrite
ln "$tmp" "$out"
echo "Copia creada: $out. Contiene datos privados y hashes de contraseñas."
