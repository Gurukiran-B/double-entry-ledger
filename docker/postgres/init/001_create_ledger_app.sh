#!/bin/sh
set -eu

: "${LEDGER_APP_PASSWORD:?LEDGER_APP_PASSWORD must be set}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=ledger_database="$POSTGRES_DB" \
  --set=ledger_app_password="$LEDGER_APP_PASSWORD" <<'SQL'
CREATE ROLE ledger_app
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'ledger_app_password';
GRANT CONNECT ON DATABASE :"ledger_database" TO ledger_app;
SQL

if [ "$POSTGRES_DB" = "ledger_test" ]; then
  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --command="COMMENT ON DATABASE ledger_test IS 'ledger-experiment-tests-only'"
fi
