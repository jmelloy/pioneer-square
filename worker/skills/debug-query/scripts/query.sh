#!/usr/bin/env bash
# Post a single read-only SELECT to the backend's DEBUG_TOKEN-gated
# /debug/query endpoint (backend/routes/debug_query.py) and print the result.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $(basename "$0") '<SELECT statement>'" >&2
  exit 2
fi

if [ -z "${DEBUG_TOKEN:-}" ]; then
  echo "DEBUG_TOKEN is not set in this environment; the debug-query skill is unavailable." >&2
  exit 1
fi

backend_url="${PIONEER_BACKEND_URL:-http://backend:8000}"
sql="$1"

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

status=$(curl -sS -o "$body_file" -w '%{http_code}' -X POST "${backend_url%/}/debug/query" \
  -H "X-Debug-Token: ${DEBUG_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg sql "$sql" '{sql: $sql}')")

cat "$body_file"
echo

if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
  exit 1
fi
