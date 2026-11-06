
"""Intentionally vulnerable transfer used to demonstrate F3."""

from uuid import uuid4

import psycopg


def transfer_without_idempotency(
    connection: psycopg.Connection,
    source_id: str,
    destination_id: str,
    amount_minor: int,
) -> str:
    """Transfer funds without recording a retry key.

    Each call creates a new transaction, even when it represents
    a retry of an operation that already committed.
    """
    if amount_minor <= 0:
        raise ValueError("Transfer amount must be positive.")

    transaction_id = str(uuid4())

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE f03_experiment_accounts
                SET balance_minor = balance_minor - %s
                WHERE account_id = %s
                  AND balance_minor >= %s
                """,
                (amount_minor, source_id, amount_minor),
            )

            if cursor.rowcount != 1:
                raise ValueError("Insufficient funds or missing source account.")

            cursor.execute(
                """
                UPDATE f03_experiment_accounts
                SET balance_minor = balance_minor + %s
                WHERE account_id = %s
                """,
                (amount_minor, destination_id),
            )

            if cursor.rowcount != 1:
                raise ValueError("Destination account does not exist.")

            cursor.execute(
                """
                INSERT INTO f03_experiment_transactions (id)
                VALUES (%s)
                """,
                (transaction_id,),
            )

        connection.commit()
        return transaction_id

    except Exception:
        connection.rollback()
        raise
