#!/usr/bin/env bash
set -euo pipefail

backup_dir="${BACKUP_DIR:-./backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
: "${BACKUP_AGE_RECIPIENT:?Set BACKUP_AGE_RECIPIENT to the organization age public recipient}"
backup_file="${backup_dir}/Grace-${timestamp}.dump.age"
mkdir -p "$backup_dir"
umask 077
trap 'rm -f "${backup_file}.tmp"' EXIT

docker compose exec -T db sh -eu -c \
  'pg_dump --no-owner --format=custom --username "$POSTGRES_USER" "$POSTGRES_DB"' \
  | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" > "${backup_file}.tmp"
mv "${backup_file}.tmp" "$backup_file"
printf 'PostgreSQL backup created: %s\n' "$backup_file"
