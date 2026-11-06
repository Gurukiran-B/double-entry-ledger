
"""F4: concurrent requests using the same idempotency key."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import Any
from uuid import uuid4

import psycopg

from ledger.config import DATABASE_URL
from ledger.transfer import transfer_idempotently


def test_concurrent_same_key_creates_only_one_transfer(
    test_admin_database_url: str,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    destination_id = uuid4()
    system_id = uuid4()
    funding_transaction_id = uuid4()
    idempotency_key = f"f4-{uuid4()}"

    # Create isolated test data.
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO ledger.owners (id, name) VALUES (%s, %s)",
            (owner_id, f"F4 test owner {owner_id}"),
        )
        cursor.execute(
            """
            INSERT INTO ledger.accounts (id, owner_id, name, allow_negative)
            VALUES
                (%s, %s, %s, FALSE),
                (%s, %s, %s, FALSE),
                (%s, %s, %s, TRUE)
            """,
            (
                source_id, owner_id, "F4 source",
                destination_id, owner_id, "F4 destination",
                system_id, owner_id, "F4 funding",
            ),
        )
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
            VALUES
                (%s, %s, %s),
                (%s, %s, %s)
            """,
            (
                funding_transaction_id, system_id, -1000,
                funding_transaction_id, source_id, 1000,
            ),
        )

    a_reached_after_lock = Event()
    release_a = Event()
    b_can_start = Event()

    pid_a: list[int] = []
    pid_b: list[int] = []

    def after_lock_callback() -> None:
        """Pause Request A after claiming the idempotency key and locking accounts."""
        a_reached_after_lock.set()
        if not release_a.wait(timeout=10.0):
            raise TimeoutError(
                "Request A timed out waiting to be released from after_lock."
            )

    def submit_request_a() -> dict[str, Any]:
        with psycopg.connect(DATABASE_URL) as connection:
            pid_a.append(connection.pgconn.backend_pid)
            return transfer_idempotently(
                connection,
                source_id,
                destination_id,
                500,
                owner_id,
                idempotency_key,
                after_lock=after_lock_callback,
            )

    def submit_request_b() -> dict[str, Any]:
        if not b_can_start.wait(timeout=10.0):
            raise TimeoutError("Request B timed out waiting for start signal.")
        with psycopg.connect(DATABASE_URL) as connection:
            pid_b.append(connection.pgconn.backend_pid)
            return transfer_idempotently(
                connection,
                source_id,
                destination_id,
                500,
                owner_id,
                idempotency_key,
            )

    executor = ThreadPoolExecutor(max_workers=2)
    future_a: Future[dict[str, Any]] | None = None
    future_b: Future[dict[str, Any]] | None = None

    try:
        # 1. Start Request A, which claims the key and pauses at after_lock.
        future_a = executor.submit(submit_request_a)
        assert a_reached_after_lock.wait(timeout=10.0), (
            "Request A did not reach after_lock hook within timeout."
        )
        assert len(pid_a) == 1, "Request A backend PID was not captured."
        backend_pid_a = pid_a[0]

        # 2. Start Request B while Request A is still uncommitted.
        future_b = executor.submit(submit_request_b)
        b_can_start.set()

        # 3. Verify that Request B is blocked by Request A.
        deadline = time.monotonic() + 10.0
        b_blocked = False
        blocking_observation: dict[str, Any] = {}

        with (
            psycopg.connect(test_admin_database_url) as mon_connection,
            mon_connection.cursor() as mon_cursor,
        ):
            while time.monotonic() < deadline:
                if pid_b:
                    backend_pid_b = pid_b[0]
                    mon_cursor.execute(
                        """
                        SELECT
                            pid,
                            wait_event_type,
                            wait_event,
                            state,
                            query,
                            pg_blocking_pids(pid)
                        FROM pg_stat_activity
                        WHERE pid = %s
                        """,
                        (backend_pid_b,),
                    )
                    row = mon_cursor.fetchone()
                    if row is not None:
                        (
                            b_pid,
                            wait_type,
                            wait_event,
                            state,
                            query,
                            blocking_pids,
                        ) = row
                        blocking_observation = {
                            "pid": b_pid,
                            "wait_event_type": wait_type,
                            "wait_event": wait_event,
                            "state": state,
                            "query": query,
                            "blocking_pids": blocking_pids,
                        }
                        if blocking_pids and backend_pid_a in blocking_pids:
                            b_blocked = True
                            break
                time.sleep(0.01)

        assert b_blocked, (
            f"Request B (PID {pid_b}) was not observed blocked by Request A "
            f"(PID {backend_pid_a}) within deadline. "
            f"Observation: {blocking_observation}"
        )
        assert blocking_observation.get("wait_event_type") == "Lock"
        assert "INSERT INTO ledger.idempotency_keys" in str(
            blocking_observation.get("query")
        )

        # 4. Release Request A and allow both requests to finish.
        release_a.set()
        result_a = future_a.result(timeout=10.0)
        result_b = future_b.result(timeout=10.0)

        # 5. Verify results and ledger state.
        transaction_id_a = result_a["body"]["transaction_id"]
        transaction_id_b = result_b["body"]["transaction_id"]

        assert transaction_id_a == transaction_id_b
        assert result_a["status"] == 201
        assert result_b["status"] == 201

        # Exactly one request is a replay.
        assert result_a["replayed"] is False
        assert result_b["replayed"] is True

        with (
            psycopg.connect(DATABASE_URL) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT COUNT(*) FROM ledger.transactions WHERE id = %s",
                (transaction_id_a,),
            )
            tx_row = cursor.fetchone()
            assert tx_row is not None
            assert tx_row[0] == 1

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM ledger.transactions
                WHERE created_by = %s
                """,
                (owner_id,),
            )
            total_tx_row = cursor.fetchone()
            assert total_tx_row is not None
            assert total_tx_row[0] == 2

            # Exactly two transfer entries exist.
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM ledger.entries
                WHERE transaction_id = %s
                """,
                (transaction_id_a,),
            )
            entries_row = cursor.fetchone()
            assert entries_row is not None
            assert entries_row[0] == 2

            # Verify source, destination, and funding balances.
            cursor.execute(
                """
                SELECT
                    (
                        SELECT COALESCE(SUM(amount_minor), 0)
                        FROM ledger.entries WHERE account_id = %s
                    ),
                    (
                        SELECT COALESCE(SUM(amount_minor), 0)
                        FROM ledger.entries WHERE account_id = %s
                    ),
                    (
                        SELECT COALESCE(SUM(amount_minor), 0)
                        FROM ledger.entries WHERE account_id = %s
                    )
                """,
                (source_id, destination_id, system_id),
            )
            balance_row = cursor.fetchone()
            assert balance_row is not None
            assert balance_row == (500, 500, -1000)

            # Exactly one idempotency record exists.
            cursor.execute(
                """
                SELECT COUNT(*), transaction_id, response_status
                FROM ledger.idempotency_keys
                WHERE owner_id = %s AND key = %s
                GROUP BY transaction_id, response_status
                """,
                (owner_id, idempotency_key),
            )
            idemp_row = cursor.fetchone()
            assert idemp_row is not None
            assert idemp_row[0] == 1
            assert str(idemp_row[1]) == transaction_id_a
            assert idemp_row[2] == 201

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM ledger.idempotency_keys
                WHERE owner_id = %s
                """,
                (owner_id,),
            )
            total_idemp_row = cursor.fetchone()
            assert total_idemp_row is not None
            assert total_idemp_row[0] == 1

    finally:
        # Unblock workers before cleanup.
        release_a.set()
        b_can_start.set()

        # Wait for workers before deleting their database records.
        for future in (future_a, future_b):
            if future is not None:
                future.exception(timeout=10.0)

        executor.shutdown(wait=True)

        # Test-only cleanup: use an isolated database without concurrent writes.
        with psycopg.connect(test_admin_database_url) as cleanup_conn:
            try:
                with cleanup_conn.cursor() as cleanup_cursor:
                    cleanup_cursor.execute(
                        """
                        DELETE FROM ledger.idempotency_keys
                        WHERE owner_id = %s
                        """,
                        (owner_id,),
                    )

                    # Trigger disabling is table-wide, not row-specific.
                    cleanup_cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "DISABLE TRIGGER entries_immutable"
                    )

                    cleanup_cursor.execute(
                        """
                        DELETE FROM ledger.entries
                        WHERE account_id = ANY(%s)
                        """,
                        ([source_id, destination_id, system_id],),
                    )

                    cleanup_cursor.execute(
                        """
                        DELETE FROM ledger.transactions
                        WHERE created_by = %s
                        """,
                        (owner_id,),
                    )

                    cleanup_cursor.execute(
                        """
                        DELETE FROM ledger.accounts
                        WHERE owner_id = %s
                        """,
                        (owner_id,),
                    )

                    cleanup_cursor.execute(
                        "ALTER TABLE ledger.entries "
                        "ENABLE TRIGGER entries_immutable"
                    )

                    cleanup_cursor.execute(
                        "DELETE FROM ledger.owners WHERE id = %s",
                        (owner_id,),
                    )

                cleanup_conn.commit()
            except Exception:
                cleanup_conn.rollback()
                raise
