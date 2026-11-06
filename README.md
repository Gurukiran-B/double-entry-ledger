# Double-Entry Ledger

A correctness-focused Python and PostgreSQL ledger that demonstrates safe money
transfers, concurrency control, idempotency, and database-enforced invariants.
The repository includes deterministic experiments that show common failure
modes alongside regression tests for the production transfer path.

> **Scope:** This project currently provides a Python library and a database
> migration runner. It does not yet include an HTTP API, authentication system,
> or web application.

## Architecture

- `src/ledger/` contains the production Python package. `transfer.py` exposes
  balance lookup, atomic transfers, and idempotent transfers using Psycopg.
- `migrations/` contains ordered PostgreSQL schema and security migrations.
  Ledger entries are signed integer minor currency units; account balances are
  calculated from those entries rather than stored as mutable balances.
- PostgreSQL coordinates concurrent transfers with ordered account-row locks.
  A restricted `SECURITY DEFINER` function provides the lock operation without
  granting the runtime role general account-update privileges.
- Database permissions restrict the runtime role. Entries are immutable, and
  deferred constraint triggers require each transaction to contain at least
  two entries whose amounts sum to zero.
- `experiments/` contains deliberately unsafe examples and demonstrations.
  `tests/` includes architecture, PostgreSQL security, and regression tests.

## Technology

- Python 3.12
- PostgreSQL 16
- Psycopg 3
- Docker Compose for local PostgreSQL
- pytest and pytest-timeout, Ruff, and mypy for development checks
- setuptools for packaging

FastAPI and Uvicorn are declared dependencies, but an HTTP service is not
implemented yet.

## Features

- Atomic transfers represented by balanced debit and credit entries
- Integer minor-unit amounts with positive-value validation
- Source-account ownership checks
- Consistent account-lock ordering to reduce deadlocks and overdraft races
- Idempotency keys scoped to the requesting owner, with saved responses and
  conflicting-request detection
- Append-only ledger entries protected by database triggers and permissions
- PostgreSQL tests and experiments covering concurrency, atomicity,
  authorization, idempotency, immutable history, and integer money

## Database migrations

Run `python -m ledger.migrate` with `LEDGER_MIGRATION_DATABASE_URL` set to a
privileged database connection. The runner applies sorted `*.sql` files once,
recording applied versions in `ledger.schema_migrations`.

| Migration | Purpose |
| --- | --- |
| `001_roles_and_schema.sql` | Create the ledger schema and migration history table |
| `002_tables.sql` | Create owners, accounts, and transactions |
| `003_add_missing_tables.sql` | Retain the historical migration sequence; currently a no-op |
| `004_entries.sql` | Create immutable, integer-valued ledger entries |
| `005_idempotency.sql` | Store owner-scoped idempotency keys and responses |
| `006_application_role_security.sql` | Set runtime privileges and add the restricted account-lock function |
| `007_immutable_entries.sql` | Reject updates and deletes to ledger entries |
| `008_balanced_transactions.sql` | Require balanced transactions at commit time |

Migrations are intended to run against a fresh database or a database whose
schema and roles have been provisioned deliberately. Do not remove a database
volume to re-run initialization if it contains data.

## Local setup and execution

Prerequisites: Python 3.12, Docker with the Compose plugin, and PowerShell for
the commands below.

1. Copy `.env.example` to `.env` and replace the example passwords with
   different strong values. URL-encode reserved characters in passwords used
   in PostgreSQL connection strings. Keep `.env` local; it is ignored by Git.

   ```powershell
   Copy-Item .env.example .env
   ```

2. Start PostgreSQL and install the project with its development dependencies.

   ```powershell
   docker compose up -d --wait
   python -m pip install -e ".[dev]"
   ```

3. Set the connection strings to match the credentials in `.env`, then apply
   migrations. The migration URL must use the `ledger_admin` role; the
   application URL must use `ledger_app`.

   ```powershell
   $env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:<admin-password>@localhost:5432/ledger"
   $env:LEDGER_DATABASE_URL = "postgresql://ledger_app:<app-password>@localhost:5432/ledger"
   python -m ledger.migrate
   ```

The package exposes `get_balance`, `transfer`, and `transfer_idempotently` from
`ledger.transfer`. Call these functions with an open Psycopg connection and
UUID identifiers. There is no HTTP server command at this time.

## Tests and development checks

Database-writing tests deliberately refuse to use PostgreSQL on port 5432.
Run them only against a separate, disposable PostgreSQL instance; never use the
local development database or its persistent Compose volume. The test
fixtures also require a database named `ledger_test`, marked with the comment
`ledger-experiment-tests-only`, and the restricted `ledger_app` role.

The following PowerShell example creates a temporary PostgreSQL 16 container
with a `tmpfs` data directory, creates and marks the test database, then runs
the migrations and tests. Replace the sample passwords locally; do not commit
them.

```powershell
$env:POSTGRES_ADMIN_PASSWORD = "replace-with-a-long-random-test-password"
$env:LEDGER_APP_PASSWORD = "replace-with-a-different-random-test-password"
$initPath = (Resolve-Path ".\docker\postgres\init").Path

docker run --detach --rm --name ledger-test-postgres `
  --env POSTGRES_USER=ledger_admin `
  --env "POSTGRES_PASSWORD=$env:POSTGRES_ADMIN_PASSWORD" `
  --env POSTGRES_DB=ledger `
  --env "LEDGER_APP_PASSWORD=$env:LEDGER_APP_PASSWORD" `
  --publish 127.0.0.1:5433:5432 `
  --tmpfs /var/lib/postgresql/data:rw,size=512m `
  --mount "type=bind,source=$initPath,target=/docker-entrypoint-initdb.d,readonly" `
  postgres:16
```

Wait until PostgreSQL is ready, create the isolated test database, and set
connection strings. The test admin URL must connect as a superuser to that
same marked database; the application URL must connect as `ledger_app`.

```powershell
docker exec ledger-test-postgres pg_isready -U ledger_admin -d ledger
docker exec ledger-test-postgres psql -U ledger_admin -d ledger `
  -c "CREATE DATABASE ledger_test"
docker exec ledger-test-postgres psql -U ledger_admin -d ledger `
  -c "COMMENT ON DATABASE ledger_test IS 'ledger-experiment-tests-only'"

$env:LEDGER_DATABASE_URL = "postgresql://ledger_app:$env:LEDGER_APP_PASSWORD@localhost:5433/ledger_test"
$env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:$env:POSTGRES_ADMIN_PASSWORD@localhost:5433/ledger_test"
$env:LEDGER_TEST_ADMIN_DATABASE_URL = $env:LEDGER_MIGRATION_DATABASE_URL

python -m ledger.migrate
python -m pytest -q
ruff check .
mypy src tests
```

Some experiments create temporary objects or require privileged setup and
cleanup. The fixtures are designed for the marked disposable database only.
When finished, stop the test container; its `tmpfs` data directory leaves no
persistent database volume:

```powershell
docker stop ledger-test-postgres
```
