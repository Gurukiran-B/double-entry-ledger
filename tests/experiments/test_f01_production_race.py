"""Production regression test for F1 – ensure concurrent transfers are safe."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import psycopg

from ledger.config import DATABASE_URL
from ledger.transfer import (
    InsufficientFundsError,
    get_balance,
    transfer,
)

# Helper functions – copied from the naive experiment but using the same schema.

def create_owner(connection: psycopg.Connection) -> UUID:
    owner_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.owners (id, name)
            VALUES (%s, %s)
            """,
            (owner_id, f"prod-f01-{owner_id}"),
        )
    connection.commit()
    return owner_id


def create_account(
    connection: psycopg.Connection,
    owner_id: UUID,
    *,
    allow_negative: bool = False,
) -> UUID:
    account_id = uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ledger.accounts (id, owner_id, name, allow_negative)
            VALUES (%s, %s, %s, %s)
            """,
            (account_id, owner_id, f"prod-{account_id}", allow_negative),
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


def test_concurrent_transfers_prevent_overdraft() -> None:
    # Setup owners and accounts.
    setup_conn = psycopg.connect(DATABASE_URL)
    try:
        owner_id = create_owner(setup_conn)
        alice = create_account(setup_conn, owner_id)
        bob = create_account(setup_conn, owner_id)
        charlie = create_account(setup_conn, owner_id)
        funding = create_account(setup_conn, owner_id, allow_negative=True)
        # Alice starts with 10 000 minor units.
        seed_balance(setup_conn, alice, owner_id, 10_000, funding)
    finally:
        setup_conn.close()

    first_lock_acquired = Event()
    release_first = Event()
    second_connection_ready = Event()
    second_lock_acquired = Event()
    poll_wait = Event()
    second_backend_pid: int | None = None

    def hold_first_after_lock() -> None:
        first_lock_acquired.set()
        if not release_first.wait(timeout=30):
            raise TimeoutError("Timed out waiting to release the first transfer.")

    def record_second_lock() -> None:
        second_lock_acquired.set()

    def run_transfer(
        dst: UUID,
        after_lock: Callable[[], None],
        *,
        signal_connection_ready: bool = False,
    ) -> str:
        nonlocal second_backend_pid
        conn = psycopg.connect(DATABASE_URL)
        try:
            if signal_connection_ready:
                second_backend_pid = conn.info.backend_pid
                second_connection_ready.set()

            transfer(
                connection=conn,
                source_account_id=alice,
                destination_account_id=dst,
                amount_minor=8_000,
                created_by=owner_id,
                after_lock=after_lock,
            )
            return "success"
        except InsufficientFundsError:
            return "insufficient"
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_transfer, bob, hold_first_after_lock)
        try:
            assert first_lock_acquired.wait(timeout=10)
            second_future = executor.submit(
                run_transfer,
                charlie,
                record_second_lock,
                signal_connection_ready=True,
            )
            assert second_connection_ready.wait(timeout=10)
            assert second_backend_pid is not None

            monitor_conn = psycopg.connect(DATABASE_URL)
            try:
                deadline = monotonic() + 10
                second_is_blocked = False
                second_status: tuple[object, ...] | None = None
                while monotonic() < deadline:
                    with monitor_conn.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT
                                cardinality(pg_blocking_pids(%s)) > 0,
                                wait_event_type,
                                wait_event,
                                state
                            FROM pg_stat_activity
                            WHERE pid = %s
                            """,
                            (second_backend_pid, second_backend_pid),
                        )
                        row = cursor.fetchone()
                    if row is None:
                        second_error = (
                            second_future.exception()
                            if second_future.done()
                            else None
                        )
                        raise RuntimeError(
                            "Could not inspect the second transfer lock; "
                            f"worker error: {second_error!r}."
                        )
                    second_status = row[1:]
                    if row[0]:
                        second_is_blocked = True
                        break
                    poll_wait.wait(timeout=0.01)
                assert second_is_blocked, (
                    "Second transfer did not wait for the account lock; "
                    f"last observed session status: {second_status!r}."
                )
            finally:
                monitor_conn.close()
        finally:
            release_first.set()

        first_result = first_future.result(timeout=15)
        assert second_lock_acquired.wait(timeout=10)
        second_result = second_future.result(timeout=15)
        results = [first_result, second_result]

    # Exactly one transfer should succeed.
    assert results.count("success") == 1
    assert results.count("insufficient") == 1

    # Verify final balances.
    verify_conn = psycopg.connect(DATABASE_URL)
    try:
        alice_bal = get_balance(verify_conn, alice)
        bob_bal = get_balance(verify_conn, bob)
        charlie_bal = get_balance(verify_conn, charlie)
    finally:
        verify_conn.close()

    # Source balance must never be negative.
    assert alice_bal >= 0
    # Total transferred amount must be exactly 8 000.
    assert bob_bal + charlie_bal == 8_000
    # One of the destinations received the money, the other stayed at 0.
    assert (bob_bal == 8_000 and charlie_bal == 0) or (bob_bal == 0 and charlie_bal == 8_000)
