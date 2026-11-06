# F2: Lost-Update Experiment

## Purpose

A lost update occurs when concurrent transactions read the same mutable value,
calculate changes from that stale value, and write their results back. A later
write can overwrite an earlier change.

This experiment intentionally demonstrates that behavior using a separate
mutable-balance table, `ledger.f02_experiment_balances`. It does not use the
production ledger's accounts or entries.

## Deterministic interleaving

The test starts the experiment row at `10,000` minor units. Two worker
transactions use independent PostgreSQL connections:

| Step | Transaction A | Transaction B |
| --- | --- | --- |
| 1 | Reads `10,000` | |
| 2 | | Reads `10,000` |
| 3 | Reaches the read barrier | Reaches the read barrier |
| 4 | Calculates `10,000 - 1,000 = 9,000` and executes its update | Waits for A's update event |
| 5 | Signals the event after its `UPDATE` statement completes, then commits | Calculates `10,000 - 2,000 = 8,000` from its earlier read and executes its update |
| 6 | | Commits after its update can proceed |

The `Barrier(2)` ensures both reads finish before either worker continues.
Transaction A's `Event` is set after A's update statement, not after its
commit. If B reaches its update while A still holds the row lock, PostgreSQL
waits for A's transaction to finish; B still writes the value calculated from
its earlier `10,000` read.

The resulting balance is `8,000`. Applying both deductions mathematically
would produce `10,000 - 1,000 - 2,000 = 7,000`. A's `1,000` deduction is
therefore lost from the final stored value.

## Why the experiment is vulnerable

The experiment performs an application-side read-modify-write against a
mutable `balance_minor` column. It has no row-locking or version check between
the read and write. The worker transactions retain the value returned by
their `SELECT` and later write a constant calculated from it.

## Why this is not the production balance path

The production transfer path writes debit and credit rows to
`ledger.entries`; it does not overwrite a stored balance. It calculates an
account balance with `SUM(amount_minor)`. The application role configured by
the migrations can insert entries but cannot update or delete them. This
append-only write path avoids this particular lost-update pattern: there is no
mutable balance value for a second transaction to overwrite.

This is a statement about this specific balance-update pattern, not a claim
that every database write path is protected against every failure mode.

## How the test checks the result

`tests/experiments/test_f02_lost_update.py` seeds the row, runs the two worker
functions in a `ThreadPoolExecutor`, and opens separate `psycopg` connections
for those transactions. It coordinates them with `Barrier` and `Event`, not
sleep-based timing. After both futures complete, the test reads the stored
balance and asserts that it is `8,000` and differs from the mathematical
result of `7,000`.

## Running safely

The test reads `LEDGER_DATABASE_URL` and creates then drops the fixed table
`ledger.f02_experiment_balances`. Run it only against a disposable test
database, with no concurrent run using that table, and with a database role
that can create and drop tables in the `ledger` schema. The normal `ledger_app`
role does not have those schema privileges, so the URL documented for normal
application tests is insufficient for this experiment. Do not point the test
at a database containing a pre-existing table of that name: its cleanup drops
that table.
