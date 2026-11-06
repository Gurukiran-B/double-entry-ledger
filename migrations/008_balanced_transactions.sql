-- Enforce balanced double-entry transactions at commit time.

CREATE OR REPLACE FUNCTION ledger.check_transaction_balance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, ledger
AS $function$
DECLARE
    target_transaction_id uuid;
    entry_count bigint;
    balance numeric;
BEGIN
    IF TG_TABLE_NAME = 'transactions' THEN
        target_transaction_id := NEW.id;
    ELSE
        target_transaction_id := NEW.transaction_id;
    END IF;

    SELECT
        COUNT(*),
        COALESCE(SUM(amount_minor), 0)
    INTO entry_count, balance
    FROM ledger.entries
    WHERE transaction_id = target_transaction_id;

    IF entry_count < 2 OR balance <> 0 THEN
        RAISE EXCEPTION
            'Transaction % is unbalanced: % entries, balance %',
            target_transaction_id,
            entry_count,
            balance
            USING ERRCODE = '23514';
    END IF;

    RETURN NULL;
END;
$function$;

CREATE CONSTRAINT TRIGGER transactions_must_balance
AFTER INSERT ON ledger.transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION ledger.check_transaction_balance();

CREATE CONSTRAINT TRIGGER entries_must_balance
AFTER INSERT ON ledger.entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION ledger.check_transaction_balance();
