
"""F5: Verify that a failed transfer leaves no partial ledger writes."""

from uuid import uuid4

import psycopg
import pytest

from ledger.config import DATABASE_URL
from ledger.transfer import get_balance, transfer


def test_failed_transfer_rolls_back_all_writes(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    destination_id = uuid4()
    system_id = uuid4()
    funding_transaction_id = uuid4()
    trigger_name = f"f5_reject_credit_{uuid4().hex}"
    function_name = f"f5_reject_credit_fn_{uuid4().hex}"

    with psycopg.connect(DATABASE_URL) as connection:
        try:
            # 1. Create the test owner and accounts.
            with (
                psycopg.connect(test_admin_database_url) as setup_connection,
                setup_connection.cursor() as cursor,
            ):
                cursor.execute(
                    """
                        INSERT INTO ledger.owners (id, name)
                        VALUES (%s, %s)
                        """,
                        (owner_id, "F5 atomicity test"),
                )
                cursor.execute(
                    """
                        INSERT INTO ledger.accounts (id, owner_id, name)
                        VALUES (%s, %s, %s), (%s, %s, %s)
                        """,
                        (
                            source_id, owner_id, "F5 source",
                            destination_id, owner_id, "F5 destination",
                        ),
                )
                cursor.execute(
                    """
                        INSERT INTO ledger.accounts
                            (id, owner_id, name, allow_negative)
                        VALUES (%s, %s, %s, TRUE)
                        """,
                        (system_id, owner_id, "F5 funding account"),
                )

            # 2. Fund the source account with 1,000 units.
            with connection.cursor() as cursor:
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
                        funding_transaction_id, system_id, -1000,
                        funding_transaction_id, source_id, 1000,
                    ),
                )

            connection.commit()

            assert get_balance(connection, source_id) == 1000
            assert get_balance(connection, destination_id) == 0

            # 3. Install a trigger that rejects the destination credit.
            with (
                psycopg.connect(test_admin_database_url) as setup_connection,
                setup_connection.cursor() as cursor,
            ):
                cursor.execute(
                    f"""
                        CREATE FUNCTION ledger.{function_name}()
                        RETURNS trigger
                        LANGUAGE plpgsql
                        AS $$
                        BEGIN
                            IF NEW.account_id = '{destination_id}'
                               AND NEW.amount_minor > 0 THEN
                                RAISE EXCEPTION 'F5 deliberate credit failure';
                            END IF;
                            RETURN NEW;
                        END;
                        $$
                        """
                )
                cursor.execute(
                    f"""
                        CREATE TRIGGER {trigger_name}
                        BEFORE INSERT ON ledger.entries
                        FOR EACH ROW
                        EXECUTE FUNCTION ledger.{function_name}()
                        """
                )

            # 4. The transfer must fail while inserting the destination credit.
            with pytest.raises(
                psycopg.Error,
                match="F5 deliberate credit failure",
            ):
                transfer(
                    connection,
                    source_id,
                    destination_id,
                    200,
                    owner_id,
                )

            # 5. Verify using a fresh connection that no partial transfer survived.
            with psycopg.connect(DATABASE_URL) as verification:
                assert get_balance(verification, source_id) == 1000
                assert get_balance(verification, destination_id) == 0

                with verification.cursor() as cursor:
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
                    entry_count = row[0]

                # One funding transaction and one source funding entry remain
                # among this owner's transactions and these two accounts.
                assert transaction_count == 1
                assert entry_count == 1

        finally:
            # Roll back any failed application transaction first.
            connection.rollback()

            # Use a separate cleanup transaction for this test's unique fixtures.
            # Run only against an isolated test database without concurrent writes.
            with psycopg.connect(test_admin_database_url) as cleanup_conn:
                try:
                    with cleanup_conn.cursor() as cursor:
                        # Remove the test's deliberately installed trigger/function.
                        cursor.execute(
                            f"DROP TRIGGER IF EXISTS {trigger_name} "
                            "ON ledger.entries"
                        )
                        cursor.execute(
                            f"DROP FUNCTION IF EXISTS "
                            f"ledger.{function_name}()"
                        )

                        cursor.execute(
                            """
                            DELETE FROM ledger.idempotency_keys
                            WHERE owner_id = %s
                            """,
                            (owner_id,),
                        )

                        # Trigger disabling is table-wide; test-only cleanup.
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
                    cleanup_conn.rollback()
                    raise
