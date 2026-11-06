
"""F3: Verify idempotent retries and reject conflicting key reuse."""

from uuid import uuid4

import psycopg
import pytest

from ledger.config import DATABASE_URL
from ledger.transfer import (
    IdempotencyConflictError,
    get_balance,
    transfer_idempotently,
)


def test_retry_returns_original_transaction_without_double_debit(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    destination_id = uuid4()
    system_id = uuid4()
    funding_transaction_id = uuid4()
    idempotency_key = f"f3-{uuid4()}"

    try:
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ledger.owners (id, name) VALUES (%s, %s)",
                    (owner_id, "F3 idempotency test"),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.accounts (id, owner_id, name)
                    VALUES (%s, %s, %s), (%s, %s, %s)
                    """,
                    (
                        source_id,
                        owner_id,
                        "F3 source",
                        destination_id,
                        owner_id,
                        "F3 destination",
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.accounts
                        (id, owner_id, name, allow_negative)
                    VALUES (%s, %s, %s, TRUE)
                    """,
                    (system_id, owner_id, "F3 funding system account"),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.transactions (id, created_by)
                    VALUES (%s, %s)
                    """,
                    (funding_transaction_id, owner_id),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.entries
                        (transaction_id, account_id, amount_minor)
                    VALUES (%s, %s, %s), (%s, %s, %s)
                    """,
                    (
                        funding_transaction_id,
                        system_id,
                        -1000,
                        funding_transaction_id,
                        source_id,
                        1000,
                    ),
                )

            connection.commit()

            # First request commits the transfer.
            first = transfer_idempotently(
                connection,
                source_id,
                destination_id,
                500,
                owner_id,
                idempotency_key,
            )

            # Retrying the same request must return the original result.
            second = transfer_idempotently(
                connection,
                source_id,
                destination_id,
                500,
                owner_id,
                idempotency_key,
            )

            assert first["status"] == 201
            assert first["replayed"] is False
            assert second["status"] == 201
            assert second["replayed"] is True

            assert (
                first["body"]["transaction_id"]
                == second["body"]["transaction_id"]
            )

            transfer_id = first["body"]["transaction_id"]

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ledger.entries
                    WHERE transaction_id = %s
                    """,
                    (transfer_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                entry_count = row[0]

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ledger.transactions
                    WHERE created_by = %s
                    """,
                    (owner_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                transaction_count = row[0]

            assert entry_count == 2
            assert transaction_count == 2  # Funding + one transfer
            assert get_balance(connection, source_id) == 500
            assert get_balance(connection, destination_id) == 500

            # Reusing the key for a different request must be rejected.
            with pytest.raises(IdempotencyConflictError):
                transfer_idempotently(
                    connection,
                    source_id,
                    destination_id,
                    400,
                    owner_id,
                    idempotency_key,
                )

            # The conflicting request must not change balances or history.
            assert get_balance(connection, source_id) == 500
            assert get_balance(connection, destination_id) == 500

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ledger.transactions
                    WHERE created_by = %s
                    """,
                    (owner_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == 2

    finally:
        # Test-only cleanup. Use an isolated development/test database.
        with psycopg.connect(test_admin_database_url) as cleanup_conn:
            try:
                with cleanup_conn.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM ledger.idempotency_keys
                        WHERE owner_id = %s
                        """,
                        (owner_id,),
                    )

                    # Temporarily disable immutability for isolated cleanup.
                    cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "DISABLE TRIGGER entries_immutable"
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.entries
                        WHERE account_id = ANY(%s)
                        """,
                        ([source_id, destination_id, system_id],),
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.transactions
                        WHERE created_by = %s
                        """,
                        (owner_id,),
                    )

                    cursor.execute(
                        """
                        DELETE FROM ledger.accounts
                        WHERE owner_id = %s
                        """,
                        (owner_id,),
                    )

                    cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "ENABLE TRIGGER entries_immutable"
                    )

                    cursor.execute(
                        "DELETE FROM ledger.owners WHERE id = %s",
                        (owner_id,),
                    )

                cleanup_conn.commit()
            except Exception:
                # Roll back cleanup and the trigger state change together.
                cleanup_conn.rollback()
                raise
