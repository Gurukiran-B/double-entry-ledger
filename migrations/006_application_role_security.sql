DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'ledger_account_locker'
    ) THEN
        CREATE ROLE ledger_account_locker
            NOLOGIN
            NOINHERIT
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$migration$;

ALTER ROLE ledger_account_locker
    NOLOGIN
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

ALTER ROLE ledger_app
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

DO $database_privileges$
BEGIN
    EXECUTE format(
        'REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC',
        current_database()
    );
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO ledger_app',
        current_database()
    );
END
$database_privileges$;

REVOKE ALL ON SCHEMA ledger FROM ledger_app;
GRANT USAGE ON SCHEMA ledger TO ledger_app;

REVOKE ALL ON TABLE
    ledger.schema_migrations,
    ledger.owners,
    ledger.accounts,
    ledger.transactions,
    ledger.entries,
    ledger.idempotency_keys
FROM ledger_app;

GRANT SELECT, INSERT ON
    ledger.owners,
    ledger.accounts,
    ledger.transactions,
    ledger.entries
TO ledger_app;

GRANT SELECT, INSERT, UPDATE ON ledger.idempotency_keys TO ledger_app;

REVOKE ALL ON SEQUENCE ledger.entries_id_seq FROM ledger_app;
GRANT USAGE ON SEQUENCE ledger.entries_id_seq TO ledger_app;

REVOKE UPDATE ON ledger.accounts FROM ledger_app;
REVOKE UPDATE (id) ON ledger.accounts FROM ledger_app;

GRANT USAGE ON SCHEMA ledger TO ledger_account_locker;
GRANT SELECT ON ledger.accounts TO ledger_account_locker;
GRANT UPDATE (id) ON ledger.accounts TO ledger_account_locker;

CREATE OR REPLACE FUNCTION ledger.lock_account_for_update(p_account_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ledger
AS $function$
BEGIN
    PERFORM account.id
    FROM ledger.accounts AS account
    WHERE account.id = p_account_id
    FOR NO KEY UPDATE;
END
$function$;

GRANT CREATE ON SCHEMA ledger TO ledger_account_locker;
ALTER FUNCTION ledger.lock_account_for_update(uuid) OWNER TO ledger_account_locker;
REVOKE CREATE ON SCHEMA ledger FROM ledger_account_locker;

REVOKE ALL ON FUNCTION ledger.lock_account_for_update(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger.lock_account_for_update(uuid) TO ledger_app;
