#!/bin/sh
set -eu

: "${LEDGER_APP_PASSWORD:?LEDGER_APP_PASSWORD must be set}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=ledger_app_password="$LEDGER_APP_PASSWORD" <<'SQL'
CREATE ROLE ledger_app
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'ledger_app_password';
GRANT CONNECT ON DATABASE ledger TO ledger_app;
SQL
