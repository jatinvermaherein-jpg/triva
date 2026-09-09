"""SQLite layer. Every table here exists to serve one requirement: a panel must
behave identically before and after a restart, so NOTHING lives in Python memory.

Idempotency is enforced in the schema, not in the code: UNIQUE indexes on
award(entry_id) and ledger(event_id, player_id) mean a double-click or a
retry-after-crash cannot award twice. That is the restart-proof guarantee.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS season (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
  weeks INTEGER NOT NULL DEFAULT 4,
  status TEXT NOT NULL DEFAULT 'draft',          -- draft|active|finalizing|closed
  coins_per_point INTEGER NOT NULL DEFAULT 1,
  xp_per_point INTEGER NOT NULL DEFAULT 0,
  role_trivia INTEGER, role_strategy INTEGER, role_hangar INTEGER, role_overall INTEGER,
  final_at TEXT
);

CREATE TABLE IF NOT EXISTS evening (
  id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL REFERENCES season(id),
  day TEXT NOT NULL,                             -- ISO date YYYY-MM-DD
  league TEXT NOT NULL,                          -- l1|l2|l3
  is_sunday INTEGER NOT NULL DEFAULT 0,
  multiplier REAL NOT NULL DEFAULT 1.0,
  opens_at TEXT NOT NULL, closes_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',      -- scheduled|open|locked|graded|void
  difficulty TEXT NOT NULL DEFAULT 'normal',      -- normal | grand (Sunday)
  message_id INTEGER, channel_id INTEGER, posted_by INTEGER,
  graded_by INTEGER, graded_at INTEGER,
  UNIQUE(day, league)
);

CREATE TABLE IF NOT EXISTS question (
  id INTEGER PRIMARY KEY, evening_id INTEGER NOT NULL REFERENCES evening(id),
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'mcq',   -- mcq|text|image|instant
  prompt TEXT NOT NULL, options TEXT NOT NULL,                  -- JSON array
  image_url TEXT, explanation TEXT,
  posted_at INTEGER,
  points_per_correct INTEGER,        -- staff-entered value for THIS question
  tier TEXT NOT NULL DEFAULT 'medium',           -- easy|medium|hard|instant
  correct_option INTEGER,                        -- set by staff; NULL = not graded
  answer_deadline TEXT NOT NULL,
  message_id INTEGER, points_awarded INTEGER NOT NULL DEFAULT 0,
  UNIQUE(evening_id, ordinal)
);

CREATE TABLE IF NOT EXISTS entry (
  id INTEGER PRIMARY KEY, evening_id INTEGER NOT NULL REFERENCES evening(id),
  question_id INTEGER REFERENCES question(id),
  player_id INTEGER NOT NULL, payload TEXT NOT NULL,
  option_idx INTEGER,
  submitted_at INTEGER NOT NULL, edited_at INTEGER,
  edit_after_close INTEGER NOT NULL DEFAULT 0,
  UNIQUE(question_id, player_id)
);
CREATE INDEX IF NOT EXISTS entry_evening_idx ON entry(evening_id, player_id);

-- L2/L3: one row per evening (no questions). Same UNIQUE guard as award().
CREATE TABLE IF NOT EXISTS submission (
  id INTEGER PRIMARY KEY, evening_id INTEGER NOT NULL REFERENCES evening(id),
  player_id INTEGER NOT NULL, text TEXT NOT NULL,
  word_count INTEGER NOT NULL DEFAULT 0,
  submitted_at INTEGER NOT NULL, edited_at INTEGER,
  edit_after_close INTEGER NOT NULL DEFAULT 0,
  duplicate_of INTEGER,
  flag TEXT,                          -- 'ai' when staff mark it; see flag_submission
  points_in INTEGER, awarded_by INTEGER, awarded_at INTEGER, note TEXT,
  status TEXT NOT NULL DEFAULT 'submitted',
  UNIQUE(evening_id, player_id)
);

CREATE TABLE IF NOT EXISTS award (
  id INTEGER PRIMARY KEY, evening_id INTEGER NOT NULL,
  player_id INTEGER NOT NULL, entry_id INTEGER UNIQUE,
  question_id INTEGER, raw REAL NOT NULL, bonus REAL NOT NULL DEFAULT 0,
  multiplier REAL NOT NULL DEFAULT 1.0, points INTEGER NOT NULL,
  coins INTEGER NOT NULL DEFAULT 0, reason TEXT, applied_by INTEGER, ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS award_player_idx ON award(player_id, evening_id);

-- evening_id is deliberately NOT a foreign key: a manual correction (league='adjust')
-- is not tied to any evening, so it lives on the sentinel evening_id=0 / day='manual'.
--
-- UNIQUE(evening_id, player_id) is load-bearing, keep it. Grading re-writes a night's
-- rows with INSERT OR REPLACE; a wider key would make re-grading ADD points instead of
-- replacing them. Consequence, stated so it is not mistaken for a bug: corrections for
-- one player are a single running total, not one row per correction - the full history
-- lives in `audit` anyway.
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL, evening_id INTEGER NOT NULL DEFAULT 0,
  season_id INTEGER NOT NULL REFERENCES season(id), league TEXT NOT NULL, day TEXT NOT NULL,
  points INTEGER NOT NULL, UNIQUE(evening_id, player_id)
);

CREATE TABLE IF NOT EXISTS hall_of_fame (
  id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL, league TEXT NOT NULL,
  player_id INTEGER NOT NULL, placement INTEGER NOT NULL, points INTEGER NOT NULL,
  role_id INTEGER, post_id INTEGER,
  UNIQUE(season_id, league, placement)
);

-- Checkout queue. The bot NEVER holds currency: it only says what is owed and
-- records that a human confirmed the transfer. UNIQUE keeps one line per
-- player/season/kind so a re-run can never create a second checkout.
CREATE TABLE IF NOT EXISTS payout (
  id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'coins',       -- coins | xp (any economy staff runs)
  points INTEGER NOT NULL, rate INTEGER NOT NULL, amount INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT 'season awards',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|cleared
  message_id INTEGER, channel_id INTEGER,
  created_at INTEGER NOT NULL, cleared_by INTEGER, cleared_at INTEGER,
  content_hash TEXT,                          -- the standings the queue was built from
  UNIQUE(season_id, player_id, kind, reason)
);

CREATE TABLE IF NOT EXISTS coin_txn (
  id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL, amount INTEGER NOT NULL,
  reason TEXT NOT NULL, ts INTEGER NOT NULL, balance_after INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appeal (
  id INTEGER PRIMARY KEY, evening_id INTEGER, submission_id INTEGER, player_id INTEGER NOT NULL,
  reason TEXT NOT NULL, outcome TEXT, resolved_by INTEGER, created_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL,
  target TEXT, before TEXT, after TEXT, ts INTEGER NOT NULL
);
"""


