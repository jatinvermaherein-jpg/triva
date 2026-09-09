"""Run the suites against a real Postgres instead of a temp file.

`HUB_TEST_DB=postgres://... python3 run_tests.py` re-points every suite: each `_fresh()`
normally deletes a .db file, and here it drops and recreates the schema instead. The point is
not decoration - the SQLite suites cannot see a dialect bug, and a Supabase deploy is exactly
where a "works on my machine" schema error becomes a season wiped mid-flight.

Each _fresh() gets its own clean schema because tests hold *two* connections at once (the
restart-persistence checks), so sharing one schema between fixtures would deadlock the intent.
"""
from __future__ import annotations

import os

ENV = "HUB_TEST_DB"


def pg_url() -> str | None:
    u = (os.environ.get(ENV) or "").strip()
    return u if u.startswith(("postgres://", "postgresql://")) else None


def reset(url: str) -> None:
    import psycopg
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        # Supabase grants its members a *schema* create right, not a database one; recreating
        # without GRANT leaves a role that can connect but cannot create tables.
        cur.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")


def _per_fixture(url: str, path) -> str:
    """Give every fixture file its own database: hub_test.db -> <base>_hub_test.

    Without this, two fixtures that each hold an open connection would drop the same shared
    schema out from under one another, and a suite would fail on state it never wrote. SQLite
    gets that isolation for free from separate files; Postgres needs it spelled out.
    """
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    slug = ("_" + name.split(".")[0].replace("-", "_")) if name else ""
    head, _, tail = url.rpartition("/")
    if "?" in tail:
        base, _, opts = tail.partition("?")
        return f"{head}/{base}{slug}?{opts}"
    return f"{head}/{base_part(tail)}{slug}"


def base_part(tail: str) -> str:
    return tail or "hub_test"


def fresh(path) -> str | None:
    """Return the URL to use instead of `path`, or None to keep the file path."""
    url = pg_url()
    if not url:
        return None
    target = _per_fixture(url, path)
    _ensure_db(target)
    reset(target)
    return target


def _ensure_db(url: str) -> None:
    """Create the fixture database if it is missing (connect to the maintenance db)."""
    import psycopg
    from urllib.parse import urlparse
    u = urlparse(url if url.startswith("postgres") else "postgres:" + url)
    maint = url.rsplit("/", 1)[0] + "/postgres"
    dbname = (u.path or "/").lstrip("/")
    with psycopg.connect(maint, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if not cur.fetchone():
            cur.execute('CREATE DATABASE "' + dbname + '"')
