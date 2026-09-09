#!/usr/bin/env python3
"""Copy a running season from a SQLite file to Postgres (Supabase), idempotently.

    python3 tools/sqlite2pg.py hub.db "postgres://postgres:[pw]@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require"

Why a tool and not "just re-run /setup": `/setup` re-provisions channels and roles, but points
and ledger rows are the season. Re-creating those by hand is how a community loses a month of
standings, so this copies the data and then fixes the one thing every manual port forgets.

The sequence step is not optional. SQLite's `INTEGER PRIMARY KEY` *is* the rowid, so an old row
carrying id=41 costs nothing. In Postgres the identity sequence does not know about 41, so the
next id-less insert tries 41 again and dies with a UNIQUE violation - during a grading run, days
after the migration looked completely fine.

Re-runnable: existing rows are superseded on their primary key, so a second pass after a
handful of nights is safe rather than a duplicate-data incident.
"""
from __future__ import annotations

import sqlite3
import sys

TABLES = [
    # parents before children: the FKs are enforced, and Postgres (unlike SQLite by default)
    # will refuse a child row whose parent has not landed yet.
    ("config", ["key"]),
    ("season", ["id"]),
    ("evening", ["id"]),
    ("question", ["id"]),
    ("entry", ["id"]),
    ("submission", ["id"]),
    ("award", ["id"]),
    ("ledger", ["id"]),
    ("hall_of_fame", ["id"]),
    ("payout", ["id"]),
    ("coin_txn", ["id"]),
    ("appeal", ["id"]),
    ("audit", ["id"]),
]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src_path, dst_url = sys.argv[1], sys.argv[2]

    try:
        import psycopg
        import psycopg.rows
    except ImportError:
        print("needs psycopg:  pip install 'psycopg[binary]==3.2.10'")
        return 1

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    # the shared db module owns both the schema and the schema-vs-engine translation, so this
    # script cannot drift into creating a second, slightly different database shape
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "bot"))
    import db as D

    pg = D.connect(dst_url)                 # creates SCHEMA + applies MIGRATIONS on the way in
    total = 0
    for table, pk in TABLES:
        order = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        have = set(order)
        if not have:
            print(f"  - {table:14} not in the source file, skipped")
            continue
        # only columns that exist on BOTH sides: a source DB from an older release may be
        # missing a migrated column, and copying a column the target lacks is a hard error
        cols = [c for c in order if c in pg.columns(table)]
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if not rows:
            print(f"  · {table:14} empty")
            continue
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in pk)
        sql = (f"INSERT INTO {table}({', '.join(cols)}) VALUES({marks})"
               f" ON CONFLICT({', '.join(pk)}) DO UPDATE SET {updates}"
               if updates else
               f"INSERT INTO {table}({', '.join(cols)}) VALUES({marks}) ON CONFLICT({', '.join(pk)}) DO NOTHING")
        with pg:
            for r in rows:
                pg.execute(sql, tuple(r[c] for c in cols))
        n = pg.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        total += len(rows)
        print(f"  ✓ {table:14} {len(rows):5} rows copied   (table now holds {n})")

    seq = pg.sync_sequences()
    print(f"\nsequences re-aligned on {seq} tables (this is the step that gets forgotten)")

    # counts must agree or the copy is not a copy
    bad = []
    for table, _ in TABLES:
        try:
            s = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        d = pg.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        if s != d:
            bad.append(f"{table}: source {s} != postgres {d}")
    if bad:
        print("ROW COUNT MISMATCH:\n  " + "\n  ".join(bad))
        print("Do not start a season on this database - finish the copy first.")
        return 1
    print(f"row counts verified equal on every table ({total} rows)")
    print("\nNow set HUB_DB to that postgres:// URL on Railway and redeploy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
