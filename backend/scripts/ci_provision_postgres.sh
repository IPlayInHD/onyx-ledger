#!/usr/bin/env bash
# Provision the PostgreSQL a CI job needs, from scratch, every time.
#
# Deliberately NOT a long-lived shared service. Every job that touches a database
# builds its own here and throws it away with the runner, so a green run can
# never depend on rows, roles, or grants some earlier run left behind — the exact
# failure mode that made an earlier pollution investigation necessary.
#
# The identity model matters as much as the freshness. Two roles are created:
#
#   onyx_migrator   owns the DDL. Superuser here ONLY because provisioning must
#                   create extensions and roles; it is the identity that applies
#                   the schema, never the identity the tests run as.
#   onyx_test       the runtime login, a member of onyx_app_rw. Created by
#                   run_backend_tests.sh. This is what the suite connects as.
#
# That separation is the point. A superuser BYPASSES row-level security, so a
# security suite run as one proves nothing about RLS — and reading a privilege
# result under the wrong identity is how an earlier investigation concluded a
# defect existed where none did.
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  postgresql-16 postgresql-16-pgvector postgresql-client-16

sudo pg_ctlcluster 16 main start || sudo pg_ctlcluster 16 main restart

HBA=/etc/postgresql/16/main/pg_hba.conf
# Local trust, on a database that exists for the length of one CI job and is
# reachable only from inside the runner. No password is embedded anywhere.
sudo sed -i '1i local all all trust'            "$HBA"
sudo sed -i '2i host  all all 127.0.0.1/32 trust' "$HBA"
sudo sed -i '3i host  all all ::1/128      trust' "$HBA"
sudo pg_ctlcluster 16 main reload

sudo -u postgres psql -q -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_migrator') THEN
    CREATE ROLE onyx_migrator LOGIN SUPERUSER;
  END IF;
END
$$;
SQL

echo "provisioned: $(sudo -u postgres psql -tAc 'SHOW server_version')"
sudo -u postgres psql -tAc "SELECT rolname, rolsuper, rolcanlogin FROM pg_roles WHERE rolname LIKE 'onyx%' ORDER BY 1;"
