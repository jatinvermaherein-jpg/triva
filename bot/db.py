"""SQLite layer. Every table here exists to serve one requirement: a panel must
behave identically before and after a restart, so NOTHING lives in Python memory.

Idempotency is enforced in the schema, not in the code: UNIQUE indexes on
award(entry_id) and ledger(event_id, player_id) mean a double-click or a
retry-after-crash cannot award twice. That is the restart-proof guarantee.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

# Canonical text: SQLite spelling. The Postgres path rewrites it at executescript time
# (_pg_schema below) rather than keeping a second copy, because a duplicated schema drifts the
# moment someone edits one half of it and the migration guard cannot see the difference.
# `id INTEGER PRIMARY KEY` means "this IS the rowid, auto-assigned" in SQLite and only
# "int, not null, unique" in Postgres - so the identity clause is not optional there.
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


def _columns(db, table: str) -> set[str]:
    """Column names of a table, in either dialect. Empty if the table is not there yet."""
    if isinstance(db, PgConnection):
        return db.columns(table)
    try:
        return {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
    except sqlite3.Error:
        return set()


def _migrate(db) -> list[str]:
    """Add columns introduced after a season began. Runs on every boot.

    Dialect note: PRAGMA is SQLite-only, so this cannot assume it. A no-op here is not
    harmless - a missing column surfaces as an error deep inside a grading run, at which
    point the operator has no idea the migration list is what failed.
    """
    added: list[str] = []
    for table in {t for t, _, _ in MIGRATIONS}:
        have = _columns(db, table)
        if not have:
            continue                     # table does not exist yet; SCHEMA will make it
        for name, decl in migrations_for(table):
            if name not in have:
                db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + decl)
                added.append(table + "." + name)
    return added


# Keys people actually paste. Supabase's "Database connection string" is JSON, its "URI" is a
# URL, and Railway's own Raw editor accepts a flat {KEY: value} object - so all three shapes
# have to land in the same place. Otherwise an operator pastes a perfectly valid config, the bot
# decides it is not a URL, and quietly writes to a file instead: no error, and the season vanishes
# at the next redeploy, which is the exact failure this backend was added to remove.
_PG_KEYS = ("host", "dbname", "database", "user", "password", "port", "ssl", "sslmode",
            "connectionstring", "connection_string", "url", "uri", "db", "driver", "endpoint")


def _info_to_uri(host, port, dbname, user, password, sslmode) -> str:
    """Build a postgres:// URI. Percent-encoding is what makes Supabase passwords - which are
    full of brackets, spaces and slashes - survivable, where keyword syntax would need quoting."""
    from urllib.parse import quote
    auth = ""
    if user:
        auth = quote(str(user), safe="")
        if password:
            auth += ":" + quote(str(password), safe="")
        auth += "@"
    tail = "" if sslmode in (None, "", "disable", "false", "0") else "?sslmode=" + sslmode
    return "postgres://" + auth + str(host) + ":" + (str(port) or "5432") + "/" + str(dbname) + tail


def _sslmode(raw) -> str:
    if raw is None:
        return "require"                      # Supabase is TLS-only; defaulting to plain is wrong
    s = str(raw).strip().lower()
    if s in ("", "false", "0", "disable", "disabled", "not required", "no"):
        return "disable"
    if s in ("true", "1", "yes", "require", "required"):
        return "require"
    return s                                  # verify-full etc. pass through


def _normalise(target) -> str:
    """HUB_DB -> a string psycopg can connect with, or "" meaning "this is a file path".

    Accepted shapes, because every one of them is something a copy/paste really produces:
      * postgres:// and postgresql:// URIs   (Supabase "Connection URI", Railway raw editor)
      * a scheme mangled by pathlib          ("postgres:/host/db" - Path collapses the "//")
      * Supabase's JSON blob                 ({"host":..,"port":..,"password":..,"db":..})
      * a one-key Railway wrapper            ({"HUB_DB": "postgres://.."})
      * psycopg keyword text                 ("dbname=postgres host=.. user=..")
    A JSON object that is not a connection config is refused loudly rather than guessed at.
    """
    s = str(target or "").strip()
    if not s:
        return ""
    if s[0] in "{[":
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as exc:
            raise OperationalError("HUB_DB looks like JSON but does not parse: " + str(exc)) from exc
        if isinstance(obj, dict) and len(obj) == 1:
            inner = next(iter(obj.values()))
            if isinstance(inner, str) and _normalise(inner):
                return _normalise(inner)
        if not isinstance(obj, dict):
            raise OperationalError(
                "HUB_DB is JSON but not an object of connection fields. Paste Supabase's\n"
                "                \"Connection URI\" (starts with postgresql://) instead.")
        low = {}
        for k, v in obj.items():
            kk = str(k).strip().lower()
            if kk not in ("description", "max_connections", "read_replica_url", "supavisor"):
                low[kk] = v
        if set(low) - set(_PG_KEYS) and not ({"host", "dbname", "database", "user"} & set(low)):
            raise OperationalError(
                "HUB_DB JSON has no host/dbname/user, so it is not a connection string.\n"
                "                Unrecognised keys: " + ", ".join(sorted(set(low) - set(_PG_KEYS))[:6]))
        host = low.get("host") or low.get("endpoint") or ""
        dbname = low.get("dbname") or low.get("database") or low.get("db") or ""
        user = low.get("user") or ""
        for k in ("connectionstring", "connection_string", "url", "uri"):
            if low.get(k):
                return _normalise(low[k])
        missing = [n for n, v in (("host", host), ("dbname", dbname), ("user", user)) if not v]
        if missing:
            raise OperationalError(
                "HUB_DB JSON is missing: " + ", ".join(missing)
                + ". Supabase's \"Connection URI\"\n"
                "                has everything in one line and is the easier paste.")
        # Supabase's JSON user is often "postgres.<project-ref>"; the pooler wants the bare role
        if "." in str(user) and not low.get("password"):
            user = str(user).split(".", 1)[0]
        return _info_to_uri(host, low.get("port"), dbname, user, low.get("password"),
                            _sslmode(low.get("sslmode") if "sslmode" in low else low.get("ssl")))
    m = re.match(r"^(postgres|postgresql)(:/+)(.*)$", s, re.S)
    if m:
        # a scheme mangled by pathlib.Path(), which collapses "//" into "/"
        return m.group(1) + "://" + m.group(3)
    if "://" not in s and re.search(r"(^|\s)(dbname|host|user|password|port|sslmode)=", s):
        return s                                  # psycopg accepts keyword strings natively
    return ""


def is_pg_target(target) -> bool:
    """True when `target` names a Postgres server rather than a file.

    Delegates to _normalise so that "is this Postgres" and "can I connect to it" can never
    disagree - the whole danger of this setting was a value that looked like neither and fell
    through to a filename.
    """
    return bool(_normalise(target))


def connect(target) -> sqlite3.Connection:
    """Open the hub database. `target` is a file path (SQLite) or a postgres:// URL.

    One entry point for both backends is the reason 144 call sites stayed synchronous when
    the store moved to Supabase: nothing outside this function knows which engine it is on.
    """
    conninfo = _normalise(target)
    if conninfo:
        db = PgConnection(conninfo)
        db.executescript(SCHEMA)
        _migrate(db)
        db.sync_sequences()
        return db
    path = str(target)
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")        # a crash must not lose an award
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def is_duplicate(exc: BaseException) -> bool:
    """True for a UNIQUE / FK / not-null violation on either backend."""
    return isinstance(exc, sqlite3.IntegrityError)


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


# ---------------------------------------------------------------------------
# Postgres (Supabase) backend.
#
# Why this exists: on an ephemeral container, a SQLite file only survives if a volume is
# mounted and never re-created. Getting that wrong does not fail loudly - the season just
# resets to empty on the next deploy. A hosted Postgres removes that whole class of bug, so
# connect() speaks to it through a sqlite3-shaped facade instead of 144 call sites being
# rewritten onto an async driver.
#
# The facade mirrors sqlite3 semantics deliberately, because that is what the service layer
# was built against and because every suite enters the database through db.connect():
#   * ? placeholders            -> %s, quote-aware (a prompt containing "?" is data, not a
#                                  parameter, and must survive the translation untouched)
#   * INSERT OR REPLACE         -> ON CONFLICT DO UPDATE SET every listed column. SQLite
#                                  REPLACE deletes then inserts, so columns ABSENT from the
#                                  INSERT revert to their DEFAULT; a partial DO UPDATE would
#                                  keep them and quietly change re-graded totals.
#   * INSERT OR IGNORE          -> ON CONFLICT DO NOTHING
#   * with conn:  (23 call sites) -> BEGIN / COMMIT, ROLLBACK on exception, nesting counted
#   * sqlite3.Row               -> Row below: ["col"], [0], dict(r), .keys(), unpacking
#   * sqlite3.IntegrityError    -> db.IntegrityError, so a duplicate submission still lands
#                                  in submit_answer's except branch instead of in a player's
#                                  face. services.py used to name sqlite3 directly.
#   * bool/float params         -> coerced to the declared column type. SQLite is lax;
#                                  Postgres refuses a float into an integer column outright.
# ---------------------------------------------------------------------------


class DBError(Exception):
    """Root of the adapter's error types.

    Subclassing sqlite3.Error rather than inventing a parallel hierarchy is the whole trick:
    an `except sqlite3.IntegrityError` written long before Postgres existed keeps firing on
    either backend, and so does `except db.IntegrityError`. A plain Exception base here was my
    first attempt, and it silently broke the duplicate-answer path on SQLite - the clause
    caught nothing, so the exception escaped submit_answer.
    """


class IntegrityError(DBError, sqlite3.IntegrityError):
    """UNIQUE / foreign key / not-null / check violation, on either backend.

    Two bases because sqlite3's and psycopg3's hierarchies share no parent and callers must be
    able to catch one name. The Postgres side maps psycopg.IntegrityError (which really covers
    unique/FK/not-null/check) and NOT errors.IntegrityConstraintViolation - in psycopg3 a
    UniqueViolation is not a subclass of that, so keying off it would turn a duplicate
    submission into a dead button for the player."""


class OperationalError(DBError, sqlite3.OperationalError):
    """Connection lost, timeout, or the server refused the statement."""


# ---- the alias rule, and why it goes this way ------------------------------------
# The adapter raises db.IntegrityError, a SUBCLASS of sqlite3.IntegrityError. That direction
# is the only one that works: code written against sqlite3 (submit_answer's duplicate-answer
# handler, for instance) catches sqlite3.IntegrityError, and a subclass is caught by a
# `except <base>` clause on both backends. Inverting it - the intuitive-looking choice - leaves
# the SQLite-raised exception uncaught, which is how a player double-tapping a button gets a
# failed interaction instead of an edit.
# Anything wanting to catch both dialects should name sqlite3's class via dbmod.sqlite3.
db_errors = (IntegrityError, OperationalError)


class Row:
    """A sqlite3.Row-shaped result row: row["col"], row[0], dict(row), iteration, len()."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = tuple(cols)
        self._vals = tuple(vals)

    def _pos(self, key):
        if isinstance(key, int):
            return key
        try:
            return self._cols.index(key)
        except ValueError:
            raise IndexError("no such column: " + str(key)) from None

    def __getitem__(self, key):
        if isinstance(key, slice):          # sqlite3.Row supports slicing; rows are handed to
            return self._vals[key]          # UI code that formats them positionally
        return self._vals[self._pos(key)]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._vals[self._cols.index(name)]
        except ValueError:
            raise AttributeError(name) from None

    def keys(self):
        return list(self._cols)

    def values(self):
        return list(self._vals)

    def items(self):
        return list(zip(self._cols, self._vals))

    def index(self, value):
        return self._vals.index(value)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __contains__(self, name):
        return name in self._cols

    def __repr__(self):
        return "<Row " + repr(dict(zip(self._cols, self._vals))) + ">"


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on `sep`, ignoring nesting inside '' and (). Column lists contain both."""
    out, buf, depth, quote = [], [], 0, False
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == "'":
                quote = False
            continue
        if ch == "'":
            quote = True
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]


def _translate(sql: str) -> str:
    """SQLite statement text -> Postgres statement text.

    Rewrites `?` to `%s`, skipping single-quoted literals (two consecutive quotes are an escaped
    quote in both engines, and that is the only thing that opens or closes a literal).
    No `%` escaping is done, deliberately: psycopg only parses placeholders when a params
    object is passed (see execute), so `LIKE 'role.%'` and a prompt containing "100%" pass
    through as literal text. Doubling them here would corrupt real data in bound strings.
    """
    if "?" not in sql:
        return sql
    out, quote, i = [], False, 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    quote = False
        elif ch == "'":
            quote = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _pg_schema(script: str) -> str:
    """SQLite DDL -> Postgres DDL, for the one construct the two engines disagree on."""
    return re.sub(r"\bid\s+INTEGER\s+PRIMARY\s+KEY\b",
                  "id bigint PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY", script, flags=re.I)


def _split_statements(script: str) -> list[str]:
    """Split a SQLite-style script on top-level `;`, ignoring ; inside '' literals and
    inside -- line comments. Keeps the trailing newline of each line so a `--` comment cannot
    run on into the next statement."""
    out, buf, i, n = [], [], 0, len(script)
    while i < n:
        ch = script[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(script[i])
                if script[i] == "'":
                    if i + 1 < n and script[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if ch == "-" and script.startswith("--", i):
            while i < n and script[i] != "\n":
                buf.append(script[i])
                i += 1
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf))
    return [s.strip() for s in out]


class PgCursor:
    """Buffers the result so a caller can index by name AND by position, as sqlite3.Row does."""

    def __init__(self, cur):
        self._cur = cur
        self._rows = None
        self._pos = 0
        self.rowcount = cur.rowcount
        self.lastrowid = None
        # sqlite3 exposes cursor.lastrowid, and create_season()/submit_answer() both take the
        # id of a row they just inserted. Fetching here (rather than lazily) is what lets that
        # work while leaving fetchone()/fetchall() chained on the same cursor afterwards.
        self._materialise()
        if self._rows and "id" in self._rows[0].keys():
            self.lastrowid = self._rows[0]["id"]

    def _materialise(self):
        if self._rows is not None:
            return
        if not self._cur.description:
            # INSERT/UPDATE/DELETE without RETURNING: psycopg raises ProgrammingError on
            # fetchall() for these, exactly as sqlite3 returns no rows. `description is None`
            # is the safe signal (rowcount works, status does not exist on this version), and
            # it matters because PgCursor buffers eagerly to expose lastrowid - without this
            # guard, every set_cfg() call would abort the `with conn:` block around it.
            self._rows = []
            return
        cols = [d.name for d in self._cur.description]
        rows = []
        for r in self._cur.fetchall():
            if isinstance(r, dict):
                rows.append(Row(cols, [r.get(c) for c in cols]))
            else:
                rows.append(Row(cols, list(r)))
        self._rows = rows

    def fetchone(self):
        self._materialise()
        if self._pos < len(self._rows):
            r = self._rows[self._pos]
            self._pos += 1
            return r
        return None

    def fetchall(self):
        self._materialise()
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def fetchmany(self, n: int = 100):
        self._materialise()
        out = self._rows[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


# All patterns avoid backslashes: this module is generated into a file and a stray escape
# in a regex string is the kind of bug a syntax check cannot see.
_NAME = "[A-Za-z_0-9.]+"
_INSERT_RE = re.compile(
    r"^\s*INSERT\s+(?:OR\s+(REPLACE|IGNORE)\s+)?INTO\s+(" + _NAME + r")\s*\(([^()]*?)\)",
    re.I)
# UPDATE / DELETE only. An INSERT's table always comes from _INSERT_RE (which is anchored at
# ^) instead: one shared "find the table" regex here was a mistake, because `INTO` and `UPDATE`
# also occur inside other words and inside string literals (a prompt reading "update the
# scoreboard" would have been parsed as an UPDATE against a table named "the").
_TABLE_RE = re.compile(r"^\s*(?:UPDATE\s+|DELETE\s+FROM\s+)(" + _NAME + r")", re.I)
_SET_RE = re.compile(r"\bSET\b(.*?)\bWHERE\b", re.I | re.S)


class PgConnection:
    """One Postgres connection, shaped like sqlite3.Connection.

    A single connection behind an RLock, because `with conn:` nests at 23 call sites and the
    20-second scheduler tick shares the process with interaction handlers. Queries run in the
    same region as the bot, so a statement costs milliseconds, and the write volume here (a
    few hundred grading writes on a busy night for 200 players) cannot starve a gateway. If
    that ever changes, this class is the only seam that needs a pool.
    """

    def __init__(self, conninfo: str):
        import psycopg
        self._psycopg = psycopg
        self._conninfo = conninfo
        self._lock = threading.RLock()
        self._depth = 0
        self._sp = 0                   # savepoint counter, reset at each BEGIN
        self.row_factory = None            # api parity: sqlite-only knob, unused here
        self.isolation_level = None        # ditto; transactions arrive via `with conn:`
        try:
            # autocommit plus a hand-run BEGIN is exactly how sqlite3 with
            # isolation_level=None behaves, which is what the service layer assumes.
            # prepare_threshold=0 is not cosmetic: Supabase :6543 routes through PgBouncer in
            # transaction mode, where server-side prepared statements leak between clients and
            # die with "prepared statement already exists". Client-side binds survive pooling.
            self._c = psycopg.connect(conninfo, autocommit=True,
                                       row_factory=psycopg.rows.dict_row,
                                       prepare_threshold=0,
                                       application_name="hub-knowledge-season")
        except Exception as exc:
            raise OperationalError("could not connect to Postgres: " + str(exc)) from exc
        self._coltypes: dict[str, dict[str, str]] = {}
        self._uniques: dict[str, dict[str, list[str]]] = {}
        self._tables: set[str] = set()
        self._bootstrap()

    # -- introspection ------------------------------------------------------
    def _bootstrap(self) -> None:
        rows = self._raw(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position")
        coltypes: dict[str, dict[str, str]] = {}
        for r in rows:
            coltypes.setdefault(r["table_name"], {})[r["column_name"]] = r["data_type"]
        self._coltypes = coltypes
        self._tables = {r["table_name"] for r in self._raw(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}

    def _types_for(self, table: str | None) -> dict[str, str]:
        """Column types, refreshed from the catalog when the cache does not know the table.

        The cache is built at bootstrap, so a table created afterwards (a migration, a probe,
        a hand-run ALTER) would be invisible - and the code that consults it treats "unknown"
        as "nothing to do", which turns a staleness bug into a silently skipped coercion.
        """
        if not table:
            return {}
        if table not in self._coltypes:
            self._bootstrap()
        return self._coltypes.get(table, {})

    def columns(self, table: str) -> set[str]:
        return set(self._types_for(table))

    def has_table(self, table: str) -> bool:
        return table in self._tables

    def _raw(self, sql: str, params=None):
        """Internal escape hatch: no INSERT OR rewriting, but placeholders are still
        translated, so ? is legal here too (it is what every other method expects)."""
        with self._lock, self._c.cursor() as cur:
            cur.execute(_translate(sql), params)
            if not cur.description:
                return []
            cols = [d.name for d in cur.description]
            out = []
            for r in cur.fetchall():
                out.append(Row(cols, list(r.values()) if isinstance(r, dict) else list(r)))
            return out

    # -- statement translation ---------------------------------------------
    def _prepare(self, sql: str) -> tuple[str, str | None, list[str]]:
        """Rewrite one SQLite statement into its Postgres equivalent.

        INSERT OR REPLACE: SQLite *deletes* the conflicting row then inserts, so every column
        left out of the column list reverts to its DEFAULT. I verified that against SQLite, and
        verified the conflict is any UNIQUE index, not just the rowid. The equivalent is
        ON CONFLICT (<that unique group>) DO UPDATE SET <all listed cols>; Postgres then re-fires
        any other unique violation for us, matching SQLite. Targeting a group the INSERT does not
        supply is impossible, so those cases fall through as a plain insert - which is the same
        result, because a new row can only supersede an old one via a key the statement carries.
        """
        cols: list[str] = []
        ins = _INSERT_RE.match(sql)
        if ins:
            table = ins.group(2).strip().strip('"').lower()
        elif (m := _TABLE_RE.match(sql)):
            # lower-cased to match information_schema, which folds unquoted identifiers
            table = m.group(1).strip().strip('"').lower()
        else:
            table = None
        if not ins:
            return _translate(sql), table, cols
        low = sql.lower()
        if not (ins.group(1) or "").upper() and "on conflict" not in low \
                and "returning" not in low and table in self._coltypes \
                and "id" in self._coltypes[table]:
            # needed for cursor.lastrowid; harmless otherwise (the caller may ignore it)
            body = sql.rstrip().rstrip(";") + " RETURNING id"
            return _translate(body), table, cols
        mode = (ins.group(1) or "").upper()
        table = table or ins.group(2).strip('"').lower()
        cols = [c.strip().strip('"') for c in _split_top(ins.group(3))]
        body = sql.rstrip()
        if body.endswith(";"):
            body = body[:-1]
        # Postgres has no `INSERT OR x` form: the modifier must go, or it is a syntax error
        # before the ON CONFLICT clause is ever considered.
        if mode:
            body = re.sub(r"\bINSERT\s+OR\s+" + mode + r"\s+INTO\b", "INSERT INTO",
                          body, count=1, flags=re.I)
        if mode == "IGNORE":
            return _translate(body + " ON CONFLICT DO NOTHING"), table, cols
        if mode == "REPLACE" and cols:
            lower = [c.lower() for c in cols]
            target = None
            for group in self._unique_groups(table):
                if all(k in lower for k in group):
                    target = group
                    break
            if target is None and "id" in lower:
                target = ["id"]
            if target:
                lower_t = [k.lower() for k in target]
                sets = ", ".join(c + " = excluded." + c for c in cols)
                # SQLite REPLACE throws the old row away, so a column the INSERT does not
                # mention comes back as its DEFAULT, not as its previous value. Leaving it
                # alone here would silently carry e.g. last run's coins into a re-grade.
                resets = ""
                if cols:
                    listed = [x.lower() for x in cols]
                    stale = [c for c in self._types_for(table)
                             if c not in lower_t and c not in listed]
                    if stale:
                        resets = ", " + ", ".join(c + " = DEFAULT" for c in stale)
                keys = ", ".join(target)
                body = body + f" ON CONFLICT ({keys}) DO UPDATE SET {sets}{resets}"
        return _translate(body), table, cols

    def _unique_groups(self, table: str) -> list[list[str]]:
        """Unique/PK column groups, from the live catalog so migrations are included.

        Read once per table: this drives which ON CONFLICT target is legal, and guessing it
        from SCHEMA would drift the moment a migration adds a guard.
        """
        if table in self._uniques:
            return self._uniques[table]
        if table not in self._tables:
            self._bootstrap()
        rows = self._raw(
            "SELECT c.conname, a.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype IN ('u','p') AND c.conrelid = ?::regclass "
            "ORDER BY array_length(c.conkey,1), c.conname, a.attnum", (table,))
        by_name: dict[str, list[str]] = {}
        for r in rows:
            by_name.setdefault(r["conname"], []).append(r["attname"])
        groups = list(by_name.values())
        self._uniques[table] = groups
        return groups

    def _set_cols(self, sql: str) -> list[str]:
        """Column order for an UPDATE's ? params, read out of its SET list."""
        if not (m := _SET_RE.search(sql)):
            return []
        return [p.split("=", 1)[0].strip().strip('"').lower() for p in _split_top(m.group(1))]

    def _coerce(self, sql: str, table, cols, params):
        if not params or not table:
            return params
        if table not in self._coltypes:
            self._bootstrap()
        if table not in self._coltypes:
            return params
        types = self._types_for(table)
        names = [c.strip().strip('"').lower() for c in cols] if cols else self._set_cols(sql)
        if len(names) < len(params):
            names = names + [None] * (len(params) - len(names))
        out = list(params)
        for i, v in enumerate(out):
            if v is None:
                continue
            col = names[i] if i < len(names) else None
            want = types.get(col) if col else None
            if want is None:
                continue
            if isinstance(v, bool) and "bool" not in want:
                out[i] = 1 if v else 0
            elif isinstance(v, float) and want.startswith(("int", "numeric")) and v.is_integer():
                out[i] = int(v)
        return tuple(out)

    def _heal(self) -> None:
        """Undo the aborted-transaction side effect, so a caught error is survivable.

        Postgres differs from SQLite in a way that breaks this bot's core flow: inside a
        transaction, ANY error puts the transaction in an aborted state and every later command
        fails with InFailedSqlTransaction - catching the exception is not enough. submit_answer()
        deliberately catches the UNIQUE violation from a player's second tap and then keeps
        working (it turns the insert into an UPDATE); without this it would answer "interaction
        failed" and lose the edit. Wrapping each in-transaction statement in a savepoint and
        rolling back to it restores a usable transaction, exactly SQLite's behaviour.
        """
        if self._depth and self._sp:
            try:
                self._c.execute("ROLLBACK TO SAVEPOINT sp" + str(self._sp))
            except self._psycopg.Error:
                pass

    def _ensure_conn(self) -> bool:
        """Re-open a dropped connection. Returns True if it had to reconnect.

        Railway restarts the process, so a dead connection does not have to be survivable -
        but a 20-second restart gap during a night costs the opening post, and Supabase's
        pooler does idle-drop. Cheap to do here because every statement funnels through
        execute(); outside a `with conn:` block, so a retry cannot duplicate half a transaction.
        """
        try:
            if not self._c.closed:
                return False
        except self._psycopg.Error:
            return False
        try:
            self._c = self._psycopg.connect(self._conninfo, autocommit=True,
                                            row_factory=self._psycopg.rows.dict_row,
                                            prepare_threshold=0,
                                            application_name="hub-knowledge-season")
            return True
        except self._psycopg.Error:
            return False

    def execute(self, sql, params=None):
        # Deliberately NOT `params or ()`: psycopg only inspects the query for placeholders
        # when a params object is supplied, and an empty tuple IS supplied - so normalising
        # None to () makes every statement containing a literal % (LIKE 'role.%', a prompt
        # saying "100%") die with "only %s, %b, %t are allowed". Pass None through.
        params = tuple(params) if params else None
        sql2, table, cols = self._prepare(sql)
        params = self._coerce(sql2, table, cols, params)
        cur = None
        guard = bool(self._depth)
        if not guard:
            self._ensure_conn()
        try:
            with self._lock:
                if guard:
                    self._sp += 1
                    self._c.execute("SAVEPOINT sp" + str(self._sp))
                cur = self._c.cursor()
                cur.execute(sql2, params)
                if guard:
                    self._c.execute("RELEASE SAVEPOINT sp" + str(self._sp))
                return PgCursor(cur)
        except self._psycopg.IntegrityError as exc:
            # psycopg puts UNIQUE / FK / NOT NULL / CHECK under psycopg.IntegrityError, and
            # NOT under errors.IntegrityConstraintViolation (that one is a sibling). Matching
            # the wrong class would let a duplicate submission sail past submit_answer's
            # handler and surface to the player as a failed button.
            # SQLite's IntegrityError covers the same set, so this is the equivalent mapping.
            if cur is not None:
                cur.close()
            self._heal()
            raise IntegrityError(str(exc).strip()) from exc
        except self._psycopg.OperationalError as exc:
            # connection-level failure outside a transaction: one retry on a fresh connection.
            # Safe because everything here is autocommit and statement-shaped, so replaying a
            # single statement cannot half-apply anything.
            if cur is not None:
                cur.close()
            if guard or not self._ensure_conn():
                raise OperationalError(str(exc).strip() + "\n  in: " + sql[:200]) from exc
            return self.execute(sql, params)
        except self._psycopg.Error as exc:
            if cur is not None:
                cur.close()
            raise OperationalError(str(exc).strip() + "\n  in: " + sql[:200]) from exc

    def executescript(self, script: str):
        """SCHEMA is one multi-statement string, sqlite-style. Split it and run each part.

        `script.split(";")` is NOT enough and fails on this very schema: SCHEMA is full of
        trailing `-- comment` lines, and if a fragment ends mid-line the comment swallows the
        newline that terminated it, so the next statement gets eaten and Postgres reports a
        truncation error three tables in. Split quote- and comment-aware instead.
        """
        for stmt in _split_statements(_pg_schema(script)):
            if not stmt:
                continue
            try:
                self._raw(stmt)
            except Exception as exc:
                raise OperationalError(str(exc) + "\n  in: " + stmt[:160]) from exc
        self._bootstrap()

    def commit(self) -> None:
        with self._lock:
            self._c.commit()

    def rollback(self) -> None:
        with self._lock:
            self._c.rollback()

    def close(self) -> None:
        with self._lock:
            try:
                if self._depth:
                    self._c.execute("ROLLBACK")
                self._c.close()
            except Exception:
                pass

    # -- `with conn:` : sqlite commits on clean exit and rolls back on error --
    def __enter__(self):
        self._lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._sp = 0
            self._c.execute("BEGIN")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._depth == 1:
                if exc_type is None:
                    self._c.execute("COMMIT")
                else:
                    self._c.execute("ROLLBACK")
        except self._psycopg.Error:
            try:
                self._c.rollback()
            except Exception:
                pass
        finally:
            self._depth -= 1
            self._lock.release()
        return False

    def sync_sequences(self) -> int:
        """Push every id sequence past any explicitly-inserted id.

        SQLite's INTEGER PRIMARY KEY *is* the rowid, so inserting a chosen id is harmless.
        In Postgres the sequence does not follow it, and the next id-less insert then collides
        with a row that already exists - which would surface days later, mid-grading, as a
        UNIQUE violation on a table nobody touched by hand. Cheap insurance: run on connect.
        """
        n = 0
        for table in sorted(self._coltypes):
            if "id" not in self._coltypes[table] or not self.has_table(table):
                continue
            rows = self._raw('SELECT COALESCE(MAX(id), 0) AS m FROM "' + table + '"')
            mx = int(rows[0]["m"] or 0) if rows else 0
            self._raw("SELECT setval(pg_get_serial_sequence('" + table + "', 'id'), "
                      "GREATEST(?, 1), ?)", (mx, mx > 0))
            n += 1
        return n

    def __repr__(self):
        return "<PgConnection tables=" + str(len(self._tables)) + ">"
