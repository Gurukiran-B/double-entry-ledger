# Double-Entry Ledger Prototype

A Python 3.12 library and PostgreSQL 16 schema for exploring atomic, double-entry transfers, row-level locking, and idempotent retries. It is an engineering prototype, not a production banking platform.

There is **no REST API and no authentication**. `created_by` is supplied by the library caller; it is not an authenticated identity. The repository includes deterministic experiments showing common transaction and concurrency failure modes.

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
   - Asserts that the caller-supplied owner (`created_by`) owns the source account.
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
- **Quality & Testing:** pytest, pytest-timeout, Ruff, Mypy
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
   This is enforced at commit by PostgreSQL deferred constraint triggers (`transactions_must_balance` and `entries_must_balance`). This checks transaction balance, not account ownership or overdraft policy; direct SQL writers can still create balanced transactions outside the transfer function.
3. **Integer Money Representation:** Currency amounts are strictly represented in minor units (e.g., cents, pence) as 64-bit integers (`BIGINT`). Floating-point types (`REAL`, `FLOAT`, `DOUBLE PRECISION`) are prohibited to eliminate binary rounding errors.

### Funding and Negative Balances

The `allow_negative` account column is stored but is not consulted by the transfer implementation: `transfer` and `transfer_idempotently` reject any transfer that would leave the source below zero, regardless of that flag. There is no public deposit, withdrawal, account-creation, or initial-funding function. Tests seed starting balances using balanced entries directly. The so-called system/funding accounts in fixtures are ordinary owner-linked accounts, not a special system-account type. Do not treat test seeding as a supported funding workflow.

---

## Atomicity, Isolation & Idempotency

### Atomicity & Transaction Boundaries
- Each public transfer function commits or rolls back the transaction on the supplied Psycopg connection, and restores its previous autocommit setting. On failure it rolls back all work in that connection's current transaction.
- Use a dedicated connection with no unrelated pending work: a successful call commits any pre-existing work on that connection, and a failure can roll it back.
- Idempotency claim, ledger writes, saved status/body, and commit are one transaction. If the operation fails before commit, its claim is rolled back too.
- PostgreSQL's default isolation level is used; this project does not configure `SERIALIZABLE`. The transfer path coordinates conflicting operations with account-row locks.

### Idempotency Guarantee
Network clients often retry requests after timeouts or disconnects:
- Every idempotent request requires an `idempotency_key`.
- The combination `(owner_id, key)` is unique.
- The request payload is hashed using SHA-256 (`request_hash`).
- **Concurrent retries:** If two identical requests hit the server concurrently, the second blocks on PostgreSQL's index row lock. Once the first commits, the second unblocks and re-reads the committed response payload, returning `replayed=True` without re-executing ledger writes.
- **Conflicting retries:** If an idempotency key is reused with modified amounts or accounts, the hash comparison detects the tampering and rejects the operation with `IdempotencyConflictError`.
- **Lost response:** After a successful commit, retrying the same owner/key and request returns the saved status and JSON body rather than creating another transfer.
- **Retention:** No expiry or automatic cleanup is implemented. Idempotency rows remain until explicitly removed or their owner is deleted subject to foreign-key constraints; clients must not assume a retry window or that a key becomes reusable after a period.

---

## Concurrency Control & Row-Level Locking

### Overdraft Race Elimination
When two transfers attempt to debit the same account concurrently, naive systems experience a Time-of-Check to Time-of-Use (TOCTOU) race:
1. Thread A reads balance: \$100.
2. Thread B reads balance: \$100.
3. Thread A debits \$80, balance becomes \$20.
4. Thread B debits \$80, account becomes -\$60 (unauthorized overdraft).

The transfer function prevents this TOCTOU race on its normal code path by acquiring a PostgreSQL row-level lock on account rows:
```sql
SELECT account.id FROM ledger.accounts WHERE id = p_account_id FOR NO KEY UPDATE;
```
`FOR NO KEY UPDATE` prevents concurrent balance computations while permitting concurrent non-conflicting foreign key validations. No `SERIALIZABLE` isolation level is set.

