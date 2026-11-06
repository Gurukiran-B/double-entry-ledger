CREATE TABLE ledger.entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    transaction_id UUID NOT NULL
        REFERENCES ledger.transactions(id),

    account_id UUID NOT NULL
        REFERENCES ledger.accounts(id),

    amount_minor BIGINT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT entries_amount_nonzero
        CHECK (amount_minor <> 0),

    CONSTRAINT entries_amount_cap
        CHECK (amount_minor BETWEEN -1000000000000 AND 1000000000000)
);

CREATE INDEX entries_account_id_id_idx
    ON ledger.entries(account_id, id);

CREATE INDEX entries_transaction_id_idx
    ON ledger.entries(transaction_id);