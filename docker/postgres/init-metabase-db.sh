#!/usr/bin/env bash
# Creates a dedicated Postgres role and database for Metabase on first boot.
# Skipped (with a warning) if METABASE_DB_PASSWORD is unset.
set -euo pipefail

if [ -z "${METABASE_DB_PASSWORD:-}" ]; then
    echo "WARNING: METABASE_DB_PASSWORD is unset — skipping Metabase DB/role creation." >&2
    echo "         Set METABASE_DB_PASSWORD and recreate the postgres volume to create the metabase role." >&2
    exit 0
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase') THEN
            CREATE ROLE metabase WITH LOGIN PASSWORD '${METABASE_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    SELECT 'CREATE DATABASE metabase OWNER metabase'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'metabase')\gexec
EOSQL
