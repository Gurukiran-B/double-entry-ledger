"""Safety checks and privileged database access for PostgreSQL experiments."""

import os

import psycopg
import pytest


@pytest.fixture
def test_admin_database_url() -> str:
    database_url = os.environ.get("LEDGER_TEST_ADMIN_DATABASE_URL")
    if not database_url:
        pytest.fail(
            "Set LEDGER_TEST_ADMIN_DATABASE_URL to a privileged connection "
            "for the isolated ledger_test database."
        )

    with psycopg.connect(database_url) as connection:
        if connection.info.port == 5432:
            pytest.fail("Test admin connections must not use port 5432.")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                    SELECT
                        current_database(),
                        role.rolsuper,
                        (
                            SELECT shobj_description(database.oid, 'pg_database')
                            FROM pg_database AS database
                            WHERE database.datname = current_database()
                        )
                    FROM pg_roles AS role
                    WHERE role.rolname = current_user
                """
            )
            row = cursor.fetchone()

    if (
        row is None
        or row[0] != "ledger_test"
        or not row[1]
        or row[2] != "ledger-experiment-tests-only"
    ):
        pytest.fail(
            "LEDGER_TEST_ADMIN_DATABASE_URL must connect as a superuser to "
            "the marked isolated ledger_test database."
        )

    return database_url
