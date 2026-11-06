import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def run_migrations() -> None:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    migration_database_url = os.environ.get("LEDGER_MIGRATION_DATABASE_URL")
    if not migration_database_url:
        raise RuntimeError("Environment variable LEDGER_MIGRATION_DATABASE_URL must be set.")

    with psycopg.connect(migration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE SCHEMA IF NOT EXISTS ledger;

                CREATE TABLE IF NOT EXISTS ledger.schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

        for migration_file in migration_files:
            version = migration_file.stem

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM ledger.schema_migrations WHERE version = %s",
                    (version,),
                )

                if cursor.fetchone() is not None:
                    print(f"Skipping {version} (already applied).")
                    continue

                sql = migration_file.read_text(encoding="utf-8")

                print(f"Running {version}...")

                cursor.execute(sql)

                cursor.execute(
                    """
                    INSERT INTO ledger.schema_migrations (version)
                    VALUES (%s)
                    """,
                    (version,),
                )

    print("Migrations complete.")


if __name__ == "__main__":
    run_migrations()
