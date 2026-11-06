CREATE TABLE ledger.owners (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE ledger.accounts (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL
        REFERENCES ledger.owners(id),
    name TEXT NOT NULL,
    allow_negative BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT accounts_name_length
        CHECK (char_length(name) BETWEEN 1 AND 100)
);

CREATE INDEX accounts_owner_id_idx
    ON ledger.accounts(owner_id);


CREATE TABLE ledger.transactions (
    id UUID PRIMARY KEY,
    reference TEXT,
    created_by UUID NOT NULL
        REFERENCES ledger.owners(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT transactions_reference_length
        CHECK (reference IS NULL OR char_length(reference) <= 140)
);