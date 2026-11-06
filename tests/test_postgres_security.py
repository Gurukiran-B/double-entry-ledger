from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from ledger.config import DATABASE_URL


def test_runtime_role_has_least_privilege_and_can_lock_accounts() -> None:
    owner_id = uuid4()
    account_id = uuid4()

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    current_user,
                    role.rolsuper,
                    role.rolcreatedb,
                    role.rolcreaterole,
                    role.rolreplication,
                    role.rolbypassrls,
                    has_database_privilege(
                        current_user, current_database(), 'CONNECT'
                    ),
                    has_database_privilege(
                        current_user, current_database(), 'CREATE'
                    ),
                    has_database_privilege(
                        current_user, current_database(), 'TEMPORARY'
                    ),
                    has_schema_privilege(current_user, 'ledger', 'USAGE'),
                    has_table_privilege(current_user, 'ledger.owners', 'SELECT'),
                    has_table_privilege(current_user, 'ledger.owners', 'INSERT'),
                    has_table_privilege(current_user, 'ledger.accounts', 'SELECT'),
                    has_table_privilege(current_user, 'ledger.accounts', 'INSERT'),
                    has_table_privilege(current_user, 'ledger.transactions', 'SELECT'),
                    has_table_privilege(current_user, 'ledger.transactions', 'INSERT'),
                    has_table_privilege(current_user, 'ledger.entries', 'SELECT'),
                    has_table_privilege(current_user, 'ledger.entries', 'INSERT'),
                    has_table_privilege(current_user, 'ledger.idempotency_keys', 'SELECT'),
                    has_table_privilege(current_user, 'ledger.idempotency_keys', 'INSERT'),
                    has_table_privilege(current_user, 'ledger.idempotency_keys', 'UPDATE'),
                    has_table_privilege(current_user, 'ledger.entries', 'UPDATE'),
                    has_table_privilege(current_user, 'ledger.entries', 'DELETE'),
                    has_table_privilege(current_user, 'ledger.transactions', 'UPDATE'),
                    has_table_privilege(current_user, 'ledger.transactions', 'DELETE'),
                    has_table_privilege(current_user, 'ledger.accounts', 'UPDATE'),
                    has_column_privilege(current_user, 'ledger.accounts', 'id', 'UPDATE'),
                    has_sequence_privilege(
                        current_user, 'ledger.entries_id_seq', 'USAGE'
                    ),
                    has_sequence_privilege(
                        current_user, 'ledger.entries_id_seq', 'SELECT'
                    ),
                    pg_has_role(
                        current_user, 'ledger_account_locker', 'MEMBER'
                    ),
                    has_function_privilege(
                        current_user,
                        'ledger.lock_account_for_update(uuid)',
                        'EXECUTE'
                    )
                FROM pg_roles AS role
                WHERE role.rolname = current_user
                """
            )
            privilege_row = cursor.fetchone()
            if privilege_row is None:
                raise AssertionError("Connected role is missing from pg_roles.")
            (
                current_user,
                is_superuser,
                can_create_database,
                can_create_roles,
                can_replicate,
                bypasses_rls,
                can_connect,
                can_create_database_objects,
                can_create_temp_tables,
                schema_usage,
                owners_select,
                owners_insert,
                accounts_select,
                accounts_insert,
                transactions_select,
                transactions_insert,
                entries_select,
                entries_insert,
                idempotency_select,
                idempotency_insert,
                idempotency_update,
                entries_update,
                entries_delete,
                transactions_update,
                transactions_delete,
                accounts_update,
                accounts_id_update,
                sequence_usage,
                sequence_select,
                locker_membership,
                can_lock_accounts,
            ) = privilege_row

            assert current_user == "ledger_app"
            assert not any(
                (
                    is_superuser,
                    can_create_database,
                    can_create_roles,
                    can_replicate,
                    bypasses_rls,
                )
            )
            assert can_connect
            assert not can_create_database_objects
            assert not can_create_temp_tables
            assert schema_usage
            assert owners_select and owners_insert
            assert accounts_select and accounts_insert
            assert transactions_select and transactions_insert
            assert entries_select and entries_insert
            assert idempotency_select and idempotency_insert and idempotency_update
            assert not entries_update and not entries_delete
            assert not transactions_update and not transactions_delete
            assert not accounts_update and not accounts_id_update
            assert sequence_usage and not sequence_select
            assert not locker_membership
            assert can_lock_accounts

            cursor.execute(
                "INSERT INTO ledger.owners (id, name) VALUES (%s, %s)",
                (owner_id, f"security-{owner_id}"),
            )
            cursor.execute(
                """
                INSERT INTO ledger.accounts (id, owner_id, name)
                VALUES (%s, %s, %s)
                """,
                (account_id, owner_id, f"security-{account_id}"),
            )
            transaction_id = uuid4()
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
                    transaction_id, account_id, amount_minor
                )
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (transaction_id, account_id, 1),
            )
            assert cursor.fetchone() is not None
            cursor.execute(
                """
                INSERT INTO ledger.idempotency_keys (
                    owner_id, key, request_hash, response_status
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_id, key)
                DO UPDATE SET response_status = EXCLUDED.response_status
                """,
                (owner_id, "security", b"\x00", 201),
            )
            cursor.execute(
                """
                INSERT INTO ledger.idempotency_keys (
                    owner_id, key, request_hash, response_status
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (owner_id, key)
                DO UPDATE SET response_status = EXCLUDED.response_status
                """,
                (owner_id, "security", b"\x00", 202),
            )
            cursor.execute(
                """
                SELECT response_status
                FROM ledger.idempotency_keys
                WHERE owner_id = %s AND key = %s
                """,
                (owner_id, "security"),
            )
            assert cursor.fetchone() == (202,)
            cursor.execute(
                "SELECT ledger.lock_account_for_update(%s)",
                (account_id,),
            )

        connection.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger.entries SET amount_minor = amount_minor WHERE false",
        "DELETE FROM ledger.entries WHERE false",
        "UPDATE ledger.transactions SET reference = reference WHERE false",
        "DELETE FROM ledger.transactions WHERE false",
        "UPDATE ledger.accounts SET id = id WHERE false",
        "CREATE TEMPORARY TABLE security_temp (id integer)",
    ],
)
def test_runtime_role_cannot_mutate_protected_rows(statement: str) -> None:
    with psycopg.connect(DATABASE_URL) as connection, pytest.raises(
        psycopg.errors.InsufficientPrivilege
    ):
        connection.execute(statement)
