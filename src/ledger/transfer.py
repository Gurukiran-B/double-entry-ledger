
"""Transfer money safely using an immutable double-entry ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg


class InsufficientFundsError(ValueError):
    """Raised when an account cannot cover a transfer."""


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different request."""


def _validate_amount_minor(amount_minor: int) -> None:
    """Accept only positive integer minor currency units."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError(
            "Transfer amount must be an integer number of minor currency units."
        )

    if amount_minor <= 0:
        raise ValueError("Transfer amount must be positive.")


def _lock_accounts(
    connection: psycopg.Connection,
    account_ids: list[UUID],
) -> None:
    """Lock accounts in a consistent order to reduce deadlocks."""
    for account_id in sorted(set(account_ids)):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ledger.lock_account_for_update(%s)",
                (account_id,),
            )


def _get_balance(
    connection: psycopg.Connection,
    account_id: UUID,
) -> int:
    """Return an account's current balance in minor currency units."""
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
        raise RuntimeError("Could not retrieve account balance.")

    return int(row[0])


def get_balance(
    connection: psycopg.Connection,
    account_id: UUID,
) -> int:
    """Return an account's current balance in minor currency units."""
    return _get_balance(connection, account_id)


def _assert_source_owner(
    connection: psycopg.Connection,
    source_account_id: UUID,
    created_by: UUID,
) -> None:
    """Prevent an owner from debiting another owner's account."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT owner_id
            FROM ledger.accounts
            WHERE id = %s
            """,
            (source_account_id,),
        )
        row = cursor.fetchone()

    if row is None or row[0] != created_by:
        raise PermissionError("Source account is not authorized for this owner.")


def _perform_transfer(
    connection: psycopg.Connection,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    created_by: UUID,
    after_lock: Callable[[], None] | None = None,
) -> UUID:
    """Perform ledger writes inside the caller's transaction.

    This helper does not commit or roll back.
    """
    _validate_amount_minor(amount_minor)

    if source_account_id == destination_account_id:
        raise ValueError("Source and destination accounts must be different.")

    _lock_accounts(connection, [source_account_id, destination_account_id])

    _assert_source_owner(connection, source_account_id, created_by)

    if after_lock is not None:
        after_lock()

    source_balance = _get_balance(connection, source_account_id)
    if source_balance < amount_minor:
        raise InsufficientFundsError("Insufficient funds.")

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

    return transaction_id


def transfer(
    connection: psycopg.Connection,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    created_by: UUID,
    after_lock: Callable[[], None] | None = None,
) -> UUID:
    """Transfer funds atomically and return the transaction ID."""
    # Validate before changing transaction state or querying the database.
    _validate_amount_minor(amount_minor)

    if source_account_id == destination_account_id:
        raise ValueError("Source and destination accounts must be different.")

    original_autocommit = connection.autocommit
    if original_autocommit:
        connection.autocommit = False

    try:
        transaction_id = _perform_transfer(
            connection,
            source_account_id,
            destination_account_id,
            amount_minor,
            created_by,
            after_lock,
        )
        connection.commit()
        return transaction_id
    except Exception:
        connection.rollback()
        raise
    finally:
        if original_autocommit:
            connection.autocommit = True


def transfer_idempotently(
    connection: psycopg.Connection,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    created_by: UUID,
    idempotency_key: str,
    after_lock: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute a transfer once per owner/key pair and replay its saved result.

    The idempotency claim, ledger transaction, and saved response share one
    commit. Reusing a key for a different request raises
    IdempotencyConflictError.
    """
    # Validate before hashing, inserting an idempotency key, or querying.
    _validate_amount_minor(amount_minor)

    if not idempotency_key or len(idempotency_key) > 255:
        raise ValueError("Idempotency key must contain 1–255 characters.")

    if source_account_id == destination_account_id:
        raise ValueError("Source and destination accounts must be different.")

    request_data = {
        "source_account_id": str(source_account_id),
        "destination_account_id": str(destination_account_id),
        "amount_minor": amount_minor,
    }
    request_hash = hashlib.sha256(
        json.dumps(
            request_data,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()

    original_autocommit = connection.autocommit
    if original_autocommit:
        connection.autocommit = False

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ledger.idempotency_keys (
                    owner_id,
                    key,
                    request_hash
                )
                VALUES (%s, %s, %s)
                ON CONFLICT (owner_id, key) DO NOTHING
                RETURNING key
                """,
                (created_by, idempotency_key, request_hash),
            )
            claimed = cursor.fetchone() is not None

        if not claimed:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT request_hash, response_status, response_body
                    FROM ledger.idempotency_keys
                    WHERE owner_id = %s AND key = %s
                    FOR UPDATE
                    """,
                    (created_by, idempotency_key),
                )
                existing = cursor.fetchone()

            if existing is None:
                raise RuntimeError("Idempotency record disappeared.")

            saved_hash, saved_status, saved_body = existing

            if saved_hash != request_hash:
                raise IdempotencyConflictError(
                    "Idempotency key was already used for a different request."
                )

            if saved_status is None or saved_body is None:
                raise RuntimeError("Idempotency record has no saved response.")

            connection.commit()
            return {
                "status": saved_status,
                "body": saved_body,
                "replayed": True,
            }

        transaction_id = _perform_transfer(
            connection,
            source_account_id,
            destination_account_id,
            amount_minor,
            created_by,
            after_lock,
        )

        response_body = {"transaction_id": str(transaction_id)}
        response_status = 201

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ledger.idempotency_keys
                SET transaction_id = %s,
                    response_status = %s,
                    response_body = %s
                WHERE owner_id = %s AND key = %s
                """,
                (
                    transaction_id,
                    response_status,
                    json.dumps(response_body),
                    created_by,
                    idempotency_key,
                ),
            )

        connection.commit()
        return {
            "status": response_status,
            "body": response_body,
            "replayed": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        if original_autocommit:
            connection.autocommit = True
