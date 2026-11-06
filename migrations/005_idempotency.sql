CREATE TABLE ledger.idempotency_keys (
    owner_id UUID NOT NULL
        REFERENCES ledger.owners(id),

    key TEXT NOT NULL,

    request_hash BYTEA NOT NULL,

    transaction_id UUID
        REFERENCES ledger.transactions(id),

    response_status SMALLINT,

    response_body JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (owner_id, key),

    CONSTRAINT idempotency_key_length
        CHECK (char_length(key) BETWEEN 1 AND 255)
);