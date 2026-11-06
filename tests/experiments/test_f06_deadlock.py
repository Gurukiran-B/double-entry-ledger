"""F6: Verify PostgreSQL detects a genuine row-lock deadlock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg.errors import DeadlockDetected

from ledger.config import DATABASE_URL


def test_postgres_detects_deadlock(test_admin_database_url: str) -> None:
    owner_id = uuid4()
    account_a = uuid4()
    account_b = uuid4()

    with psycopg.connect(test_admin_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ledger.owners (id, name)
                VALUES (%s, %s)
                """,
                (owner_id, "F6 deadlock test"),
            )
            cursor.execute(
                """
                INSERT INTO ledger.accounts (id, owner_id, name)
                VALUES (%s, %s, %s), (%s, %s, %s)
                """,
                (
                    account_a, owner_id, "F6 account A",
                    account_b, owner_id, "F6 account B",
                ),
            )
        connection.commit()

    barrier = Barrier(2, timeout=10)

    def lock_in_opposite_order(first_account, second_account) -> str:
        with psycopg.connect(DATABASE_URL) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT ledger.lock_account_for_update(%s)",
                        (first_account,),
                    )

                barrier.wait()

                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT ledger.lock_account_for_update(%s)",
                        (second_account,),
                    )

                connection.commit()
                return "success"

            except DeadlockDetected:
                connection.rollback()
                return "deadlock"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(
                lock_in_opposite_order, account_a, account_b
            )
            future_b = executor.submit(
                lock_in_opposite_order, account_b, account_a
            )

            results = [
                future_a.result(timeout=20),
                future_b.result(timeout=20),
            ]

        assert results.count("deadlock") == 1, results
        assert results.count("success") == 1, results

    finally:
        with psycopg.connect(test_admin_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM ledger.accounts WHERE owner_id = %s",
                    (owner_id,),
                )
                cursor.execute(
                    "DELETE FROM ledger.owners WHERE id = %s",
                    (owner_id,),
                )
            connection.commit()