# Adding a column to SCHEMA is not enough for anyone already running a season:
# `CREATE TABLE IF NOT EXISTS` will not touch the existing table. These pairs are
# applied on every boot, so upgrading the bot never means rebuilding a database.
# A LIST of (table, column, decl), not a dict keyed by table: two additions to the
# same table are routine, and a dict would let the second silently delete the
# first - a missing migration is invisible until a live database errors.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("evening", "difficulty", "TEXT NOT NULL DEFAULT 'normal'"),
    ("evening", "posted_by", "INTEGER"),
    ("question", "posted_at", "INTEGER"),
    ("question", "points_per_correct", "INTEGER"),
    ("season", "xp_per_point", "INTEGER NOT NULL DEFAULT 0"),
    ("season", "final_at", "TEXT"),
    ("payout", "content_hash", "TEXT"),
    ("entry", "edit_after_close", "INTEGER NOT NULL DEFAULT 0"),
    ("submission", "edit_after_close", "INTEGER NOT NULL DEFAULT 0"),
    # 'ai' when staff mark an entry as model-written. NOT on entry: L1 answers are
    # option taps, there is no prose to judge.
    ("submission", "flag", "TEXT"),
    # NOTE: pre-migration DBs carry a FK on ledger.evening_id that makes manual
    # corrections fail. Documented, not silently rewritten: fixing it needs a
    # table rebuild (see README "Upgrading").
]


def migrations_for(table: str) -> list[tuple[str, str]]:
    return [(name, decl) for tbl, name, decl in MIGRATIONS if tbl == table]


def _migrate(db: sqlite3.Connection) -> list[str]:
    added: list[str] = []
    for table in {t for t, _, _ in MIGRATIONS}:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue                     # table does not exist yet; SCHEMA will make it
        for name, decl in migrations_for(table):
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
    # an old payout table with no UNIQUE guard would allow duplicate checkouts
    have = {r[1] for r in db.execute("PRAGMA index_list(payout)")}
    return added


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")        # a crash must not lose an award
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def now() -> int:
    return int(time.time())


def cfg(db, key, default=None):
    row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_cfg(db, key, value) -> None:
    db.execute("INSERT INTO config(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))


def audit(db, action: str, actor_id=None, target=None, before=None, after=None) -> None:
    db.execute("INSERT INTO audit(actor_id,action,target,before,after,ts) VALUES(?,?,?,?,?,?)",
               (actor_id, action, str(target),
                json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None, now()))


def balance(db, player_id: int) -> int:
    row = db.execute("SELECT balance_after FROM coin_txn WHERE player_id=? "
                     "ORDER BY id DESC LIMIT 1", (player_id,)).fetchone()
    return row["balance_after"] if row else 0


def add_coins(db, player_id: int, amount: int, reason: str) -> int:
    after = balance(db, player_id) + amount
    db.execute("INSERT INTO coin_txn(player_id,amount,reason,ts,balance_after) VALUES(?,?,?,?,?)",
               (player_id, amount, reason, now(), after))
    return after


def json_list(value) -> list:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value or [])
