"""Async-seam suite: proves the event loop never waits on the database.

The production failure this suite exists for:

    WARNING discord.gateway: Shard ID None heartbeat blocked for more than 10 seconds.

with the loop traceback ending inside psycopg's socket wait (a `RELEASE SAVEPOINT`
that never came back). Every blocking database call in the async surface now goes
through db.acall(), which runs Postgres work in a worker thread; these checks prove
the two halves of that claim, and the connect-time safety net that bounds a stall.

No Postgres server is needed: acall only asks isinstance(conn, PgConnection), so the
Postgres branch is exercised with object.__new__ - a connection object whose execute
is replaced by a SLEEP, which is exactly what a stalled WAN round trip looks like.

Run:  python3 tests/test_async_seam.py
"""
import asyncio
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import db as D                 # noqa: E402

ok = 0


def check(name, cond, detail=""):
    global ok
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    ok += 1
    print(f"pass  {name}")


# --------------------------------------------------------------------------- #
# 1. the connect-time safety net
# --------------------------------------------------------------------------- #

class _FakePsycopg:
    """Just enough of psycopg for _pg_connect: record the kwargs, hand back a token."""
    class rows:
        dict_row = "dict_row"

    def __init__(self):
        self.got = None

    def connect(self, conninfo, **kw):
        self.got = (conninfo, kw)
        return "<conn>"


fake = _FakePsycopg()
out = D._pg_connect(fake, "postgresql://u@h:5432/db")
conninfo, kw = fake.got
check("_pg_connect forwards the conninfo and returns the connection",
      out == "<conn>" and conninfo == "postgresql://u@h:5432/db", repr(fake.got))
check("reconnects are bounded: connect_timeout=10", kw.get("connect_timeout") == 10, str(kw))
check("dead links are detected: TCP keepalives on a ~1-minute fuse",
      kw.get("keepalives") == 1 and kw.get("keepalives_idle") == 30
      and kw.get("keepalives_interval") == 10 and kw.get("keepalives_count") == 3, str(kw))
check("prepared statements stay disabled (pooler-safe)", kw.get("prepare_threshold") is None,
      str(kw))
check("autocommit + sqlite-shaped transactions preserved", kw.get("autocommit") is True, str(kw))

# --------------------------------------------------------------------------- #
# 2. acall routes by backend
# --------------------------------------------------------------------------- #

conn_pg = object.__new__(D.PgConnection)      # isinstance says Postgres; no server attached
seen_threads = []


def pg_work():
    seen_threads.append(threading.get_ident())
    return 42


loop_thread_holder = {}


async def _route_capturing():
    loop_thread_holder["tid"] = threading.get_ident()
    return await D.acall(conn_pg, pg_work)


result = asyncio.run(_route_capturing())
check("acall returns the worker's result on the Postgres backend", result == 42, str(result))
check("the work ran OFF the event-loop thread",
      seen_threads and seen_threads[-1] != loop_thread_holder["tid"],
      f"worker={seen_threads} loop={loop_thread_holder}")


class _Boom(Exception):
    pass


def boom():
    raise _Boom("grading refuses politely")


async def _exc():
    try:
        await D.acall(conn_pg, boom)
    except _Boom as e:
        return str(e)
    return None


check("a worker exception reaches the awaiter unchanged",
      asyncio.run(_exc()) == "grading refuses politely")

# sqlite stays inline: a local file costs microseconds and needs no thread hop
tmp = ROOT / "tests" / "_seam_tmp.db"
for suffix in ("", "-wal", "-shm", "-journal"):
    p = pathlib.Path(str(tmp) + suffix)
    if p.exists():
        p.unlink()
conn_lite = D.connect(tmp)
inline_threads = []


def lite_probe():
    inline_threads.append(threading.get_ident())
    return D.cfg(conn_lite, "never_set", "fallback")


async def _inline():
    return await D.acall(conn_lite, lite_probe)


val = asyncio.run(_inline())
check("the SQLite backend runs inline (same thread, no executor)",
      val == "fallback" and inline_threads
      and inline_threads[0] == threading.get_ident(),
      f"{val} {inline_threads}")

# --------------------------------------------------------------------------- #
# 3. the heartbeat property: a stalled database must not stop the loop
# --------------------------------------------------------------------------- #

STALL_S = 1.0


def stalled():
    """A WAN round trip that hangs - the exact shape of the production incident."""
    time.sleep(STALL_S)
    return "done"


async def _with_seam():
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    hb = asyncio.create_task(heartbeat())
    res = await D.acall(conn_pg, stalled)
    hb.cancel()
    return res, ticks


res, ticks = asyncio.run(_with_seam())
check("through acall the loop keeps beating during a 1s database stall",
      res == "done" and ticks >= 10, f"ticks={ticks} (need >=10)")


async def _old_way():
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.05)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)              # let the heartbeat start
    stalled()                           # what the old code did: run it on the loop
    hb.cancel()
    return ticks


blocked = asyncio.run(_old_way())
check("control: the same stall run inline freezes the loop (this is the bug)",
      blocked <= 2, f"ticks={blocked}")

# --------------------------------------------------------------------------- #
# 4. concurrency smoke: several awaited blocks, all of them land
# --------------------------------------------------------------------------- #


async def _load():
    async def worker(n):
        def block():
            with conn_lite:
                for _ in range(25):
                    conn_lite.execute("INSERT INTO audit(actor_id,action,ts) VALUES(?,?,?)",
                                      (n, "seam.load", D.now()))
        await D.acall(conn_lite, block)
    await asyncio.gather(*(worker(i) for i in range(4)))


asyncio.run(_load())
n = conn_lite.execute("SELECT COUNT(*) c FROM audit WHERE action='seam.load'").fetchone()["c"]
check("four concurrent transaction blocks all land intact", n == 100, str(n))

conn_lite.close()
for suffix in ("", "-wal", "-shm", "-journal"):
    p = pathlib.Path(str(tmp) + suffix)
    if p.exists():
        p.unlink()

print(f"\n{ok} async-seam checks passed - the event loop never waits on the database.")
