
"""F3: Demonstrate duplicate execution after a retry."""

from uuid import uuid4

import psycopg

from experiments.f03_retry_double_debit.transfer import (
    transfer_without_idempotency,
)
from ledger.config import DATABASE_URL


def test_retry_without_idempotency_executes_transfer_twice(
    test_admin_database_url: str,
) -> None:
    source_id = str(uuid4())
    destination_id = str(uuid4())

    with (
        psycopg.connect(test_admin_database_url) as setup,
        setup.cursor() as cursor,
    ):
        cursor.execute("DROP TABLE IF EXISTS public.f03_experiment_transactions")
        cursor.execute("DROP TABLE IF EXISTS public.f03_experiment_accounts")
        cursor.execute(
            """
                CREATE TABLE public.f03_experiment_accounts (
                    account_id TEXT PRIMARY KEY,
                    balance_minor BIGINT NOT NULL
                        CHECK (balance_minor >= 0)
                )
            """
        )
        cursor.execute(
            """
                CREATE TABLE public.f03_experiment_transactions (
                    id TEXT PRIMARY KEY
                )
            """
        )
        cursor.execute(
            """
                GRANT SELECT, INSERT, UPDATE, DELETE
                ON public.f03_experiment_accounts,
                   public.f03_experiment_transactions
                TO ledger_app
            """
        )

    with psycopg.connect(DATABASE_URL) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO f03_experiment_accounts
                        (account_id, balance_minor)
                    VALUES (%s, %s), (%s, %s)
                    """,
                    (source_id, 1000, destination_id, 0),
                )

            connection.commit()

            # Request 1 commits successfully. Imagine its response is lost.
            first_transaction = transfer_without_idempotency(
                connection, source_id, destination_id, 500
            )

            # Request 2 retries the same logical operation.
            second_transaction = transfer_without_idempotency(
                connection, source_id, destination_id, 500
            )

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT account_id, balance_minor
                    FROM f03_experiment_accounts
                    ORDER BY account_id
                    """
                )
                balances: dict[str, int] = dict(cursor.fetchall())

                cursor.execute(
                    "SELECT COUNT(*) FROM f03_experiment_transactions"
                )
                row = cursor.fetchone()
                assert row is not None
                transaction_count = row[0]

            assert first_transaction != second_transaction
            assert balances[source_id] == 0
            assert balances[destination_id] == 1000
            assert transaction_count == 2

        finally:
            connection.rollback()
            with (
                psycopg.connect(test_admin_database_url) as cleanup,
                cleanup.cursor() as cursor,
            ):
                cursor.execute(
                    "DROP TABLE IF EXISTS public.f03_experiment_transactions"
                )
                cursor.execute(
                    "DROP TABLE IF EXISTS public.f03_experiment_accounts"
                )
