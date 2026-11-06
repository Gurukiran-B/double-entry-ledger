
"""F10: Existing ledger entries cannot be updated or deleted."""

from uuid import uuid4

import psycopg
import pytest


def test_ledger_entries_cannot_be_updated_or_deleted(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    account_id = uuid4()
    transaction_id = uuid4()

    with psycopg.connect(test_admin_database_url) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ledger.owners (id, name)
                    VALUES (%s, %s)
                    """,
                    (owner_id, "F10 test owner"),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.accounts (id, owner_id, name)
                    VALUES (%s, %s, %s)
                    """,
                    (account_id, owner_id, "F10 test account"),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.transactions (id, created_by)
                    VALUES (%s, %s)
                    """,
                    (transaction_id, owner_id),
                )
                cursor.execute(
                    """
                    INSERT INTO ledger.entries
                        (transaction_id, account_id, amount_minor)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (transaction_id, account_id, 500),
                )
                row = cursor.fetchone()
                assert row is not None
                entry_id = row[0]

            # UPDATE must fail without changing the original entry.
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT before_update")
                with pytest.raises(
                    psycopg.Error,
                    match="Ledger entries are immutable",
                ):
                    cursor.execute(
                        """
                        UPDATE ledger.entries
                        SET amount_minor = 999
                        WHERE id = %s
                        """,
                        (entry_id,),
                    )
                cursor.execute("ROLLBACK TO SAVEPOINT before_update")
                cursor.execute("RELEASE SAVEPOINT before_update")

            # DELETE must fail as well.
            with connection.cursor() as cursor:
                cursor.execute("SAVEPOINT before_delete")
                with pytest.raises(
                    psycopg.Error,
                    match="Ledger entries are immutable",
                ):
                    cursor.execute(
                        "DELETE FROM ledger.entries WHERE id = %s",
                        (entry_id,),
                    )
                cursor.execute("ROLLBACK TO SAVEPOINT before_delete")
                cursor.execute("RELEASE SAVEPOINT before_delete")

            # Verify that the original entry still exists unchanged.
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT amount_minor
                    FROM ledger.entries
                    WHERE id = %s
                    """,
                    (entry_id,),
                )
                row = cursor.fetchone()

            assert row is not None
            assert row[0] == 500

        finally:
            # All fixtures were created in this uncommitted transaction.
            # Rollback cleans them up without deleting protected history.
            connection.rollback()
