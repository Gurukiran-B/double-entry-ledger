
"""F8: Verify that an owner cannot debit another owner's account."""

from uuid import uuid4

import psycopg
import pytest

from ledger.config import DATABASE_URL
from ledger.transfer import transfer


def test_owner_cannot_debit_another_owners_account(
    test_admin_database_url: str,
) -> None:
    owner_a = uuid4()
    owner_b = uuid4()

    source_id = uuid4()
    destination_id = uuid4()
    funding_account_id = uuid4()
    funding_transaction_id = uuid4()

    with psycopg.connect(DATABASE_URL) as connection:
        try:
            # Create two owners and their accounts.
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ledger.owners (id, name)
                    VALUES (%s, %s), (%s, %s)
                    """,
                    (
                        owner_a, "F8 account owner",
                        owner_b, "F8 unauthorized owner",
                    ),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.accounts (id, owner_id, name)
                    VALUES (%s, %s, %s), (%s, %s, %s)
                    """,
                    (
                        source_id, owner_a, "F8 protected source",
                        destination_id, owner_b, "F8 destination",
                    ),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.accounts
                        (id, owner_id, name, allow_negative)
                    VALUES (%s, %s, %s, TRUE)
                    """,
                    (funding_account_id, owner_a, "F8 funding account"),
                )

                # Fund the protected source account.
                cursor.execute(
                    """
                    INSERT INTO ledger.transactions (id, created_by)
                    VALUES (%s, %s)
                    """,
                    (funding_transaction_id, owner_a),
                )

                cursor.execute(
                    """
                    INSERT INTO ledger.entries
                        (transaction_id, account_id, amount_minor)
                    VALUES (%s, %s, %s), (%s, %s, %s)
                    """,
                    (
                        funding_transaction_id, funding_account_id, -1000,
                        funding_transaction_id, source_id, 1000,
                    ),
                )

            connection.commit()

            # Owner B must not debit Owner A's source account.
            with pytest.raises(PermissionError, match="not authorized"):
                transfer(
                    connection,
                    source_id,
                    destination_id,
                    100,
                    owner_b,
                )

            # Verify that the protected balance remains unchanged.
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(amount_minor), 0)
                    FROM ledger.entries
                    WHERE account_id = %s
                    """,
                    (source_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == 1000

        finally:
            connection.rollback()

            # Use a separate cleanup transaction for isolated test fixtures.
            # Run only against a test database without concurrent application writes.
            with psycopg.connect(test_admin_database_url) as cleanup_conn:
                try:
                    with cleanup_conn.cursor() as cursor:
                        cursor.execute(
                            """
                            DELETE FROM ledger.idempotency_keys
                            WHERE owner_id IN (%s, %s)
                            """,
                            (owner_a, owner_b),
                        )

                        # Temporarily disable the table-wide trigger for cleanup.
                        cursor.execute(
                            "ALTER TABLE ledger.entries "
                            "DISABLE TRIGGER entries_immutable"
                        )

                        cursor.execute(
                            """
                            DELETE FROM ledger.entries
                            WHERE account_id = ANY(%s)
                            """,
                            ([source_id, destination_id, funding_account_id],),
                        )

                        cursor.execute(
                            """
                            DELETE FROM ledger.transactions
                            WHERE created_by IN (%s, %s)
                            """,
                            (owner_a, owner_b),
                        )

                        cursor.execute(
                            """
                            DELETE FROM ledger.accounts
                            WHERE id = ANY(%s)
                            """,
                            ([source_id, destination_id, funding_account_id],),
                        )

                        cursor.execute(
                            "ALTER TABLE ledger.entries "
                            "ENABLE TRIGGER entries_immutable"
                        )

                        cursor.execute(
                            """
                            DELETE FROM ledger.owners
                            WHERE id IN (%s, %s)
                            """,
                            (owner_a, owner_b),
                        )

                    cleanup_conn.commit()
                except Exception:
                    cleanup_conn.rollback()
                    raise