### Deadlock Prevention
If Thread 1 transfers from Account A to Account B, and Thread 2 transfers from Account B to Account A:
- Naive order: Thread 1 locks A, Thread 2 locks B $\rightarrow$ Deadlock.
- **Deterministic ordering:** `_lock_accounts` sorts the account UUIDs ascending before acquiring locks:
  $$\text{Lock Order} = \text{sort}([A, B])$$
  Both threads acquire lock A first, then lock B, converting a potential deadlock cycle into serial execution.

---

## Security Design & Database Roles

The schema separates migration/admin, runtime, and lock-function roles:

| Role | Login | Superuser | Privileges | Purpose |
| --- | --- | --- | --- | --- |
| `ledger_admin` | Yes | Deployment-dependent (the supplied Docker setup makes it the PostgreSQL bootstrap superuser) | Migration and test setup privileges | Applies schema migrations and runs test setup/cleanup |
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

These are role grants, not an authentication or tenant-isolation boundary. `created_by` and the owner UUID are supplied by the caller, and the source-account ownership check is in Python. The runtime role also has direct `INSERT` access to ledger tables, so a holder of its credentials can bypass the transfer function's ownership and overdraft checks (but database constraints still apply). It can also directly change idempotency records, including saved responses. PostgreSQL superusers and sufficiently privileged owners can bypass ordinary grants and disable triggers; immutability and least privilege do not protect against them.

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
- This protects normal DML paths; a superuser or privileged table owner can disable/drop the trigger or otherwise bypass it.

---

## Database Migrations

Migrations are stored in `migrations/` as numbered SQL files:

| File | Purpose | Invariants Established |
| --- | --- | --- |
| `001_roles_and_schema.sql` | Schema creation | Creates `ledger` schema and `schema_migrations` table |
| `002_tables.sql` | Core entities | Creates `owners`, `accounts`, and `transactions` tables |
| `003_add_missing_tables.sql` | Historical no-op | Tables it once referred to were already created by migration 002; retained to preserve applied migration history |
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
The runner records applied filenames in `ledger.schema_migrations` and skips them on subsequent runs. Migration 003 is intentionally empty and must not be rewritten to retrofit a schema change; add a new numbered migration for future changes. Existing applied migration history is not edited.

---

## Configuration & Environment Variables

Configure the service using environment variables. Placeholders are used below:

```bash
# Local library database (Docker maps this to host port 5433)
LEDGER_DATABASE_URL=postgresql://ledger_app:<app-password>@127.0.0.1:5433/ledger

# Local migration connection
LEDGER_MIGRATION_DATABASE_URL=postgresql://ledger_admin:<admin-password>@127.0.0.1:5433/ledger

# Isolated test database (Docker maps this to host port 5434)
LEDGER_TEST_ADMIN_DATABASE_URL=postgresql://ledger_admin:<admin-password>@127.0.0.1:5434/ledger_test
```

The test fixtures reject connection URLs configured for port 5432 before connecting. The admin fixture also verifies the database name, marker, and superuser role; the application fixture verifies the runtime role's grants. Keep `.env` local and replace the example passwords with distinct values.

---

## Local Setup & Execution

### Prerequisites
- Python 3.12
- Docker & Docker Compose
- Windows PowerShell, Linux shell, or macOS shell

### 1. Initialize Configuration

Windows PowerShell:
```powershell
Copy-Item .env.example .env
# Edit .env and replace the example values with distinct local passwords.
```

Linux/macOS:
```bash
cp .env.example .env
# Edit .env and replace the example values with distinct local passwords.
```

### 2. Start PostgreSQL

The local library database is bound to `127.0.0.1:5433`; it does not use host port 5432:
```powershell
docker compose up -d --wait
```

### 3. Set Up the Python Environment

Windows PowerShell:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Linux/macOS:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 4. Apply Schema Migrations

Windows PowerShell:
```powershell
$env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:<admin-password>@127.0.0.1:5433/ledger"
python -m ledger.migrate
```

