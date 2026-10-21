#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_FILE:?Set BACKUP_FILE to a specific PostgreSQL custom-format backup}"
: "${RESTORE_DATABASE:?Set RESTORE_DATABASE to a new disposable database name}"
: "${RESTORE_AGE_IDENTITY:?Set RESTORE_AGE_IDENTITY to the protected age identity file}"
case "$RESTORE_DATABASE" in
  *[!a-zA-Z0-9_]*|"") echo "RESTORE_DATABASE may contain only letters, numbers, and underscores." >&2; exit 2 ;;
esac
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file does not exist." >&2
  exit 2
fi

docker compose exec -T db sh -eu -c \
  'psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 --command "CREATE DATABASE \"'"$RESTORE_DATABASE"'\";"'
age --decrypt --identity "$RESTORE_AGE_IDENTITY" "$BACKUP_FILE" \
  | docker compose exec -T db sh -eu -c \
    'pg_restore --no-owner --exit-on-error --username "$POSTGRES_USER" --dbname "'"$RESTORE_DATABASE"'"'
docker compose exec -T db sh -eu -c \
  'psql --username "$POSTGRES_USER" --dbname "'"$RESTORE_DATABASE"'" --set ON_ERROR_STOP=1 --tuples-only --command "SELECT version_num FROM alembic_version;"'
printf 'Restore drill completed into disposable database %s.\n' "$RESTORE_DATABASE"
