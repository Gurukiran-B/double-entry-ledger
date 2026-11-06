
"""F9: Idempotency keys must be isolated between owners."""

from uuid import UUID, uuid4

import psycopg

from ledger.config import DATABASE_URL
from ledger.transfer import transfer_idempotently


def test_same_idempotency_key_is_isolated_between_owners(
    test_admin_database_url: str,
) -> None:
    owner_a, owner_b = uuid4(), uuid4()
    key = "F9-shared-key"

    source_a, destination_a = uuid4(), uuid4()
    source_b, destination_b = uuid4(), uuid4()
    funding_a, funding_b = uuid4(), uuid4()
    funding_tx_a, funding_tx_b = uuid4(), uuid4()

    account_ids = (
        source_a,
        destination_a,
        funding_a,
        source_b,
        destination_b,
        funding_b,
    )

    try:
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ledger.owners (id, name)
                    VALUES (%s, %s), (%s, %s)
                    """,
                    (owner_a, "F9 owner A", owner_b, "F9 owner B"),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.accounts (id, owner_id, name)
                    VALUES
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s)
                    """,
                    (
                        source_a, owner_a, "F9 A source",
                        destination_a, owner_a, "F9 A destination",
                        funding_a, owner_a, "F9 A funding",
                        source_b, owner_b, "F9 B source",
                        destination_b, owner_b, "F9 B destination",
                        funding_b, owner_b, "F9 B funding",
                    ),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.transactions (id, created_by)
                    VALUES (%s, %s), (%s, %s)
                    """,
                    (funding_tx_a, owner_a, funding_tx_b, owner_b),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.entries
                        (transaction_id, account_id, amount_minor)
                    VALUES
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s),
                        (%s, %s, %s)
                    """,
                    (
                        funding_tx_a, funding_a, -1000,
                        funding_tx_a, source_a, 1000,
                        funding_tx_b, funding_b, -1000,
                        funding_tx_b, source_b, 1000,
                    ),
                )

            connection.commit()

            # The same key can be used independently by both owners.
            result_a = transfer_idempotently(
                connection, source_a, destination_a, 100, owner_a, key
            )
            result_b = transfer_idempotently(
                connection, source_b, destination_b, 200, owner_b, key
            )

            assert result_a["replayed"] is False
            assert result_b["replayed"] is False
            assert result_a["body"]["transaction_id"] != (
                result_b["body"]["transaction_id"]
            )

            # Each owner's retry replays only their own result.
            retry_a = transfer_idempotently(
                connection, source_a, destination_a, 100, owner_a, key
            )
            retry_b = transfer_idempotently(
                connection, source_b, destination_b, 200, owner_b, key
            )

            assert retry_a["replayed"] is True
            assert retry_b["replayed"] is True
            assert retry_a["body"]["transaction_id"] == (
                result_a["body"]["transaction_id"]
            )
            assert retry_b["body"]["transaction_id"] == (
                result_b["body"]["transaction_id"]
            )

            # Verify that retries did not cause duplicate debits.
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT account_id, SUM(amount_minor)
                    FROM ledger.entries
                    WHERE account_id IN (%s, %s, %s, %s)
                    GROUP BY account_id
                    """,
                    (source_a, destination_a, source_b, destination_b),
                )
                balances: dict[UUID, int] = dict(cursor.fetchall())

            assert balances[source_a] == 900
            assert balances[destination_a] == 100
            assert balances[source_b] == 800
            assert balances[destination_b] == 200

    finally:
        # Privileged cleanup for isolated test fixtures only.
        with psycopg.connect(test_admin_database_url) as cleanup:
            try:
                with cleanup.cursor() as cursor:
                    # Remove idempotency records before their transactions.
                    cursor.execute(
                        """
                        DELETE FROM ledger.idempotency_keys
                        WHERE owner_id IN (%s, %s)
                        """,
                        (owner_a, owner_b),
                    )

                    # Existing history is immutable during normal operation.
                    # Disable the trigger only for this test cleanup transaction.
                    cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "DISABLE TRIGGER entries_immutable"
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.entries
                        WHERE account_id = ANY(%s)
                        """,
                        (list(account_ids),),
                    )

                    # Includes funding and transfer transactions.
                    cursor.execute(
                        """
                        DELETE FROM ledger.transactions
                        WHERE created_by IN (%s, %s)
                        """,
                        (owner_a, owner_b),
                    )

                    cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "ENABLE TRIGGER entries_immutable"
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.accounts
                        WHERE id = ANY(%s)
                        """,
                        (list(account_ids),),
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.owners
                        WHERE id IN (%s, %s)
                        """,
                        (owner_a, owner_b),
                    )

                cleanup.commit()
            except Exception:
                # PostgreSQL rolls back the trigger change as well.
                cleanup.rollback()
                raise
