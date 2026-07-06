#!/usr/bin/env bash
# Bootstraps the Postgres instance bundled in the worker image (issue #786) so
# repo test suites — notably pioneer-square's own backend/tests — can run
# against a real Postgres without depending on the postgres-test compose
# service. Initialises a standalone cluster on first run (owned by the
# unprivileged `worker` user, no root/postgres-system-user juggling needed),
# starts it if not already running, then creates the `pioneer` role and the
# `pioneer` / `pioneer_test` databases matching DATABASE_URL and
# backend/tests/_test_config.py's default TEST_DATABASE_URL so both the
# runtime and tests auto-detect them with zero configuration.
set -euo pipefail

# The `postgresql` apt package's postinst creates a default cluster and
# registers it with postgresql-common, so pg_lsclusters reliably reports the
# installed major version without guessing at filesystem layout.
PG_VERSION="$(pg_lsclusters --no-header | awk '{print $1}' | head -1)"
if [ -z "$PG_VERSION" ]; then
    echo "ERROR: could not detect installed Postgres version via pg_lsclusters" >&2
    exit 1
fi

PG_BIN="/usr/lib/postgresql/$PG_VERSION/bin"
if [ ! -x "$PG_BIN/initdb" ]; then
    echo "ERROR: could not find initdb binary at $PG_BIN/initdb" >&2
    exit 1
fi
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
if ! pg_isready -h localhost -p "$PG_PORT" -q >/dev/null 2>&1; then
    echo "ERROR: Postgres did not become ready after 30 seconds" >&2
fi
pg_isready -h localhost -p "$PG_PORT" -q

if ! psql -h localhost -p "$PG_PORT" -U postgres -d postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='pioneer'" | grep -q 1; then
    psql -h localhost -p "$PG_PORT" -U postgres -d postgres -c \
        "CREATE ROLE pioneer LOGIN CREATEDB PASSWORD 'pioneer_password'" >/dev/null
fi

for db in pioneer pioneer_test; do
    if ! psql -h localhost -p "$PG_PORT" -U postgres -d postgres -tAc \
            "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
        createdb -h localhost -p "$PG_PORT" -U postgres -O pioneer "$db"
    fi
done

exec "$@"
