"""F7: Verify non-positive transfer amounts are rejected."""

from uuid import uuid4

import psycopg
import pytest

from ledger.config import DATABASE_URL
from ledger.transfer import InsufficientFundsError, transfer


def test_negative_amount_is_rejected_without_ledger_writes(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    destination_id = uuid4()

    with psycopg.connect(DATABASE_URL) as connection:
        try:
            # Create isolated test data.
            with psycopg.connect(test_admin_database_url) as cleanup:
                with cleanup.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO ledger.owners (id, name)
                        VALUES (%s, %s)
                        """,
                        (owner_id, "F7 negative amount test"),
                    )
                    cursor.execute(
                        """
                        INSERT INTO ledger.accounts (id, owner_id, name)
                        VALUES (%s, %s, %s), (%s, %s, %s)
                        """,
                        (
                            source_id,
                            owner_id,
                            "F7 source",
                            destination_id,
                            owner_id,
                            "F7 destination",
                        ),
                    )
                cleanup.commit()

            # Record the initial state.
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
                transactions_before = row[0]

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ledger.entries
                    WHERE account_id IN (%s, %s)
                    """,
                    (source_id, destination_id),
                )
                row = cursor.fetchone()
                assert row is not None
                entries_before = row[0]

            # Finish the read transaction before calling transfer().
            connection.commit()

            # A negative amount must be rejected.
            with pytest.raises(
                ValueError,
                match="Transfer amount must be positive",
            ):
                transfer(
                    connection,
                    source_id,
                    destination_id,
                    -100,
                    owner_id,
                )

            # Verify no transaction or ledger entry was added.
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
                transactions_after = row[0]

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ledger.entries
                    WHERE account_id IN (%s, %s)
                    """,
                    (source_id, destination_id),
                )
                row = cursor.fetchone()
                assert row is not None
                entries_after = row[0]

            assert transactions_after == transactions_before
            assert entries_after == entries_before

        finally:
            if not connection.closed:
                connection.rollback()

            with psycopg.connect(test_admin_database_url) as cleanup:
                with cleanup.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM ledger.entries
                        WHERE account_id IN (%s, %s)
                        """,
                        (source_id, destination_id),
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
                        """
                        DELETE FROM ledger.owners
                        WHERE id = %s
                        """,
                        (owner_id,),
                    )
                cleanup.commit()


def test_allow_negative_does_not_permit_overdraft(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    destination_id = uuid4()

    try:
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ledger.owners (id, name) VALUES (%s, %s)",
                    (owner_id, "F7 allow-negative test"),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.accounts (id, owner_id, name, allow_negative)
                    VALUES (%s, %s, %s, TRUE), (%s, %s, %s, FALSE)
                    """,
                    (
                        source_id,
                        owner_id,
                        "F7 negative source",
                        destination_id,
                        owner_id,
                        "F7 destination",
                    ),
                )

            with pytest.raises(InsufficientFundsError, match="Insufficient funds"):
                transfer(connection, source_id, destination_id, 1, owner_id)

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
                assert row[0] == 0

    finally:
        with (
            psycopg.connect(test_admin_database_url) as cleanup,
            cleanup.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM ledger.accounts WHERE owner_id = %s",
                (owner_id,),
            )
            cursor.execute(
                "DELETE FROM ledger.owners WHERE id = %s",
                (owner_id,),
            )