Linux/macOS:
```bash
export LEDGER_MIGRATION_DATABASE_URL='postgresql://ledger_admin:<admin-password>@127.0.0.1:5433/ledger'
python -m ledger.migrate
```

### 5. Using the Python Library

Replace the example IDs with existing owner/account UUIDs; this package does not create owners, accounts, or starting funds.
```python
import psycopg
from ledger import transfer_idempotently, get_balance

with psycopg.connect("postgresql://ledger_app:<app-password>@127.0.0.1:5433/ledger") as conn:
    balance = get_balance(conn, source_account_id)
    result = transfer_idempotently(
        connection=conn,
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        amount_minor=5000,
        created_by=owner_id,
        idempotency_key="tx-req-12345",
    )
    print(result)
```

---

## Automated Testing & Verification

### Safety Rule for Tests
All database tests refuse to connect to host port 5432. They use a separate `ledger_test` database on port `5434`, marked `ledger-experiment-tests-only`. Start it independently from the local library database:

### Start the Ephemeral Test Database
```powershell
docker compose --profile test up -d --wait postgres-test
```

Linux/macOS:
```bash
docker compose --profile test up -d --wait postgres-test
```

The test service has its own in-memory `tmpfs` data directory and host port 5434; it does not mount the local ledger database volume. Its initialization script creates the runtime role and database marker.

### Migrate and Run Checks

Windows PowerShell (use the same passwords as in `.env`):
```powershell
$env:LEDGER_MIGRATION_DATABASE_URL = "postgresql://ledger_admin:<admin-password>@127.0.0.1:5434/ledger_test"
$env:LEDGER_DATABASE_URL = "postgresql://ledger_app:<app-password>@127.0.0.1:5434/ledger_test"
$env:LEDGER_TEST_ADMIN_DATABASE_URL = $env:LEDGER_MIGRATION_DATABASE_URL

python -m ledger.migrate
python -m pytest -v
ruff check .
mypy src tests
```

Linux/macOS:
```bash
export LEDGER_MIGRATION_DATABASE_URL='postgresql://ledger_admin:<admin-password>@127.0.0.1:5434/ledger_test'
export LEDGER_DATABASE_URL='postgresql://ledger_app:<app-password>@127.0.0.1:5434/ledger_test'
export LEDGER_TEST_ADMIN_DATABASE_URL="$LEDGER_MIGRATION_DATABASE_URL"

python -m ledger.migrate
python -m pytest -v
ruff check .
mypy src tests
```

Remove only the isolated test database container when finished (the local library database is left running):
```shell
docker compose --profile test rm --stop --force postgres-test
```

---

## Known Limitations & Future Architecture

1. **Single-Currency Ledger:**
   The current schema handles one abstract minor currency unit (`BIGINT`). It does not store currency codes (e.g., USD, EUR) or support multi-currency foreign exchange (FX) settlement entries.
2. **Balance Aggregation at Scale:**
   Balances are recomputed by summing immutable entries. The `(account_id, id)` index helps, but aggregation cost and index/storage growth increase with ledger history; high-volume accounts may need a carefully designed checkpoint/snapshot strategy. No benchmark or supported scale threshold is claimed.
3. **No HTTP or Identity Layer:**
   This repository exposes a Python library only. It has no REST API or authentication; a future network-facing service would need request validation, authenticated identity, and connection lifecycle controls.
4. **Funding and Withdrawals:**
   There is no supported deposit, withdrawal, or account funding operation. These require explicit product/accounting rules and should not be inferred from raw entry insertion or `allow_negative`.
5. **Database Trust Boundary:**
   Role grants protect the ordinary application connection, not against PostgreSQL superusers or privileged database owners. `created_by` is not authenticated, and the database does not enforce transfer ownership/overdraft policy for arbitrary direct inserts by `ledger_app`.
6. **Single-Cluster PostgreSQL:**
   The transfer path uses PostgreSQL row-level locks at the configured/default transaction isolation; this is not a claim of SERIALIZABLE isolation or distributed transaction guarantees.
