"""F1: Demonstrate the overdraft race in the naive ledger implementation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import psycopg

from experiments.naive_app.transfer import (
    InsufficientFundsError,
    get_balance,
    transfer,
)
from ledger.config import DATABASE_URL


def create_owner(connection: psycopg.Connection) -> UUID:
    """Create an isolated owner for the experiment."""

    owner_id = uuid4()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.owners (id, name)
            VALUES (%s, %s)
            """,
            (owner_id, f"f01-{owner_id}"),
        )

    connection.commit()

    return owner_id


def create_account(
    connection: psycopg.Connection,
    owner_id: UUID,
    *,
    allow_negative: bool = False,
) -> UUID:
    """Create an account owned by the supplied owner."""

    account_id = uuid4()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.accounts (id, owner_id, name, allow_negative)
            VALUES (%s, %s, %s, %s)
            """,
            (account_id, owner_id, f"experiment-{account_id}", allow_negative),
        )

    connection.commit()

    return account_id


def seed_balance(
    connection: psycopg.Connection,
    account_id: UUID,
    owner_id: UUID,
    amount_minor: int,
    funding_account_id: UUID,
) -> None:
    """Seed an account through a balanced two-entry ledger transaction.

    Requires a funding account with allow_negative=TRUE.
    """

    transaction_id = uuid4()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.transactions (id, created_by)
            VALUES (%s, %s)
            """,
            (transaction_id, owner_id),
        )

        cursor.execute(
            """
            INSERT INTO ledger.entries (
                transaction_id,
                account_id,
                amount_minor
            )
            VALUES (%s, %s, %s), (%s, %s, %s)
            """,
            (
                transaction_id, funding_account_id, -amount_minor,
                transaction_id, account_id, amount_minor,
            ),
        )

    connection.commit()


def test_two_concurrent_transfers_can_overdraw_naive_ledger() -> None:
    """
    Prove F1 by forcing both transfers to observe the same balance.

    Alice starts with ₹100. Two concurrent requests each transfer ₹80.
    Both requests pass the balance check because they observe ₹100
    before either request writes its debit.

    Expected naive result:

        Alice   = -₹60
        Bob     =  ₹80
        Charlie =  ₹80
    """

    setup_connection = psycopg.connect(DATABASE_URL)

    try:
        owner_id = create_owner(setup_connection)

        alice_id = create_account(setup_connection, owner_id)
        bob_id = create_account(setup_connection, owner_id)
        charlie_id = create_account(setup_connection, owner_id)
        funding_id = create_account(setup_connection, owner_id, allow_negative=True)

        seed_balance(
            setup_connection,
            alice_id,
            owner_id,
            10_000,
            funding_id,
        )
    finally:
        setup_connection.close()

    barrier = Barrier(2)

    def run_transfer(destination_id: UUID) -> str:
        """Run one transfer using an independent database connection."""

        connection = psycopg.connect(DATABASE_URL)

        try:

            def after_balance_check(balance: int) -> None:
                assert balance == 10_000
                barrier.wait(timeout=10)

            transfer(
                connection=connection,
                source_account_id=alice_id,
                destination_account_id=destination_id,
                amount_minor=8_000,
                created_by=owner_id,
                after_balance_check=after_balance_check,
            )

            return "success"

        except InsufficientFundsError:
            return "insufficient_funds"

        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(run_transfer, bob_id),
            executor.submit(run_transfer, charlie_id),
        ]

        results = [future.result(timeout=15) for future in futures]

    verification_connection = psycopg.connect(DATABASE_URL)

    try:
        alice_balance = get_balance(verification_connection, alice_id)
        bob_balance = get_balance(verification_connection, bob_id)
        charlie_balance = get_balance(verification_connection, charlie_id)
    finally:
        verification_connection.close()

    assert results == ["success", "success"]

    assert alice_balance == -6_000
    assert bob_balance == 8_000
    assert charlie_balance == 8_000