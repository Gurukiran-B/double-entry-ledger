-- F10: Make historical ledger entries immutable.

CREATE OR REPLACE FUNCTION ledger.reject_ledger_entry_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, ledger
AS $$
BEGIN
    RAISE EXCEPTION 'Ledger entries are immutable'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS entries_immutable ON ledger.entries;

CREATE TRIGGER entries_immutable
BEFORE UPDATE OR DELETE ON ledger.entries
FOR EACH ROW
EXECUTE FUNCTION ledger.reject_ledger_entry_mutation();
