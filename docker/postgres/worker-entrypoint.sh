#!/usr/bin/env bash
# Bootstraps the Postgres instance bundled in the worker image (issue #786) so
# repo test suites — notably pioneer-square's own backend/tests — can run
# against a real Postgres without depending on the postgres-test compose
# service. Initialises a standalone cluster on first run (owned by the
# unprivileged `worker` user, no root/postgres-system-user juggling needed),
# starts it if not already running, then creates the `pioneer` role and
# `pioneer_test` database matching backend/tests/_test_config.py's default
# TEST_DATABASE_URL so tests auto-detect it with zero configuration.
set -euo pipefail

PG_BIN="$(dirname "$(find /usr/lib/postgresql -maxdepth 3 -name initdb | sort -V | tail -1)")"
export PATH="$PG_BIN:$PATH"

PGDATA="${PGDATA:-/home/worker/pgdata}"
PG_PORT="${PG_PORT:-5433}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    initdb --username=postgres --auth=trust --locale=C.UTF-8 --encoding=UTF8 -D "$PGDATA" >/dev/null
fi

if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    pg_ctl -D "$PGDATA" -l "$PGDATA/postgres.log" \
        -o "-p $PG_PORT -c listen_addresses=localhost -c unix_socket_directories=$PGDATA" \
        start
fi

for _ in $(seq 1 30); do
    if pg_isready -h localhost -p "$PG_PORT" -q; then
        break
    fi
    sleep 1
done
pg_isready -h localhost -p "$PG_PORT" -q

if ! psql -h localhost -p "$PG_PORT" -U postgres -d postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='pioneer'" | grep -q 1; then
    psql -h localhost -p "$PG_PORT" -U postgres -d postgres -c \
        "CREATE ROLE pioneer LOGIN SUPERUSER PASSWORD 'pioneer_password'" >/dev/null
fi

if ! psql -h localhost -p "$PG_PORT" -U postgres -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname='pioneer_test'" | grep -q 1; then
    createdb -h localhost -p "$PG_PORT" -U postgres -O pioneer pioneer_test
fi

exec "$@"
