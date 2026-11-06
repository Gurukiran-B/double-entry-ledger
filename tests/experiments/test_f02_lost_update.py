"""F2: Demonstrate a classic lost-update race."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import psycopg

from ledger.config import DATABASE_URL


def _setup_experiment_table(connection: psycopg.Connection) -> None:
    """Create the mutable-balance table used only by the F2 experiment."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DROP TABLE IF EXISTS ledger.f02_experiment_balances
            """
        )
        cursor.execute(
            """
            CREATE TABLE ledger.f02_experiment_balances (
                account_id UUID PRIMARY KEY,
                balance_minor BIGINT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            GRANT SELECT, INSERT, UPDATE
            ON ledger.f02_experiment_balances TO ledger_app
            """
        )

    connection.commit()


def _drop_experiment_table(connection: psycopg.Connection) -> None:
    """Remove the F2 experiment table."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            DROP TABLE IF EXISTS ledger.f02_experiment_balances
            """
        )

    connection.commit()


def _seed_account(
    connection: psycopg.Connection,
    account_id: UUID,
    balance_minor: int,
) -> None:
    """Seed one mutable balance row for the experiment."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.f02_experiment_balances (
                account_id,
                balance_minor
            )
            VALUES (%s, %s)
            """,
            (account_id, balance_minor),
        )

    connection.commit()


def _run_vulnerable_update(
    connection: psycopg.Connection,
    account_id: UUID,
    decrement_minor: int,
    after_read: Barrier,
    update_turn: Event | None = None,
    update_complete: Event | None = None,
) -> None:
    """Perform an intentionally unsafe read-modify-write operation.

    Both transactions read the same balance before either transaction updates
    the row. The events then force transaction A to update first and
    transaction B to update second.
    """
    connection.autocommit = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT balance_minor
                FROM ledger.f02_experiment_balances
                WHERE account_id = %s
                """,
                (account_id,),
            )

            row = cursor.fetchone()

            if row is None:
                raise RuntimeError("F2 experiment account was not found.")

            balance_minor = int(row[0])

            # Both transactions must finish their read first.
            after_read.wait(timeout=10)

            # Transaction B waits until transaction A has performed its
            # UPDATE. Transaction A has no update_turn and proceeds directly.
            if update_turn is not None and not update_turn.wait(timeout=10):
                raise TimeoutError(
                    "Timed out waiting for the first transaction to update."
                )

            new_balance_minor = balance_minor - decrement_minor

            cursor.execute(
                """
                UPDATE ledger.f02_experiment_balances
                SET balance_minor = %s
                WHERE account_id = %s
                """,
                (new_balance_minor, account_id),
            )

            # Transaction A signals B only after its UPDATE has completed.
            if update_complete is not None:
                update_complete.set()

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.autocommit = True


def test_mutable_balance_can_suffer_lost_update(
    test_admin_database_url: str,
) -> None:
    """Prove F2 using an intentionally vulnerable mutable-balance model.

    Initial balance:

        10_000

    Transaction A:

        10_000 - 1_000 = 9_000

    Transaction B:

        10_000 - 2_000 = 8_000

    The mathematically correct result is 7_000.

    Both transactions read 10_000 before either writes. Transaction A is then
    forced to write 9_000 first, followed by transaction B writing its stale
    calculation of 8_000. B therefore overwrites A's update.
    """
    with psycopg.connect(test_admin_database_url) as admin_connection:
        _setup_experiment_table(admin_connection)

    setup_connection = psycopg.connect(DATABASE_URL)

    try:
        account_id = uuid4()

        _seed_account(
            setup_connection,
            account_id,
            10_000,
        )

        after_read = Barrier(2)
        transaction_a_updated = Event()

        def run_transaction_a() -> None:
            connection = psycopg.connect(DATABASE_URL)

            try:
                _run_vulnerable_update(
                    connection=connection,
                    account_id=account_id,
                    decrement_minor=1_000,
                    after_read=after_read,
                    update_complete=transaction_a_updated,
                )
            finally:
                connection.close()

        def run_transaction_b() -> None:
            connection = psycopg.connect(DATABASE_URL)

            try:
                _run_vulnerable_update(
                    connection=connection,
                    account_id=account_id,
                    decrement_minor=2_000,
                    after_read=after_read,
                    update_turn=transaction_a_updated,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(run_transaction_a)
            future_b = executor.submit(run_transaction_b)

            future_a.result(timeout=15)
            future_b.result(timeout=15)

        with setup_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT balance_minor
                FROM ledger.f02_experiment_balances
                WHERE account_id = %s
                """,
                (account_id,),
            )

            row = cursor.fetchone()

            if row is None:
                raise RuntimeError("F2 experiment account disappeared.")

            final_balance = int(row[0])

        mathematically_correct_balance = 10_000 - 1_000 - 2_000

        assert mathematically_correct_balance == 7_000
        assert final_balance == 8_000
        assert final_balance != mathematically_correct_balance

    finally:
        setup_connection.close()
        with psycopg.connect(test_admin_database_url) as admin_connection:
            _drop_experiment_table(admin_connection)