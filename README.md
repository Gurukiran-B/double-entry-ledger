# Double-Entry Ledger Service

A correctness-focused, ACID-compliant double-entry ledger backend implemented in Python 3.12 and PostgreSQL 16. The service is engineered to guarantee financial invariants: money cannot be created or destroyed out of thin air, account balances cannot be overdrawn concurrently, transaction history is immutable, and duplicate requests are handled idempotently.

The repository includes a production transfer engine alongside deterministic experiment suites demonstrating concurrency bugs (overdraft races, lost updates, deadlocks) and the architectural mechanisms that eliminate them.

> **Scope Notice:** This project currently provides an in-process Python library and a migration runner. It enforces transaction safety, database-level security, and idempotency at the database and service boundaries. A minimal REST API integration (FastAPI) is evaluated in the design documentation for future HTTP exposure.

---

## Table of Contents

1. [Architecture & Execution Flow](#architecture--execution-flow)
2. [Technology Stack](#technology-stack)
3. [Database Schema & Relationships](#database-schema--relationships)
4. [Double-Entry Accounting Principles](#double-entry-accounting-principles)
5. [Atomicity, Isolation & Idempotency](#atomicity-isolation--idempotency)
6. [Concurrency Control & Row-Level Locking](#concurrency-control--row-level-locking)
7. [Security Design & Database Roles](#security-design--database-roles)
8. [Database Migrations](#database-migrations)
9. [Configuration & Environment Variables](#configuration--environment-variables)
10. [Local Setup & Execution](#local-setup--execution)
11. [Automated Testing & Verification](#automated-testing--verification)
12. [Known Limitations & Future Architecture](#known-limitations--future-architecture)

---

## Architecture & Execution Flow

The service is structured as follows:

```
├── docker/
│   └── postgres/init/001_create_ledger_app.sh   # Provisions application role
├── docker-compose.yml                            # Local PostgreSQL 16 service
├── migrations/                                   # Ordered SQL migrations (001-008)
├── src/ledger/
│   ├── __init__.py                               # Package exports
│   ├── config.py                                 # Environment validation
│   ├── migrate.py                                # Idempotent migration runner
│   └── transfer.py                               # Core transfer & idempotency engine
├── experiments/                                  # Demonstrations of naive failure modes
└── tests/                                        # Architecture, security & regression tests
```

### Execution Flow for a Transfer

When executing an idempotent transfer (`transfer_idempotently`):

1. **Input Validation:**
   - Validates that `amount_minor` is a positive integer (rejects booleans, floats, decimals, negatives, and zero).
   - Validates that `source_account_id` and `destination_account_id` are distinct UUIDs.
   - Validates that `idempotency_key` is a non-empty string between 1 and 255 characters.
2. **Request Fingerprinting:**
   - Generates a deterministic SHA-256 hash of the canonical JSON payload `{"amount_minor": ..., "destination_account_id": ..., "source_account_id": ...}`.
3. **Idempotency Claim:**
   - Attempts `INSERT INTO ledger.idempotency_keys (owner_id, key, request_hash) VALUES (...) ON CONFLICT (owner_id, key) DO NOTHING`.
   - If an existing record is detected, the transaction locks the row (`FOR UPDATE`), compares the stored hash to the incoming hash, and either:
     - Returns the saved response if identical (`replayed=True`), or
     - Raises `IdempotencyConflictError` if the key was previously used with different parameters.
4. **Row-Level Account Locking:**
   - If claimed, locks both `source_account_id` and `destination_account_id` in deterministic ascending UUID order via `ledger.lock_account_for_update(uuid)`.
5. **Authorization & Balance Verification:**
   - Asserts that the authenticated owner (`created_by`) owns the source account.
   - Calculates the source account's real-time balance (`COALESCE(SUM(amount_minor), 0)`).
   - Raises `InsufficientFundsError` if `source_balance < amount_minor`.
6. **Double-Entry Insertion:**
   - Inserts transaction metadata into `ledger.transactions`.
   - Inserts two balanced entries into `ledger.entries`:
     - Debit: `-amount_minor` for the source account.
     - Credit: `+amount_minor` for the destination account.
7. **Idempotency Finalization:**
   - Updates `ledger.idempotency_keys` with the transaction ID, response status (201), and response body.
8. **Commit & Constraint Trigger Verification:**
   - Commits the database transaction. PostgreSQL deferred constraint triggers verify that the transaction contains $\ge 2$ entries and sums exactly to zero.
   - On any failure, rolls back all changes atomically, leaving no partial records.

---

## Technology Stack

- **Language:** Python 3.12 (`>=3.12, <3.13`)
- **Database:** PostgreSQL 16
- **Database Driver:** Psycopg 3 (`psycopg[binary]`)
- **Containerization:** Docker & Docker Compose
- **Quality & Testing:** pytest, pytest-timeout, Ruff, Mypy (strict type checking enabled)
- **Packaging:** Setuptools (PEP 621 compliant `pyproject.toml`)

---

## Database Schema & Relationships

All application tables reside in the dedicated `ledger` schema.

```
                    +-----------------------+
                    |     ledger.owners     |
                    +-----------------------+
                    | id (UUID, PK)         |
                    | name (TEXT)           |
                    | created_at (TIMESTAMPTZ)|
                    +-----------------------+
                                | 1
                                |
                                | N
                    +-----------------------+
                    |    ledger.accounts    |
                    +-----------------------+
                    | id (UUID, PK)         |
                    | owner_id (UUID, FK)   |--------+
                    | name (TEXT)           |        |
                    | allow_negative (BOOL) |        |
                    | created_at (TIMESTAMPTZ)|      |
                    +-----------------------+        |
                                | 1                  |
                                |                    |
                                | N                  |
+--------------------------+    |                    |
|   ledger.transactions    |    |                    |
+--------------------------+    |                    |
| id (UUID, PK)            |    |                    |
| created_by (UUID, FK)----+----+                    |
| reference (TEXT)         |                         |
| created_at (TIMESTAMPTZ) |                         |
+--------------------------+                         |
       | 1                           | 1             |
       |                             |               |
       | N                           | N             |
+------------------------------------------+         |
|              ledger.entries              |         |
+------------------------------------------+         |
| id (BIGINT GENERATED ALWAYS, PK)         |         |
| transaction_id (UUID, FK)                |         |
| account_id (UUID, FK)                    |         |
| amount_minor (BIGINT, CHECK <> 0)        |         |
| created_at (TIMESTAMPTZ)                 |         |
+------------------------------------------+         |
                                                     |
+------------------------------------------+         |
|         ledger.idempotency_keys          |         |
+------------------------------------------+         |
| owner_id (UUID, PK, FK) -----------------+---------+
| key (TEXT, PK, CHECK 1..255)             |
| request_hash (BYTEA)                     |
| transaction_id (UUID, FK)                |
| response_status (SMALLINT)               |
| response_body (JSONB)                    |
| created_at (TIMESTAMPTZ)                 |
+------------------------------------------+
```

### Table Definitions & Invariants

1. **`ledger.owners`**: Represents legal identities/tenants.
2. **`ledger.accounts`**: Represents accounts owned by an owner. Has index on `owner_id`.
3. **`ledger.transactions`**: Represents financial events. Contains audit reference and `created_by` foreign key.
4. **`ledger.entries`**: Signed line items representing debits (negative) and credits (positive).
   - `amount_minor` cannot be 0 (`CHECK (amount_minor <> 0)`).
   - `amount_minor` capped to $\pm 10^{12}$ minor units (`CHECK (amount_minor BETWEEN -1000000000000 AND 1000000000000)`).
   - Indexed by `(account_id, id)` for efficient balance aggregation and `transaction_id` for join performance.
   - Protected by `entries_immutable` trigger.
5. **`ledger.idempotency_keys`**: Composite primary key `(owner_id, key)`. Scoped per owner so different users can safely use identical client keys.
6. **`ledger.schema_migrations`**: Tracks applied SQL migration filenames and timestamps.

---

## Double-Entry Accounting Principles

In traditional naive architectures, an account balance is stored as a mutable number:
```sql
-- DANGEROUS: Susceptible to lost updates and missing audit trail
UPDATE accounts SET balance = balance - 100 WHERE id = ...;
```
This design is fundamentally vulnerable to race conditions, lost updates, and untraceable discrepancies.

This ledger enforces **pure double-entry book-keeping**:
1. **Balances are Derived, Not Stored:** An account balance is the algebraic sum of all historical immutable entries:
   $$\text{Balance}(A) = \sum_{e \in \text{Entries}(A)} e.\text{amount\_minor}$$
2. **Zero-Sum Transactions:** Every transaction must have at least two entries whose amounts sum exactly to zero:
   $$\sum_{e \in \text{Transaction}} e.\text{amount\_minor} = 0$$
   This is enforced at the database level by PostgreSQL deferred constraint triggers (`transactions_must_balance` and `entries_must_balance`). Money cannot be created or destroyed.
3. **Integer Money Representation:** Currency amounts are strictly represented in minor units (e.g., cents, pence) as 64-bit integers (`BIGINT`). Floating-point types (`REAL`, `FLOAT`, `DOUBLE PRECISION`) are prohibited to eliminate binary rounding errors.

---

## Atomicity, Isolation & Idempotency

### Atomicity & Transaction Boundaries
- All ledger modifications occur within a single database transaction (`BEGIN ... COMMIT`).
- If an unhandled exception or constraint failure occurs, Psycopg issues `ROLLBACK`, guaranteeing that half-transfers or dangling debits can never exist.
- Autocommit state is restored safely only if the connection remains open.

### Idempotency Guarantee
Network clients often retry requests after timeouts or disconnects:
- Every idempotent request requires an `idempotency_key`.
- The combination `(owner_id, key)` is unique.
- The request payload is hashed using SHA-256 (`request_hash`).
- **Concurrent retries:** If two identical requests hit the server concurrently, the second blocks on PostgreSQL's index row lock. Once the first commits, the second unblocks and re-reads the committed response payload, returning `replayed=True` without re-executing ledger writes.
- **Conflicting retries:** If an idempotency key is reused with modified amounts or accounts, the hash comparison detects the tampering and rejects the operation with `IdempotencyConflictError`.

---

## Concurrency Control & Row-Level Locking

### Overdraft Race Elimination
When two transfers attempt to debit the same account concurrently, naive systems experience a Time-of-Check to Time-of-Use (TOCTOU) race:
1. Thread A reads balance: \$100.
2. Thread B reads balance: \$100.
3. Thread A debits \$80, balance becomes \$20.
4. Thread B debits \$80, account becomes -\$60 (unauthorized overdraft).

This service eliminates TOCTOU races by acquiring an exclusive row-level lock on account rows:
```sql
SELECT account.id FROM ledger.accounts WHERE id = p_account_id FOR NO KEY UPDATE;
```
`FOR NO KEY UPDATE` prevents concurrent balance computations while permitting concurrent non-conflicting foreign key validations.

### Deadlock Prevention
If Thread 1 transfers from Account A to Account B, and Thread 2 transfers from Account B to Account A:
- Naive order: Thread 1 locks A, Thread 2 locks B $\rightarrow$ Deadlock.
- **Deterministic ordering:** `_lock_accounts` sorts the account UUIDs ascending before acquiring locks:
  $$\text{Lock Order} = \text{sort}([A, B])$$
  Both threads acquire lock A first, then lock B, converting a potential deadlock cycle into serial execution.

---

## Security Design & Database Roles

The database adheres strictly to the **Principle of Least Privilege (PoLP)** with three distinct roles:

| Role | Login | Superuser | Privileges | Purpose |
| --- | --- | --- | --- | --- |
| `ledger_admin` | Yes | Yes / Admin | Full privileges on `ledger` schema | Applies schema migrations and runs test setup/cleanup |
| `ledger_account_locker` | No | No | `USAGE` on schema, `UPDATE (id)` on `ledger.accounts` | Dedicated role that owns the `SECURITY DEFINER` account locking function |
| `ledger_app` | Yes | No | Minimal DML (details below) | Runtime application role used by the service |

### Runtime Role (`ledger_app`) Restrictions
- **REVOKED:** All permissions on `ledger.schema_migrations` (application cannot tamper with migration history).
- **REVOKED:** `TEMPORARY` on database and `CREATE` on schema (cannot create tables or temp tables).
- **REVOKED:** `UPDATE` and `DELETE` on `ledger.entries`, `ledger.transactions`, and `ledger.accounts`.
- **GRANTED:** `SELECT`, `INSERT` on `owners`, `accounts`, `transactions`, `entries`.
- **GRANTED:** `SELECT`, `INSERT`, `UPDATE` on `idempotency_keys` (to save responses).
- **GRANTED:** `USAGE` on sequence `entries_id_seq` (no `SELECT` privilege to prevent sequence value manipulation).
- **EXECUTE:** `ledger.lock_account_for_update(uuid)`.

### Function Hardening (`SECURITY DEFINER`)
`lock_account_for_update` is owned by `ledger_account_locker` and explicitly sets:
```sql
SET search_path = pg_catalog, ledger;
```
This protects against search-path hijacking attacks. `ledger_app` can execute this function to lock an account row without having general `UPDATE` privileges on account attributes.

### Database Immutability Trigger
To defend against accidental or malicious internal updates:
- Migration `007_immutable_entries.sql` attaches a `BEFORE UPDATE OR DELETE` trigger on `ledger.entries`.
- Any modification raises an exception with SQLSTATE `55000` (`Ledger entries are immutable`).
- Even superusers cannot update or delete entries without explicitly disabling the trigger.

---

## Database Migrations

Migrations are stored in `migrations/` as numbered SQL files:

| File | Purpose | Invariants Established |
| --- | --- | --- |
| `001_roles_and_schema.sql` | Schema creation | Creates `ledger` schema and `schema_migrations` table |
| `002_tables.sql` | Core entities | Creates `owners`, `accounts`, and `transactions` tables |
| `003_add_missing_tables.sql` | Sequence preserve | No-op preserved for migration history compatibility |
| `004_entries.sql` | Ledger entries | Creates `entries` table with nonzero and range constraints |
| `005_idempotency.sql` | Idempotency store | Creates `idempotency_keys` table with composite primary key |
| `006_application_role_security.sql` | PoLP security | Sets role privileges and deploys `lock_account_for_update` |
| `007_immutable_entries.sql` | Append-only ledger | Adds `entries_immutable` trigger preventing mutation |
| `008_balanced_transactions.sql` | Conservation of money | Adds deferred triggers enforcing zero-sum balanced transactions |

### Applying Migrations
Run the migration module with administrative credentials:
```powershell
python -m ledger.migrate
```
The runner is idempotent: applied versions are recorded in `ledger.schema_migrations` and skipped on subsequent runs.

---

## Configuration & Environment Variables

Configure the service using environment variables. Placeholders are used below:

```bash
# Database connection for the runtime application (PoLP role)
LEDGER_DATABASE_URL=postgresql://ledger_app:<app-password>@127.0.0.1:5432/ledger

# Database connection for migrations (administrative role)
LEDGER_MIGRATION_DATABASE_URL=postgresql://ledger_admin:<admin-password>@127.0.0.1:5432/ledger

# Database connection for test execution (points to isolated test DB on port 5433)
LEDGER_TEST_ADMIN_DATABASE_URL=postgresql://ledger_admin:<admin-password>@127.0.0.1:5433/ledger_test
```

> **Note on Windows Hosts:** Always use `127.0.0.1` rather than `localhost` in database connection strings to avoid IPv6 name resolution delays.

---

## Local Setup & Execution

### Prerequisites
- Python 3.12+
- Docker & Docker Compose
- Windows PowerShell or Bash

### 1. Initialize Configuration
```powershell
Copy-Item .env.example .env
# Edit .env and supply strong passwords for POSTGRES_ADMIN_PASSWORD and LEDGER_APP_PASSWORD
```

### 2. Start PostgreSQL Container
```powershell
docker compose up -d --wait
```

### 3. Setup Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 4. Apply Schema Migrations
```powershell
$env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:<admin-password>@127.0.0.1:5432/ledger"
$env:LEDGER_DATABASE_URL = "postgresql://ledger_app:<app-password>@127.0.0.1:5432/ledger"
python -m ledger.migrate
```

### 5. Using the Python Library
```python
from uuid import uuid4
import psycopg
from ledger import transfer_idempotently, get_balance

with psycopg.connect("postgresql://ledger_app:<app-password>@127.0.0.1:5432/ledger") as conn:
    # Check balance
    balance = get_balance(conn, source_account_id)
    
    # Execute safe transfer
    result = transfer_idempotently(
        connection=conn,
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        amount_minor=5000,  # $50.00 in minor units
        created_by=owner_id,
        idempotency_key="tx-req-12345",
    )
    print("Transfer executed:", result)
```

---

## Automated Testing & Verification

### Safety Rule for Tests
All tests that execute database modifications **strictly refuse** to connect to port 5432. They require an isolated database named `ledger_test` on port `5433`, marked with the description `'ledger-experiment-tests-only'`.

### Starting the Isolated Test Database
```powershell
$initPath = (Resolve-Path ".\docker\postgres\init").Path

docker run --detach --rm --name ledger-test-postgres `
  --env POSTGRES_USER=ledger_admin `
  --env POSTGRES_PASSWORD=<test-admin-password> `
  --env POSTGRES_DB=ledger_test `
  --env LEDGER_APP_PASSWORD=<test-app-password> `
  --publish 127.0.0.1:5433:5432 `
  --tmpfs /var/lib/postgresql/data:rw,size=512m `
  --mount "type=bind,source=$initPath,target=/docker-entrypoint-initdb.d,readonly" `
  postgres:16

# Configure database marker and application role
docker exec ledger-test-postgres psql -U ledger_admin -d ledger_test `
  -c "COMMENT ON DATABASE ledger_test IS 'ledger-experiment-tests-only'; CREATE ROLE ledger_app LOGIN PASSWORD '<test-app-password>'; GRANT CONNECT ON DATABASE ledger_test TO ledger_app;"
```

### Running Migrations & Test Suites
```powershell
$env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:<test-admin-password>@127.0.0.1:5433/ledger_test"
$env:LEDGER_DATABASE_URL = "postgresql://ledger_app:<test-app-password>@127.0.0.1:5433/ledger_test"
$env:LEDGER_TEST_ADMIN_DATABASE_URL = $env:LEDGER_MIGRATION_DATABASE_URL

# 1. Apply migrations to test DB
python -m ledger.migrate

# 2. Run full pytest suite (33 tests)
python -m pytest -v

# 3. Static code analysis and type checks
ruff check .
mypy src tests
```

---

## Known Limitations & Future Architecture

1. **Single-Currency Ledger:**
   The current schema handles one abstract minor currency unit (`BIGINT`). It does not store currency codes (e.g., USD, EUR) or support multi-currency foreign exchange (FX) settlement entries.
2. **Balance Aggregation at Scale:**
   Balances are computed dynamically via `SUM(amount_minor)`. While high-performance indexes `(account_id, id)` support fast aggregation for thousands of transactions, high-throughput accounts with millions of entries require periodic balance checkpoints/snapshots.
3. **In-Process Interface vs REST API:**
   The service is currently consumed as a Python library. Declared dependencies include FastAPI and Uvicorn. Exposing a REST API requires adding HTTP request validation, authentication middleware (e.g., JWT/mTLS to identify `owner_id`), and an asynchronous Psycopg connection pool.
4. **Single-Cluster PostgreSQL:**
   The row-locking and trigger guarantees rely on PostgreSQL's single-node serializable capabilities. Scaling horizontally across sharded database engines would require two-phase commit (2PC) or distributed consensus coordination (e.g., Raft).
