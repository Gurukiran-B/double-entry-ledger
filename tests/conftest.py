"""Safety checks for every test that connects to PostgreSQL."""

import psycopg
import pytest


@pytest.fixture(autouse=True)
def require_disposable_application_database(
    request: pytest.FixtureRequest,
) -> None:
    if request.node.path.name == "test_architecture.py":
        return
    if request.node.path.name == "test_f11_integer_money.py":
        return

    from ledger.config import DATABASE_URL

    connection_info = psycopg.conninfo.conninfo_to_dict(DATABASE_URL)
    port = connection_info.get("port")
    if port is None or str(port) == "5432":
        pytest.fail("Tests must not connect to PostgreSQL on port 5432.")

    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                SELECT
                    current_database(),
                    current_user,
                    (
                        SELECT shobj_description(database.oid, 'pg_database')
                        FROM pg_database AS database
                        WHERE database.datname = current_database()
                    ),
                    role.rolsuper,
                    role.rolcreatedb,
                    role.rolcreaterole,
                    role.rolreplication,
                    role.rolbypassrls,
                    has_database_privilege(
                        current_user, current_database(), 'CREATE'
                    ),
                    has_database_privilege(
                        current_user, current_database(), 'TEMPORARY'
                    ),
                    has_schema_privilege(current_user, 'public', 'CREATE'),
                    has_table_privilege(
                        current_user, 'ledger.entries', 'UPDATE'
                    ),
                    has_table_privilege(
                        current_user, 'ledger.entries', 'DELETE'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_trigger AS trg
                        JOIN pg_class AS cls
                          ON cls.oid = trg.tgrelid
                        JOIN pg_namespace AS ns
                          ON ns.oid = cls.relnamespace
                        WHERE ns.nspname = 'ledger'
                          AND cls.relname = 'entries'
                          AND trg.tgname = 'entries_immutable'
                          AND trg.tgenabled = 'O'
                    )
                FROM pg_roles AS role
                WHERE role.rolname = current_user
                """
        )
        row = cursor.fetchone()

    if row is None:
        pytest.fail("The application database role is missing from pg_roles.")

    (
        database_name,
        role_name,
        database_marker,
        is_superuser,
        can_create_database,
        can_create_roles,
        can_replicate,
        bypasses_rls,
        can_create_database_objects,
        can_create_temp_tables,
        can_create_public_objects,
        can_update_entries,
        can_delete_entries,
        immutable_trigger_enabled,
    ) = row

    if (
        database_name != "ledger_test"
        or database_marker != "ledger-experiment-tests-only"
        or role_name != "ledger_app"
    ):
        pytest.fail(
            "Database-writing tests require ledger_app in the database "
            "ledger_test marked ledger-experiment-tests-only."
        )
    if any(
        (
            is_superuser,
            can_create_database,
            can_create_roles,
            can_replicate,
            bypasses_rls,
            can_create_database_objects,
            can_create_temp_tables,
            can_create_public_objects,
            can_update_entries,
            can_delete_entries,
        )
    ):
        pytest.fail("The test application role has excessive privileges.")
    if not immutable_trigger_enabled:
        pytest.fail("The ledger.entries immutable-entry trigger is not enabled.")
