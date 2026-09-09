"""SQLite/Postgres parity suite.

Runs one complete season flow twice - once on a temp SQLite file, once on Postgres when
HUB_TEST_DB is set - and asserts the two agree on every number a player or staff member can
see. Dialect drift is the failure mode this repo cannot afford: the same code path returning a
different leaderboard depending on where the DB lives is indistinguishable from a rigging bug
to 200 players.

Run:  python3 tests/test_pg_parity.py
      HUB_TEST_DB=postgres://user:pw@host:6543/db python3 tests/test_pg_parity.py
Without HUB_TEST_DB it still runs (SQLite vs SQLite) so the file is never dead weight, and it
says so loudly rather than pretending to have covered Postgres.
"""
import asyncio
import datetime as dt
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(ROOT / "tests"))

import dbtarget  # noqa: E402
import discord  # noqa: E402
import services as V  # noqa: E402
import db as D  # noqa: E402

ok = 0
fails = []


def check(name, cond, detail=""):
    global ok
    if not cond:
        fails.append(name)
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    ok += 1
    print(f"pass  {name}")


def season_flow(conn):
    """Everything that touches SQL, from provisioning to payout. Returns comparable numbers."""
    D.set_cfg(conn, "answer_seconds", 120)
    D.set_cfg(conn, "standings_night_floor", 0)
    res = V.create_season(conn, "Season 1", "2026-09-14", weeks=1)
    ev = conn.execute("SELECT * FROM evening WHERE league='l1' AND day='2026-09-14'").fetchone()
    V.open_evening(conn, ev["id"], 1, 1)
    q1 = V.add_question(conn, ev["id"], 1, "Which mech is a sniper?", ["Raijin", "Artemis", "Other"], "easy")
    q2 = V.add_question(conn, ev["id"], 2, "Best % counter?", ["Shield", "Cover", "None"], "medium")
    for qid, ans, player, grade in ((q1["question_id"], 0, 100, 0), (q2["question_id"], 1, 100, 1)):
        V.submit_answer(conn, qid, player, ans)
    # a duplicate tap must become an EDIT, not a second row, and not an error
    dup = V.submit_answer(conn, q1["question_id"], 100, 1)
    check("duplicate submission is an edit on this backend", dup["change"] == "edited", str(dup))
    check("edited answer is the one stored",
          conn.execute("SELECT option_idx FROM entry WHERE question_id=? AND player_id=?",
                       (q1["question_id"], 100)).fetchone()["option_idx"] == 1)
    V.submit_answer(conn, q1["question_id"], 200, 0)
    V.submit_answer(conn, q2["question_id"], 200, 1)
    # actor_id second, per the real signature: grade_question(qid, correct_option, actor_id, ...)
    g1 = V.grade_question(conn, q1["question_id"], 1, 1)
    g2 = V.grade_question(conn, q2["question_id"], 1, 1)
    board = V.standings(conn, scope="season", limit=10)
    ledg = conn.execute("SELECT player_id, points FROM ledger ORDER BY player_id").fetchall()
    V.queue_payouts(conn, res["season_id"])
    pend = V.pending_payouts(conn)
    audit_n = conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]
    awards = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(points),0) p FROM award").fetchone()
    return {
        "season_id": res["season_id"],
        "evenings": res["evenings"],
        "graded": [g1.get("accepted"), g2.get("accepted")],
        "ledger": [(r["player_id"], r["points"]) for r in ledg],
        "awards": (awards["c"], awards["p"]),
        "board": [(r["player_id"], r["points"]) for r in board],
        "audit_rows": audit_n,
        "payout_total": sum(p.get("amount", 0) for p in pend) if pend else 0,
    }


def _clean(path):
    for suf in ("", "-wal", "-shm", "-journal"):
        p = pathlib.Path(str(path) + suf)
        if p.exists():
            p.unlink()
    return path


def main() -> int:
    (ROOT / ".pytest_tmp").mkdir(exist_ok=True)
    sq = D.connect(_clean(ROOT / ".pytest_tmp" / "parity_a.db"))
    a = season_flow(sq)

    url = dbtarget.pg_url()
    if not url:
        print("\n(note) HUB_TEST_DB not set: comparing two SQLite databases. The parity\n"
              "       assertions below still run (they catch a broken flow), but dialect drift\n"
              "       is only visible against a real server. Use HUB_TEST_DB=postgres://...")
        pg = D.connect(_clean(ROOT / ".pytest_tmp" / "parity_b.db"))
    else:
        pg = D.connect(dbtarget.fresh(pathlib.Path("parity_pg.db")))
    b = season_flow(pg)

    check("same season_id allocation", a["season_id"] == b["season_id"], f"{a['season_id']} vs {b['season_id']}")
    check("same evening count", a["evenings"] == b["evenings"], f"{a['evenings']} vs {b['evenings']}")
    check("same grading outcome", a["graded"] == b["graded"], f"{a['graded']} vs {b['graded']}")
    check("same ledger rows", a["ledger"] == b["ledger"], f"{a['ledger']} vs {b['ledger']}")
    check("same award count and total", a["awards"] == b["awards"], f"{a['awards']} vs {b['awards']}")
    check("same leaderboard order and points", a["board"] == b["board"], f"{a['board']} vs {b['board']}")
    check("same audit trail size", a["audit_rows"] == b["audit_rows"],
          f"{a['audit_rows']} vs {b['audit_rows']}")
    check("same payout total", a["payout_total"] == b["payout_total"],
          f"{a['payout_total']} vs {b['payout_total']}")

    # a literal '%' must survive as data on Postgres (psycopg parses % as a placeholder marker)
    pct = pg.execute("SELECT COUNT(*) c FROM question WHERE prompt LIKE 'Best % counter?'").fetchone()["c"]
    check("literal % in a LIKE works", pct == 1, str(pct))

    if url:
        # the connection is the one thing a container can lose mid-night; prove it recovers
        pg._c.close()
        n = pg.execute("SELECT COUNT(*) c FROM ledger").fetchone()["c"]
        check("dropped connection reconnects and serves the next statement", n == len(b["ledger"]), str(n))
        print("      (reconnect exercised against the live server)")

    print(f"\n{ok} parity checks passed" + ("" if url else " (single-backend)"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
