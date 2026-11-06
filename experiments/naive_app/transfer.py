"""Intentionally vulnerable ledger implementation for the F1 race experiment."""

from __future__ import annotations

import os
from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg

DATABASE_URL = os.getenv("LEDGER_DATABASE_URL", "host=localhost dbname=postgres")


BalanceCheckHook = Callable[[int], None]


class InsufficientFundsError(Exception):
    """Raised when the source account cannot cover the transfer."""


def get_balance(
    connection: psycopg.Connection,
    account_id: UUID,
) -> int:
    """Return an account balance in minor currency units."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount_minor), 0)
            FROM ledger.entries
            WHERE account_id = %s
            """,
            (account_id,),
        )

        row = cursor.fetchone()

    if row is None:
        raise RuntimeError("Balance query returned no result.")

    return int(row[0])


def transfer(
    connection: psycopg.Connection,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    created_by: UUID,
    after_balance_check: BalanceCheckHook | None = None,
) -> None:
    """
    Perform a deliberately naive transfer.

    WARNING:
        This implementation is intentionally vulnerable to F1:
        the overdraft race.

    The vulnerable sequence is:

        1. Read the source balance.
        2. Check whether sufficient funds exist.
        3. Invoke the experiment hook.
        4. Write the debit.
        5. Write the credit.

    There is deliberately no database row lock protecting the
    balance check.
    """

    if amount_minor <= 0:
        raise ValueError("Transfer amount must be positive.")

    if source_account_id == destination_account_id:
        raise ValueError("Source and destination accounts must differ.")

    balance = get_balance(connection, source_account_id)

    if balance < amount_minor:
        raise InsufficientFundsError("Insufficient funds.")

    if after_balance_check is not None:
        after_balance_check(balance)

    transaction_id = uuid4()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.transactions (id, created_by)
            VALUES (%s, %s)
            """,
            (transaction_id, created_by),
        )

        cursor.execute(
            """
            INSERT INTO ledger.entries (
                transaction_id,
                account_id,
                amount_minor
            )
            VALUES (%s, %s, %s)
            """,
            (transaction_id, source_account_id, -amount_minor),
        )

        cursor.execute(
            """
            INSERT INTO ledger.entries (
                transaction_id,
                account_id,
                amount_minor
            )
            VALUES (%s, %s, %s)
            """,
            (transaction_id, destination_account_id, amount_minor),
        )

    connection.commit()
