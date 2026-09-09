#!/usr/bin/env python3
"""Everything a `HUB_DB` paste can contain, plus the two boot paths that only fire on a
real deploy. This suite exists because the first live Railway deploy crash-looped on an
`invalid connection option "database"` and then on a `NameError` - and neither was
reachable from a fake.

Run standalone (SQLite) or under HUB_TEST_DB for the Postgres-only checks:
    python3 tests/test_deploy.py
    HUB_TEST_DB=postgres://... python3 tests/test_deploy.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "tests"))

ok = fails = 0


def check(label, cond, got=""):
    global ok, fails
    if cond:
        ok += 1
        print(f"  pass  {label}")
    else:
        fails += 1
        print(f"  FAIL  {label}" + (f"  [got: {got}]" if got else ""))


def _raise(D, v):
    """The exception connect() raises for `v`, or None - an unreadable paste must be an
    OperationalError with a fix in it, never a traceback out of the argument parser."""
    try:
        D.connect(v)
    except Exception as e:
        return e
    return None


def main() -> int:
    import db as D

    # ---------------------------------------------------------------- pastes
    # Supabase's "Database connection string" JSON uses the key `database`; libpq's
    # conninfo grammar does not accept it. Handing the paste through unchanged is what
    # killed the first deploy, so each shape is asserted at the psycopg boundary.
    def norm(v):
        try:
            return D._normalise(v) or ""
        except D.OperationalError as e:
            return "RAISE:" + str(e)

    check("URI passes through intact",
          norm("postgres://u:p@h:6543/db?sslmode=require")
          == "postgres://u:p@h:6543/db?sslmode=require",
          norm("postgres://u:p@h:6543/db?sslmode=require"))
    check("?database= is dropped, never forwarded",
          "database" not in norm("postgresql://u:p@h:6543/db?sslmode=require&database=x"))
    check("path db wins over a contradicting ?database=",
          norm("postgresql://u:p@h:6543/mydb?database=postgres").startswith(
              "postgres://u:p@h:6543/mydb"),
          norm("postgresql://u:p@h:6543/mydb?database=postgres"))
    check("?db= alias too",
          "db=" not in norm("postgresql://u:p@h:6543/db?db=other"))
    kw = norm("host=h port=5432 database=hubseason user=u password=pw sslmode=require")
    check("keyword string aliases database -> dbname", "dbname=hubseason" in kw, kw)
    check("keyword string keeps no unknown keys", "database=" not in kw, kw)
    blob = ('{"host":"aws.pooler.supabase.com","port":6543,"database":"postgres",'
            '"user":"postgres.abc","password":"pw","ssl":"true",'
            '"driver":"node-postgres","max_connections":20,"description":"x"}')
    n = norm(blob)
    check("Supabase JSON builds a URI", n.startswith("postgres://") and "?sslmode=require" in n, n)
    check("Supabase JSON never emits a bare database key", "database" not in n, n)
    check("pathlib-mangled scheme is restored",
          norm("postgres:/u@h:5432/db").startswith("postgres://u@h:5432/db"),
          norm("postgres:/u@h:5432/db"))
    check("a file path is still a file path", norm("hub.db") == "")
    check("whitespace-only is a file path", norm("   ") == "")
    check("garbage JSON refuses loudly", norm('{"foo":"bar"}').startswith("RAISE:"))
    check("a bad paste names the fix", "Connection URI" in norm('{"host":"h"}'),
          norm('{"host":"h"}'))
    check("detection never raises, only classifies",
          D.is_pg_target('{"foo":"bar"}') is False and D.is_pg_target('{"host":"h"}') is True
          and D.is_pg_target("hub.db") is False and D.is_pg_target(blob) is True)
    check("connect() turns an unreadable paste into a one-line error",
          isinstance(_raise(D, '{"foo":"bar"}'), D.OperationalError))
    # Supabase's console writes `ssl=true`, which is neither a conninfo keyword nor a
    # legal sslmode value; libpq answers "invalid sslmode value" and the boot dies again.
    k = norm('host=h port=6543 dbname=d user=u password=p ssl=true')
    check("ssl=true becomes sslmode=require", "sslmode=require" in k and "ssl=true" not in k, k)
    k2 = norm('host=h dbname=d user=u sslmode=disable')
    check("a legitimate sslmode survives untouched", "sslmode=disable" in k2, k2)
    u = norm("postgresql://u@h:5432/db?sslmode=require&ssl=true&connect_timeout=10")
    check("URI query: ssl dropped, sslmode kept, timeout kept",
          "ssl=true" not in u and "sslmode=require" in u and "connect_timeout=10" in u, u)
    k3 = norm('host=h dbname=d user=u sslmode=bogles')
    check("an invented sslmode is dropped, not forwarded",
          "sslmode" not in k3, k3)
    check("verify-full, a real mode, is kept",
          "sslmode=verify-full" in norm('host=h dbname=d user=u sslmode=verify-full'))
    # detection must be total (it runs before anything can catch) and resolution
    # detailed. A value that is neither is treated as a file name, and no JSON paste
    # with a host in it can ever be mistaken for one.
    for v, want in [("postgres://u@h/db", True), (kw, True), (blob, True),
                    ("hub.db", False), ("", False), ('{"a":1}', False),
                    (pathlib.Path("hub.db"), False)]:
        check(f"is_pg_target({str(v)[:34]!r}) is {want}", D.is_pg_target(v) is want,
              str(D.is_pg_target(v)))
    check("_normalise agrees for every pg-shaped value",
          all(bool(norm(v)) for v in ["postgres://u@h/db", kw, blob]))

    # ------------------------------------------------- the PgBouncer prepared-statement trap
    # prepare_threshold=0 does NOT disable prepared statements (psycopg's guard is
    # `is None`); 0 means "prepare on the first execution". Under a transaction pooler
    # those statements outlive the process and the next boot dies before it can log in.
    fake = _FakePsycopg()
    _run_with_fake(fake, D)
    check("connect() disables prepared statements entirely",
          fake.kwargs.get("prepare_threshold", "missing") is None,
          repr(fake.kwargs.get("prepare_threshold")))
    check("connect() keeps autocommit", fake.kwargs.get("autocommit") is True)

    # --------------------------------------------------------- boot paths a fake never hits
    # HubBot.setup_hook runs only once discord.py logs in. A persistent view defined
    # inside build_tree() was a module-level NameError there - the whole bot crash-looped.
    try:
        import main as M
    except Exception as e:
        check("bot/main.py imports", False, f"{type(e).__name__}: {e}")
        return 1
    check("bot/main.py imports", True)
    for name in ("_SetupAskView", "HubBot", "build_tree", "main"):
        check(f"{name} resolves at module scope", hasattr(M, name))
    views = _drive_setup_hook(M)
    check("setup_hook registers /setup's persistent view", "_SetupAskView" in views,
          "registered: " + ", ".join(sorted(views)))
    check("every persistent view ui.install() promised is on the bot",
          {"AnswerView", "SubmitView", "HubPanelView"} <= views, str(sorted(views)))

    # A bad HUB_DB must produce a diagnosis, not a traceback: Railway shows the first
    # lines of a container that restarts every second, and a stack of `raise ... from exc`
    # frames teaches a volunteer nothing about which variable is wrong.
    import subprocess
    env = {**os.environ, "HUB_DB": "postgresql://u:p@127.0.0.1:1/postgres?sslmode=disable",
           "HUB_TOKEN": "", "CONNECT_TIMEOUT": "2"}
    r = subprocess.run([sys.executable, str(ROOT / "bot" / "main.py"), "--check"],
                       capture_output=True, text=True, timeout=90, env=env, cwd=str(ROOT))
    out = r.stdout + r.stderr
    check("a refused connection exits non-zero", r.returncode != 0, str(r.returncode))
    check("a refused connection prints no traceback", "Traceback" not in out,
          out.strip().splitlines()[-1][:120] if out.strip() else "(no output)")
    check("a refused connection says the database could not be opened",
          "could not open its database" in out, out[:200])

    # ------------------------------------------------------------ only with a live server
    url = (sys.argv[1] if len(sys.argv) > 1 else "") or ""
    if url.startswith("postgres"):
        live = D.connect(url)
        for _ in range(6):
            live._raw("SELECT table_name FROM information_schema.tables LIMIT 3")
        n = live._raw("SELECT count(*) n FROM pg_prepared_statements")[0]["n"]
        check("live server holds zero server-side prepared statements", n == 0, str(n))
        n = _prepared_after_repeats(url, 0)
        check("…and the probe is sensitive: the shipped value used to leak statements",
              n > 0, f"{n} with prepare_threshold=0")
        check("…and None is the value that stops it",
              _prepared_after_repeats(url, None) == 0)
        d = _prepared_after_repeats(url, "default")
        print(f"      (psycopg's own default would have prepared {d} - the trap only "
              f"fires on the 6th repetition, which is why it hid until a real deploy)")
        with live:
            live.execute("INSERT INTO config(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         ("deploy_probe", "1"))
        check("a row survives on the live backend",
              live.execute("SELECT value FROM config WHERE key='deploy_probe'").fetchone()["value"]
              == "1")
        # The reconnect path is a *second* psycopg.connect() call, so it can carry a
        # different (broken) setting from __init__ - and `_raw` deliberately bypasses the
        # reconnect logic, which is why this used to be a check that could not fail.
        # `execute()` is what goes through _ensure_conn().
        live._c.close()
        got = live.execute("SELECT value FROM config WHERE key='deploy_probe'").fetchone()["value"]
        check("execute() reconnects after a dropped connection and still serves the row",
              got == "1", repr(got))
        conns = live._raw("SELECT count(*) n FROM pg_stat_activity "
                          "WHERE application_name='hub-knowledge-season' "
                          "AND state <> 'idle'")[0]["n"]
        check("the reconnect did not leave a second session behind", conns <= 1, str(conns))
        check("the reconnected session prepares nothing either",
              live._raw("SELECT count(*) n FROM pg_prepared_statements")[0]["n"] == 0)
        live.close()
        print("      (live checks exercised against the real server)")
    else:
        print("      (no HUB_TEST_DB - live-server checks skipped)")

    print(f"\n{ok} deploy checks passed" + ("" if fails else ", 0 failed"))
    return 1 if fails else 0


class _FakeCursor:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a): pass
    description = None
    def fetchall(self): return []


class _FakeConn:
    closed = False
    def cursor(self): return _FakeCursor()
    def close(self): pass


class _FakePsycopg:
    """Stands in for the psycopg module so connect() can be called with no server.
    Only reaches the `import psycopg` inside PgConnection.__init__."""
    def __init__(self): self.kwargs = {}
    def connect(self, conninfo, **kw):
        self.kwargs = kw
        return _FakeConn()


def _run_with_fake(fake, D):
    """Install a stub `psycopg` in sys.modules, open a PgConnection, remove it.

    Deliberately does not go through D.connect(): that would run executescript(SCHEMA)
    against the fake. PgConnection.__init__ is the exact site the deploy traceback
    pointed at, and it is the only thing that passes psycopg kwargs.
    """
    import types
    mod = types.ModuleType("psycopg")
    mod.connect = fake.connect
    mod.rows = types.SimpleNamespace(dict_row=object())
    mod.Error = Exception
    saved = sys.modules.get("psycopg")
    sys.modules["psycopg"] = mod
    try:
        D.PgConnection("host=h dbname=d user=u")
    finally:
        if saved is not None:
            sys.modules["psycopg"] = saved
        else:
            sys.modules.pop("psycopg", None)


def _prepared_after_repeats(url, threshold):
    """Statements the server is holding after 6 identical queries at `threshold`.

    Straight psycopg, not db.connect(): this measures the *setting*, and it is the only
    way to show that 0 means "prepare everything" while None means "never". Six
    repetitions because psycopg's default of 5 prepares on the sixth - the reason a
    short-lived test can pass while a day-old deploy dies.
    """
    import psycopg
    kw = {} if threshold == "default" else {"prepare_threshold": threshold}
    c = psycopg.connect(url, autocommit=True, **kw)
    try:
        with c, c.cursor() as cur:
            for _ in range(6):
                cur.execute("SELECT count(*) FROM config")
            return cur.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0]
    finally:
        c.close()


def _drive_setup_hook(M):
    """Run HubBot.setup_hook against a stand-in that supplies only what it touches.

    setup_hook needs no gateway: it registers views, reads two config keys, spawns the
    clock. That is exactly the code path that NameError'd on the real deploy, and it is
    reachable without discord.py ever connecting - so it is worth standing on.
    """
    import asyncio
    import ui

    class Fake:
        hub_channel_id = None

        def __init__(self):
            self.views = []
            self.conn = _MiniCfg()

        def add_view(self, v): self.views.append(v)
        async def backfill(self): pass
        async def clock(self): pass      # spawned, never awaited here

        class _L:
            def create_task(self, coro):
                coro.close()          # never scheduled - close it, no RuntimeWarning
        loop = _L()

    f = Fake()
    orig_bind = ui.bind_bot
    ui.bind_bot = lambda bot: None          # no channel to refresh yet
    try:
        asyncio.run(M.HubBot.setup_hook(f))
    finally:
        ui.bind_bot = orig_bind
    return {type(v).__name__ for v in f.views}


class _MiniCfg:
    """The least a connection can be: every query answers "no rows", every write succeeds."""
    def execute(self, sql, params=None):
        class R:
            description = [("value",)]
            def fetchone(self): return {"c": 0} if "COUNT" in sql.upper() else None
            def fetchall(self): return []
            def __iter__(self): return iter(())
        return R()

    def close(self): pass


if __name__ == "__main__":
    import os
    sys.argv.append((os.environ.get("HUB_TEST_DB") or "").strip())
    raise SystemExit(main())
