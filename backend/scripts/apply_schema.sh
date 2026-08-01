#!/usr/bin/env bash
# Apply the canonical schema (backend/db/sql) to a database in dependency order.
# Usage: DATABASE_URL="postgres://user@host/db" ./scripts/apply_schema.sh
set -euo pipefail
: "${DATABASE_URL:?set DATABASE_URL}"
DIR="$(cd "$(dirname "$0")/../db/sql" && pwd)"
for f in "$DIR"/*.sql; do
  echo ">> applying $(basename "$f")"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$f"
done
echo "schema applied."
