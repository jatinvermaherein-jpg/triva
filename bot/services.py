"""Service layer — every rule the bot enforces lives here, with Discord fully absent.

That separation is deliberate: the buttons in ui.py are ~10-line wrappers around these
functions, so the entire competition can be tested (and re-verified after any refactor)
without a token, a gateway connection or a guild.

Invariants enforced here:
  * grading is idempotent           (regrade deletes and rebuilds that evening's rows)
  * a point is never updated in place (append-only ledger; corrections are new rows)
  * an evening's status is the ONLY state a panel may act on (restart-proof)
  * speed bonus is computed from timestamps, never from a human's memory
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import discord
from zoneinfo import ZoneInfo

from loader import load_db, load_scoring          # noqa: E402

dbmod = load_db()
sc = load_scoring()

IST = ZoneInfo("Asia/Kolkata")
BAND_OPTIONS = [(25, "Excellent"), (15, "Good"), (8, "Average"), (2, "Poor")]
L23_MIN, L23_MAX = 0, 25


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def ist(ts: float | int | dt.datetime) -> dt.datetime:
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, IST)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def iso(ts: float | dt.datetime) -> str:
    return ist(ts).isoformat(timespec="seconds")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


# IST on purpose. `open_at` builds the datetime with tzinfo=IST, so these are the
# LOCAL hours the docs and the announcement quote. An earlier version stored 16:00
# UTC and every document called it "16:00 IST" - six hours away from the truth, in
# the one number players are told to show up for. If you change it, the card, the
# board and the announcement are all derived from here, so change it ONCE.
NIGHT_OPEN_HOUR = 16      # 16:00 IST, every night, every league
NIGHT_OPEN_MINUTE = 0
NIGHT_WINDOW_S = 24 * 3600


def open_at(day_iso: str, hour: int = NIGHT_OPEN_HOUR, minute: int = NIGHT_OPEN_MINUTE) -> dt.datetime:
    d = dt.date.fromisoformat(day_iso)
    return dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


# --------------------------------------------------------------------------- #
# one-command provisioning: everything the bot needs, addressed BY ID
# --------------------------------------------------------------------------- #
#
# The whole point of the map below: a channel or role is NEVER looked up by name
# at runtime. `/setup` resolves each entry once, stores the snowflake in `config`,
# and every later lookup goes id -> object. So the owner can rename #trivia-night
# to #quiz-night or promote "Trivia Champion" to a gold colour and nothing breaks.
# Names appear in exactly two places: the "reuse if it already exists" search at
# setup time, and the text a human reads.

STAFF_ROLE = "Hub Staff"

PROVISION_ROLES = [
    # key, name, hex colour, hoisted (shown separately in the member list)
    ("staff", STAFF_ROLE, 0x9b59b6, False),
    ("champ_l1", "Trivia Champion", 0x3498db, True),
    ("champ_l2", "Strategy Champion", 0xe74c3c, True),
    ("champ_l3", "Hangar Champion", 0xe67e22, True),
    ("champ_overall", "Season Champion", 0xf1c40f, True),
]

PROVISION_CHANNELS = [
    # key, name, staff_only, slowmode_seconds, purpose
    ("league_l1", "trivia-night", False, 10, "League 1 posts and answers"),
    ("league_l2", "strategy-night", False, 30, "League 2 scenario submissions"),
    ("league_l3", "hangar-review", False, 30, "League 3 hangar reviews"),
    ("hub", "hub-announcements", False, 0, "night cards, results, champion announcements"),
    ("league_table", "league-table", False, 0, "the permanent auto-updating board"),
    ("results", "season-results", False, 0, "season closings and the Hall of Fame"),
    ("appeals", "appeals", False, 10, "one reply per appeal, within 24h"),
    ("checkout", "pending-checkout", True, 0, "staff pay these by hand, then press CLEAR"),
    ("staff", "staff-only", True, 0, "the hub panel and every staff control"),
]
CATEGORY_NAME = "Hub Knowledge Season"


def _hubkey(kind: str, key: str) -> str:
    """The config key a provisioned object lives under.

    Namespaced BY KIND, because the same bare key was legitimately used twice:
    "staff" named both the Hub Staff ROLE and the #staff-only CHANNEL. Provisioning
    therefore wrote hub:staff twice, the second write deleted the first, the role
    lost its stored id, and the bot fell back to matching that role by NAME - which
    is precisely the thing this id-based design exists to make unnecessary.
    """
    prefix = {"role": "role_", "category": "cat_", "channel": "chan_"}[kind]
    return f"hub:{prefix}{key}"


def _no_collisions() -> None:
    """Refuse to import if two provisioned objects ever want the same key again."""
    keys = [_hubkey("role", k) for k, *_ in PROVISION_ROLES]
    keys.append(_hubkey("category", "category"))
    keys += [_hubkey("channel", k) for k, *_ in PROVISION_CHANNELS]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise RuntimeError(f"provision key collision: {dupes} - those objects would "
                          f"overwrite each other's stored id")


_no_collisions()


def provision_plan(conn) -> dict:
    """What /setup will create or adopt, plus anything already stored by id."""
    stored = {_hubkey("role", k): dbmod.cfg(conn, _hubkey("role", k))
              for k, *_ in PROVISION_ROLES}
    stored.update({_hubkey("channel", k): dbmod.cfg(conn, _hubkey("channel", k))
                   for k, *_ in PROVISION_CHANNELS})
    return {"roles": [dict(key=k, name=n, colour=c, hoist=h,
                           id=stored.get(_hubkey("role", k)))
                      for k, n, c, h in PROVISION_ROLES],
            "channels": [dict(key=k, name=n, staff_only=so, slow=sl, why=w,
                              id=stored.get(_hubkey("channel", k)))
                         for k, n, so, sl, w in PROVISION_CHANNELS],
            "category": {"name": CATEGORY_NAME,
                         "id": dbmod.cfg(conn, _hubkey("category", "category"))}}


def _adopt(ws, conn, kind: str, key: str, name: str, get, find):
    """Resolve one Discord object: stored id first, then name, then nothing.

    Returns (obj, adopted) where adopted is "id" / "name" / None(=create). Kept as
    one function because the ORDER is the entire design: an id lookup is what makes a
    rename harmless, so no caller is allowed to forget it.
    """
    stored = dbmod.cfg(conn, _hubkey(kind, key))
    if stored:
        obj = get(stored)
        if obj is not None:
            return obj, "id"
    obj = find(key, name)
    if obj is not None:
        return obj, "name"
    return None, None


async def provision(conn, ws, staff_role_id: int | None = None,
                    actor_id: int | None = None, *, category: bool = True) -> dict:
    """Create (or adopt) every channel and role, then store ids in `config`.

    The workspace protocol (`ws`), which keeps this Discord-free and testable:
        ws.get_role(id) / ws.find_role(name)
        ws.get_channel(id) / ws.find_channel(key, name)
        ws.create_role(name=, colour=, hoist=, reason=)
        ws.create_channel(name=, category=, overwrites=, slowmode=, reason=)
        ws.create_category(name=, overwrites=, reason=)

    Idempotent by construction: every key resolves id -> name -> create, and the ids
    are written from ONE dict at the end (no positional bookkeeping, which is how the
    first version of this function wrote `hub:staff` from a different run's ids).
    """
    ids: dict[str, int] = {}       # key -> discord id, the single source written back
    legacy: dict[str, int] = {}    # written WITHOUT the hub: prefix (channel:l1)
    made: list[dict] = []

    def note(kind, key, name, obj, created, adopted):
        ids[_hubkey(kind, key)] = int(obj.id)
        made.append({"kind": kind, "key": key, "name": name, "id": int(obj.id),
                     "created": created, "adopted": adopted})

    # ---- roles ---------------------------------------------------------- #
    for key, name, colour, hoist in PROVISION_ROLES:
        if key == "staff" and staff_role_id is not None:
            # The human picked which role runs the quizzes. Adopt THEIRS and do not
            # mint a competing "Hub Staff": two staff roles means two answers to
            # "who is staff", and the buttons would follow whichever one matched.
            picked = ws.get_role(int(staff_role_id)) or ws.find_role(name)
            if picked is None:
                raise ValueError(f"role {staff_role_id} does not exist in this server")
            note("role", key, picked.name, picked, False, "selected")
            continue
        obj, adopted = _adopt(ws, conn, "role", key, name, ws.get_role,
                             lambda _k, n: ws.find_role(n))
        if obj is None:
            obj = await ws.create_role(name=name, colour=colour, hoist=hoist,
                                       reason="Hub Knowledge Season /setup")
            note("role", key, name, obj, True, None)
        else:
            note("role", key, name, obj, False, adopted)

    # look it up by its namespaced key: ids is keyed by the FULL config key now, and
    # a KeyError here would be a confusing crash inside a setup nobody can re-run
    # halfway.
    staff_id = ids[_hubkey("role", "staff")]

    # ---- category ------------------------------------------------------- #
    cat_obj = None
    if category:
        obj, adopted = _adopt(ws, conn, "category", "category", CATEGORY_NAME,
                              ws.get_channel, lambda _k, n: ws.find_channel(_k, n))
        if obj is None:
            # everyone_read=True ON PURPOSE: a deny on the category cascades to every
            # public league channel under it. Privacy is enforced per channel only.
            obj = await ws.create_category(
                name=CATEGORY_NAME, reason="Hub Knowledge Season /setup",
                overwrites=_writes(staff_id, everyone_read=True))
            note("category", "category", CATEGORY_NAME, obj, True, None)
        else:
            note("category", "category", CATEGORY_NAME, obj, False, adopted)
        cat_obj = obj

    # ---- channels ------------------------------------------------------- #
    for key, name, staff_only, slow, _why in PROVISION_CHANNELS:
        obj, adopted = _adopt(ws, conn, "channel", key, name, ws.get_channel,
                              ws.find_channel)
        if obj is None:
            obj = await ws.create_channel(
                name=name, category=cat_obj, slowmode=slow,
                overwrites=_writes(staff_id, everyone_read=not staff_only),
                reason="Hub Knowledge Season /setup")
            note("channel", key, name, obj, True, None)
        else:
            note("channel", key, name, obj, False, adopted)
        if key.startswith("league_"):
            # Populate the legacy per-league key too, so a fresh /setup needs no
            # /setup-channel follow-up. An existing hand-set mapping is left alone:
            # /setup-channel still wins, as an override rather than an accident.
            lg = key[len("league_"):]
            if dbmod.cfg(conn, f"channel:{lg}") is None:
                legacy[f"channel:{lg}"] = int(obj.id)

    with conn:
        for key, value in ids.items():      # already the full config key
            dbmod.set_cfg(conn, key, value)
        for key, value in legacy.items():
            dbmod.set_cfg(conn, key, value)
        dbmod.set_cfg(conn, "staff_role_id", staff_id)
    _bind_season_roles(conn)
    dbmod.audit(conn, "hub.provision", actor_id, None, None,
                {"entries": len(made),
                 "created": sum(1 for m in made if m["created"]),
                 "adopted": sum(1 for m in made if not m["created"]),
                 "staff_role_id": staff_id})
    return {"entries": made,
            "created": [m for m in made if m["created"]],
            "reused": [m for m in made if not m["created"]],
            "staff_role_id": staff_id, "ids": ids}


def _writes(staff_id: int, *, everyone_read: bool) -> dict:
    """Permission overwrites, in LOGICAL form - the caller turns this into discord.py
    PermissionOverwrite objects, because only that layer knows what @everyone's id is.

    Public league/result channels: @everyone can read and type; staff can do both too
    (nothing extra, no per-league grants). Private channels: @everyone cannot even see
    them, and the selected staff role is the only thing that can. That is the whole
    permission surface - a channel is either "the server can see it" or "staff only".
    """
    return {"everyone": {"read": everyone_read, "write": everyone_read},
            "staff": {"read": True, "write": True},
            "staff_id": staff_id}


def channel_ids(conn) -> dict:
    """Every provisioned channel id, keyed by the PROVISION_CHANNELS key."""
    return {k: dbmod.cfg(conn, _hubkey("channel", k)) for k, *_ in PROVISION_CHANNELS}


def _bind_season_roles(conn) -> None:
    """Point the ACTIVE season at the champion roles it just made.

    Only fills NULLs: if an owner has hand-set a season's role to something else,
    that is a decision, not an oversight, and /setup must not quietly overwrite it.
    """
    mapping = {"champ_l1": "role_trivia", "champ_l2": "role_strategy",
               "champ_l3": "role_hangar", "champ_overall": "role_overall"}
    sid = closing_season_id(conn)
    if sid is None:
        return
    with conn:
        for rkey, col in mapping.items():
            rid = dbmod.cfg(conn, _hubkey("role", rkey))
            if rid:
                conn.execute(f"UPDATE season SET {col}=COALESCE({col},?) WHERE id=?",
                             (rid, sid))


# --------------------------------------------------------------------------- #
# season + schedule
# --------------------------------------------------------------------------- #

def answer_window(conn) -> int:
    """How long a question stays open. 24 hours for EVERY league, every night.

    The owner's rule: a Monday trivia question is open until Tuesday 21:30, so the
    next trivia question (Thursday) arrives ~48h after the first one opened - a
    player who only plays at weekends still sees every question once.
    """
    return int(dbmod.cfg(conn, "answer_seconds", NIGHT_WINDOW_S))


def create_season(conn, name: str, start_day: str, weeks: int = 4,
                  coins_per_point: int = 1) -> dict:
    """No window parameter ON PURPOSE: 24 hours is a rule, not a per-season option.

    An earlier version took `answer_seconds`, which meant a season could be created
    with a two-minute window and the calendar would look fine while nobody could
    answer. A server-wide override lives in config (`answer_seconds`) for the rare
    case a whole deployment needs something else.""",
    """Builds the whole calendar up front: 3 leagues x one evening per day.

    The scheduler needs this to exist before the first night, so a restart -
    or a host going offline - cannot desynchronise which evening is 'today'.
    """
    start = dt.date.fromisoformat(start_day)
    with conn:
        cur = conn.execute(
            "INSERT INTO season(name,starts_at,ends_at,weeks,coins_per_point,status) "
            "VALUES(?,?,?,?,?,'active')",
            (name, start.isoformat(),
             (start + dt.timedelta(days=weeks * 7 - 1)).isoformat(),
             weeks, coins_per_point))
        season_id = cur.lastrowid
        # ONE global key: every night, every league, closes answer_seconds after it
        # opened. sub_close_hour is gone - it used to give L1 and L2/L3 different
        # deadlines, which is exactly the asymmetry the 24h rule removes. A stale
        # sub_close_hour left in config by an older version is simply never read.
        window = answer_window(conn)      # read once, applied to every evening
        made = 0
        # Your rotation: Mon/Thu knowledge, Tue/Fri strategy, Wed/Sat hangar, and
        # Sunday where ALL THREE run together. That gives every league the same
        # 12 scored nights, which is why Sundays can count inside the league tables
        # here (they could not when League 1 had twice as many weekday nights).
        # Sunday carries no multiplier and no special name - it is a day, not an event.
        weekday_leagues = {0: ("l1",), 1: ("l2",), 2: ("l3",),
                           3: ("l1",), 4: ("l2",), 5: ("l3",),
                           6: ("l1", "l2", "l3")}
        for i in range(weeks * 7):
            day = start + dt.timedelta(days=i)
            opens = open_at(day.isoformat())
            closes = opens + dt.timedelta(seconds=window)
            # Sunday is a scheduling fact (all three leagues run it), not a reward
            # tier: is_sunday stays for the card label, multiplier/difficulty do not.
            sunday = day.weekday() == 6
            for league in weekday_leagues[day.weekday()]:
                conn.execute(
                    "INSERT OR IGNORE INTO evening(season_id,day,league,is_sunday,multiplier,"
                    "difficulty,opens_at,closes_at,status) VALUES(?,?,?,?,?,?,?,?,'scheduled')",
                    (season_id, day.isoformat(), league, int(sunday), 1.0, "normal",
                     iso(opens), iso(closes)))
                made += 1
    per = {lg: conn.execute("SELECT COUNT(*) c FROM evening WHERE season_id=? AND league=?",
                            (season_id, lg)).fetchone()["c"] for lg in ("l1", "l2", "l3")}
    if len(set(per.values())) != 1:
        raise RuntimeError(f"calendar is unbalanced: {per} - leagues must have equal nights")
    dbmod.audit(conn, "season.create", None, None,
                {"season": name, "start": start_day, "weeks": weeks, "evenings": made,
                 "nights_per_league": per})
    return {"season_id": season_id, "name": name, "evenings": made, "nights_per_league": per,
            "grand_final_day": (start + dt.timedelta(days=weeks * 7 - 1)).isoformat()}


def season_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM season WHERE status='active' ORDER BY id DESC "
                       "LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM season ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def closing_season_id(conn) -> int | None:
    """The season a "season end" action should act on: the active one, else the
    newest, else None. ONE resolver, used by finalize_season AND the dry-run button
    - if the two each re-resolved it, the preview could describe a different season
    from the change it previews, which is worse than having no preview at all."""
    row = conn.execute("SELECT id FROM season WHERE status='active' ORDER BY id DESC "
                       "LIMIT 1").fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM season ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def todays_evenings(conn, when: dt.datetime | None = None) -> list[dict]:
    when = ist(when or dt.datetime.now(IST))
    sid = season_id(conn)
    if sid is None:
        return []
    rows = conn.execute(
        "SELECT * FROM evening WHERE season_id=? AND day=? ORDER BY league",
        (sid, when.date().isoformat())).fetchall()
    return [dict(r) for r in rows]


def authorable_nights(conn, limit: int = 8, now: dt.datetime | None = None) -> list[dict]:
    """Nights staff can still put content into - for the pickers, so nobody ever
    types an evening id or a date.

    `scheduled` is included deliberately: pre-authoring the week ahead is the
    normal workflow, and a night already open still accepts a fourth question if
    staff want to add one before the timer runs. A `locked`/`graded` night is
    excluded because its result has already been paid out.
    """
    when = ist(now or dt.datetime.now(IST)).date().isoformat()
    sid = season_id(conn)
    if sid is None:
        return []
    rows = conn.execute(
        "SELECT e.*, s.name season FROM evening e JOIN season s ON s.id=e.season_id "
        "WHERE e.season_id=? AND e.status IN ('scheduled','open') AND e.day>=? "
        "ORDER BY e.day, e.league LIMIT ?", (sid, when, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        n_q = conn.execute("SELECT COUNT(*) c FROM question WHERE evening_id=?",
                           (r["id"],)).fetchone()["c"]
        d["questions"] = n_q
        d["has_prompt"] = bool(dbmod.cfg(conn, f"prompt:{r['day']}:{r['league']}"))
        out.append(d)
    return out


def flag_submission(conn, submission_id: int, flag: str | None, actor_id: int,
                    note: str = "") -> dict:
    """Mark an entry as AI-written ('ai'), or clear the mark (None).

    There is deliberately NO detector in this bot. Model-written prose is not
    reliably separable from a rushed human paragraph, and a false positive here
    takes a player's night away from them on a statistic. So the rule is stated on
    the card, the near-duplicate check below catches copy-paste between players, and
    this field records the staff decision - who flagged it, when, and what was said.
    """
    if flag not in (None, "ai"):
        raise ValueError("only 'ai' is a valid flag (or None to clear it)")
    row = conn.execute("SELECT * FROM submission WHERE id=?", (submission_id,)).fetchone()
    if not row:
        raise ValueError("submission not found")
    with conn:
        conn.execute("UPDATE submission SET flag=? WHERE id=?", (flag, submission_id))
        dbmod.audit(conn, "submission.flag", actor_id, submission_id,
                    {"flag": row["flag"]}, {"flag": flag, "note": note})
    return {"submission_id": submission_id, "flag": flag, "player_id": row["player_id"],
            "previously": row["flag"]}


def set_scenario_prompt(conn, day: str, league: str, text: str,
                        deadline: str | None = None, actor_id: int | None = None) -> dict:
    """The L2/L3 card for a night, plus an optional per-night deadline.

    Returns `deadline_applied=False` when the night has already posted: changing
    the stored deadline then would leave the visible card lying about its own
    cut-off, so staff are told to refresh the card instead of being quietly
    mis-rounded."""
    if league == "l1":
        raise ValueError("League 1 has questions, not a prompt")
    ev = conn.execute("SELECT id, status, closes_at FROM evening WHERE day=? AND league=?",
                      (day, league)).fetchone()
    if not ev:
        raise ValueError(f"no {league.upper()} evening on {day}")
    if not (text or "").strip():
        raise ValueError("the prompt is empty")
    with conn:
        dbmod.set_cfg(conn, f"prompt:{day}:{league}", text.strip())
        applied = False
        if deadline:
            hh, mm = (deadline.strip().split(":") + ["0"])[:2]
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError("deadline must be HH:MM")
            opens = parse_iso(conn.execute("SELECT opens_at FROM evening WHERE id=?",
                                           (ev["id"],)).fetchone()["opens_at"])
            new_close = opens.replace(hour=int(hh), minute=int(mm), second=0)
            if new_close <= opens:
                raise ValueError("the deadline has to be after the 16:00 opening")
            if ev["status"] in ("scheduled", "open"):
                conn.execute("UPDATE evening SET closes_at=? WHERE id=?",
                             (new_close.isoformat(timespec="seconds"), ev["id"]))
                applied = True
            for q in conn.execute("SELECT id, answer_deadline FROM question WHERE evening_id=?",
                                  (ev["id"],)).fetchall():
                conn.execute("UPDATE question SET answer_deadline=? WHERE id=?",
                             (new_close.isoformat(timespec="seconds"), q["id"]))
        dbmod.audit(conn, "scenario.set", actor_id, ev["id"], None,
                    {"day": day, "league": league, "chars": len(text),
                     "deadline": deadline, "deadline_applied": applied})
    return {"evening_id": ev["id"], "day": day, "league": league,
            "deadline_applied": applied, "posted": ev["status"] not in ("scheduled",)}


def board_panels(conn) -> list[dict]:
    """Every pinned leaderboard panel, so an award can refresh them all."""
    return [dict(r) for r in conn.execute(
        "SELECT key, value FROM config WHERE key LIKE 'board:%' ORDER BY key").fetchall()]


def pending_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM payout WHERE status='pending'").fetchone()["c"]


def open_evening(conn, evening_id: int, channel_id: int, actor_id: int) -> dict:
    """Flips status scheduled -> open. Safe to call twice, safe to call after a
    restart at any point during the evening; that is what makes the panel
    recoverable rather than re-creatable."""
    ev = conn.execute("SELECT * FROM evening WHERE id=?", (evening_id,)).fetchone()
    if not ev:
        raise ValueError("evening not found")
    if ev["status"] != "scheduled":
        return {"evening": dict(ev), "reopened": False, "already": True}
    with conn:
        conn.execute("UPDATE evening SET status='open', channel_id=?, posted_by=? WHERE id=?",
                     (channel_id, actor_id, evening_id))
        dbmod.audit(conn, "evening.open", actor_id, evening_id,
                    {"status": "scheduled"}, {"status": "open"})
    return {"evening": dict(conn.execute("SELECT * FROM evening WHERE id=?",
                                         (evening_id,)).fetchone()),
            "reopened": False, "already": False}


def close_evening(conn, evening_id: int, actor_id: int | None = None,
                  reason: str = "timer", now: dt.datetime | None = None) -> dict:
    ev = conn.execute("SELECT * FROM evening WHERE id=?", (evening_id,)).fetchone()
    if not ev:
        raise ValueError("evening not found")
    if ev["status"] == "scheduled":
        raise ValueError("cannot close an evening that never opened")
    with conn:
        conn.execute("UPDATE evening SET status='locked' WHERE id=? AND status='open'",
                     (evening_id,))
        # A manual close IS the deadline. Without moving closes_at, an evening
        # closed at 18:00 still advertises "closes 20:00", and every submission
        # between now and 20:00 would be stamped as an after-hours edit (x0.5)
        # for a freeze the player was told about in nobody's language but ours.
        stamp_dt = ist(now) if now is not None else ist(dt.datetime.now(IST))
        opens_at = parse_iso(ev["opens_at"])
        # Move the deadline to the moment it actually closed - EARLIER when staff
        # close early, later when a night runs long. Never before the opening.
        if stamp_dt > opens_at and stamp_dt != parse_iso(ev["closes_at"]):
            stamp = stamp_dt.isoformat(timespec="seconds")
            conn.execute("UPDATE evening SET closes_at=? WHERE id=?", (stamp, evening_id))
            ev = conn.execute("SELECT * FROM evening WHERE id=?",
                              (evening_id,)).fetchone()
        if ev["league"] == "l1":
            # freeze every L1 question so late edits are impossible, not merely flagged
            conn.execute("UPDATE question SET answer_deadline=? WHERE evening_id=?",
                         (ev["closes_at"], evening_id))
        conn.execute("UPDATE submission SET edit_after_close=1 WHERE evening_id=? "
                     "AND edited_at IS NOT NULL AND edited_at > ?",
                     (evening_id, int(parse_iso(ev["closes_at"]).timestamp())))
        dbmod.audit(conn, "evening.close", actor_id, evening_id,
                    {"status": ev["status"]}, {"status": "locked", "reason": reason})
    return {"evening": dict(conn.execute("SELECT * FROM evening WHERE id=?",
                                         (evening_id,)).fetchone())}


def default_points(tier: str) -> int:
    """What to pre-fill in the staff modal: the BASE value for this question.

    Every night pays the same now, so there is exactly one number per tier and
    nothing to inflate in your head. A SUGGESTION the staff member can override
    for a question that turned out easier or meaner than intended.
    """
    return int(sc.L1_TIER_POINTS.get(tier, 6))


def set_question_points(conn, question_id: int, base: int, actor_id: int | None = None,
                        why: str | None = None) -> int:
    """Staff decide the number; the bot owns everything derived from it.

    Audited, because "why did that question pay 8" is the single most common
    appeal, and the answer should be a row in the database, not a memory."""
    if not 0 <= int(base) <= 40:
        raise ValueError("base points must be 0-40")
    before = conn.execute("SELECT points_per_correct, tier, evening_id FROM question "
                          "WHERE id=?", (question_id,)).fetchone()
    if before is None:
        raise ValueError("question not found")
    with conn:
        conn.execute("UPDATE question SET points_per_correct=? WHERE id=?",
                     (int(base), question_id))
        dbmod.audit(conn, "question.points", actor_id, question_id,
                    {"points_per_correct": before["points_per_correct"],
                     "tier": before["tier"]},
                    {"points_per_correct": int(base), "why": why,
                     "evening_id": before["evening_id"]})
    return int(base)


def suggested_points(conn, question_id: int) -> dict:
    """What the modal should offer: the tier default, or whatever staff already set.

    No Sunday adjustment - a night pays its own per-question points and nothing else,
    so what the staff member sees IS what gets paid (before the speed bonus the bot
    works out on its own)."""
    q = conn.execute(
        "SELECT q.tier, q.points_per_correct FROM question q WHERE q.id=?",
        (question_id,)).fetchone()
    if q is None:
        raise ValueError("question not found")
    base = int(q["points_per_correct"]) if q["points_per_correct"] is not None \
        else default_points(q["tier"])
    return {"tier": q["tier"], "base": base, "multiplier": 1.0, "after_grand": base}


def add_question(conn, evening_id: int, ordinal: int | None, prompt: str,
                 options: list[str],
                 tier: str = "medium", kind: str = "mcq", image_url: str | None = None,
                 explanation: str | None = None,
                 points_per_correct: int | None = None) -> dict:
    ev = conn.execute("SELECT opens_at, status, league, day FROM evening WHERE id=?",
                      (evening_id,)).fetchone()
    if not ev:
        raise ValueError("evening not found")
    if ev["league"] != "l1":
        # League 2 and 3 are submission nights. A question authored onto one is
        # never posted, never graded and never scored - it just sits there while
        # staff believe it is live, so refuse at authoring time instead.
        raise ValueError(f"evening {evening_id} is a {ev['league'].upper()} night - only "
                         "League 1 has questions. Set that night's card with "
                         f"/scenario-set day:{ev['day']} league:{ev['league']} instead")
    # 'scheduled' is allowed on purpose: pre-authoring tonight's questions in the
    # morning, hours before the bot posts them, is the normal staff workflow and
    # there is no race to lose - grading and the answer deadline are guarded where
    # they matter. Only a night that has been locked or graded refuses new content,
    # because adding a question then would silently change a result already paid out.
    # Without this the insert dies as "FOREIGN KEY constraint failed" (question's
    # own evening guard) and nobody can tell why.
    if ev["status"] in ("locked", "graded"):
        raise ValueError(f"evening {evening_id} is {ev['status']!r}; it has already run, so "
                         "its questions are frozen. Open a fresh evening instead")
    if tier not in sc.L1_TIER_POINTS:
        raise ValueError(f"tier must be one of {sorted(sc.L1_TIER_POINTS)}")
    if not 2 <= len(options) <= 6:
        raise ValueError("an MCQ needs between 2 and 6 options (Discord allows 6 buttons "
                         "comfortably on one card)")
    # set_cfg JSON-encodes, so read it back through cfg() - int() on the raw
    # column value yields '"120"' and raises.
    ans_secs = answer_window(conn)
    deadline = parse_iso(ev["opens_at"]) + dt.timedelta(seconds=ans_secs)
    if points_per_correct is not None and not 0 <= int(points_per_correct) <= 40:
        raise ValueError("per-question points must be 0-40 (40 = a hard Sunday question)")
    if ordinal is None or int(ordinal) <= 0:
        # Auto-slot. This must NOT fall through to 0: question is UNIQUE
        # (evening_id, ordinal) and the insert is `OR REPLACE`, so every extra
        # "question 0" would silently DELETE the one before it. One lost question
        # per authored night, and no error anywhere.
        ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM question WHERE "
                               "evening_id=?", (evening_id,)).fetchone()[0]
    ordinal = int(ordinal)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO question(evening_id,ordinal,kind,prompt,options,image_url,"
            "explanation,tier,answer_deadline,points_per_correct) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (evening_id, ordinal, kind, prompt, json.dumps(options), image_url,
             explanation, tier, iso(deadline), points_per_correct))
        qid = conn.execute("SELECT id FROM question WHERE evening_id=? AND ordinal=?",
                           (evening_id, ordinal)).fetchone()["id"]
    return {"question_id": qid, "ordinal": ordinal, "options": options, "tier": tier,
            "suggested_points": points_per_correct if points_per_correct is not None
            else default_points(tier),
            "answer_deadline": iso(deadline)}


def set_question_message(conn, question_id: int, message_id: int) -> None:
    with conn:
        conn.execute("UPDATE question SET message_id=?, posted_at=? WHERE id=?",
                     (message_id, dbmod.now(), question_id))


# --------------------------------------------------------------------------- #
# answers
# --------------------------------------------------------------------------- #

def submit_answer(conn, question_id: int, player_id: int, option_idx: int | None,
                  payload: str | None = None) -> dict:
    q = conn.execute("SELECT q.*, e.status AS ev_status, e.opens_at "
                     "FROM question q JOIN evening e ON e.id=q.evening_id WHERE q.id=?",
                     (question_id,)).fetchone()
    if not q:
        raise ValueError("question not found")
    if q["ev_status"] not in ("open",):
        return {"accepted": False, "why": "locked",
                "message": "Answers are locked for that evening."}
    if q["correct_option"] is not None:
        return {"accepted": False, "why": "graded",
                "message": "That question is already graded."}
    text = payload if payload is not None else str(option_idx)
    now_ts = dbmod.now()
    with conn:
        try:
            cur = conn.execute(
                "INSERT INTO entry(evening_id,question_id,player_id,payload,option_idx,"
                "submitted_at) VALUES(?,?,?,?,?,?)",
                (q["evening_id"], question_id, player_id, text, option_idx, now_ts))
        except sqlite3.IntegrityError:
            # UNIQUE(question_id, player_id): the player is changing their mind.
            # Editing while OPEN is allowed and free (fixing a typo is not cheating).
            prev = conn.execute("SELECT * FROM entry WHERE question_id=? AND player_id=?",
                                (question_id, player_id)).fetchone()
            conn.execute("UPDATE entry SET payload=?, option_idx=?, edited_at=? "
                         "WHERE id=?", (text, option_idx, now_ts, prev["id"]))
            return {"accepted": True, "change": "edited", "entry_id": prev["id"]}
    return {"accepted": True, "change": "created", "entry_id": cur.lastrowid}


def clear_answer(conn, question_id: int, player_id: int) -> bool:
    q = conn.execute("SELECT qq.correct_option, qq.answer_deadline, e.status AS ev_status "
                     "FROM question qq JOIN evening e ON e.id=qq.evening_id WHERE qq.id=?",
                     (question_id,)).fetchone()
    if not q:
        raise ValueError("question not found")
    # Status first. Once staff have graded, correct_option is set and the deadline may
    # still be in the future - without this check a player could DELETE their answer
    # after grading and silently un-pay themselves (or wipe a graded result).
    if q["ev_status"] != "open":
        return False
    if q["correct_option"] is not None or parse_iso(q["answer_deadline"]) <= dt.datetime.now(IST):
        return False
    with conn:
        cur = conn.execute("DELETE FROM entry WHERE question_id=? AND player_id=?",
                           (question_id, player_id))
    return cur.rowcount > 0


def mark_edit_after_close(conn, question_id: int, player_id: int, edited_ts: int) -> None:
    """Only ever called after the freeze, so it flags rather than rewrites."""
    with conn:
        conn.execute("UPDATE entry SET edited_at=?, edit_after_close=1 "
                     "WHERE question_id=? AND player_id=?",
                     (edited_ts, question_id, player_id))


# --------------------------------------------------------------------------- #
# L1 grading: staff picks the right option, the bot does ALL arithmetic
# --------------------------------------------------------------------------- #

def grade_question(conn, question_id: int, correct_option: int | None,
                   actor_id: int | None = None, base_points: int | None = None) -> dict:
    """Staff decision: which option is right, and what this question is worth.

    Base value wins in this order: the value passed now > the value authored on the
    question > the tier default. Speed bonus, first-correct +1 and the Sunday x1.5
    are then applied by the bot - that split is the whole point of the design.
    """
    """Idempotent by construction: we delete that question's awards and rebuild them.

    A host double-clicking, or the bot restarting mid-grade, can never pay twice.
    """
    q = conn.execute("SELECT q.*, e.multiplier, e.season_id, e.league, e.day, "
                     "e.graded_by, e.id AS evening_id "
                     "FROM question q JOIN evening e ON e.id=q.evening_id WHERE q.id=?",
                     (question_id,)).fetchone()
    if not q:
        raise ValueError("question not found")
    n_opts = len(dbmod.json_list(q["options"]))
    if correct_option is not None and not 0 <= correct_option < n_opts:
        raise ValueError(f"correct option must be 0..{n_opts - 1}")
    # No status filter: an `entry` row IS an answer, there is nothing else an entry
    # can be. (This query once said `AND status='submitted'` against a column that
    # never existed - it only worked because a stale hub.db-wal carried an old
    # schema. A fresh database raised immediately, which is the honest behaviour.)
    rows = conn.execute("SELECT id, player_id, option_idx, submitted_at FROM entry "
                        "WHERE question_id=?", (question_id,)).fetchall()
    base = int(base_points if base_points is not None
               else (q["points_per_correct"]
                     if q["points_per_correct"] is not None
                     else default_points(q["tier"])))
    if not 0 <= base <= 40:
        raise ValueError("per-question points must be 0-40")

    awarded = 0
    winners: list[tuple[int, int]] = []
    rate = conn.execute("SELECT coins_per_point c FROM season WHERE id=?",
                        (q["season_id"],)).fetchone()["c"]
    with conn:
        # Re-grade = delete this question's awards, then rebuild every affected
        # player's evening total from the remaining awards. Never touch a
        # teammate question's ledger row, or grading Q2 would erase Q1's points.
        affected = [r["player_id"] for r in rows]
        conn.execute("DELETE FROM award WHERE question_id=?", (question_id,))
        who = actor_id if actor_id is not None else q["graded_by"]
        conn.execute("UPDATE question SET correct_option=?, points_awarded=0 WHERE id=?",
                     (correct_option, question_id))
        conn.execute("UPDATE evening SET graded_by=? WHERE id=? AND ? IS NOT NULL",
                     (who, q["evening_id"], who))
        if correct_option is not None:
            posted_at = q["posted_at"] or 0
            correct_rows = []
            for r in rows:
                if r["option_idx"] == correct_option:
                    t = max(0.0, r["submitted_at"] - posted_at) if posted_at else None
                    correct_rows.append((r["player_id"], t))
            fastest = min((t for _, t in correct_rows if t is not None), default=None)
            per_player: dict[int, int] = {}
            for pid, t in correct_rows:
                first = fastest is not None and t is not None and abs(t - fastest) < 1e-9
                raw = float(base) + sc.speed_bonus(t) + (sc.FIRST_CORRECT_BONUS if first else 0.0)
                pts = max(0, int(raw * q["multiplier"] + 0.5))
                conn.execute(
                    "INSERT INTO award(evening_id,player_id,entry_id,question_id,raw,bonus,"
                    "multiplier,points,coins,reason,applied_by,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (q["evening_id"], pid, None, question_id, raw, 0.0, q["multiplier"], pts,
                     pts * rate, "l1_auto", who, dbmod.now()))
                per_player[pid] = per_player.get(pid, 0) + pts
            awarded = sum(per_player.values())
            winners = sorted(per_player.items(), key=lambda kv: -kv[1])
            conn.execute("UPDATE question SET points_awarded=? WHERE id=?",
                         (awarded, question_id))
        everyone = [r["player_id"] for r in conn.execute(
            "SELECT DISTINCT player_id FROM entry WHERE evening_id=?",
            (q["evening_id"],)).fetchall()]
        for pid in dict.fromkeys(affected + [p for p, _ in winners] + everyone):
            total = conn.execute(
                "SELECT COALESCE(SUM(points),0) t FROM award WHERE evening_id=? AND player_id=?",
                (q["evening_id"], pid)).fetchone()["t"]
            conn.execute("INSERT OR REPLACE INTO ledger(player_id,evening_id,season_id,league,"
                         "day,points) VALUES(?,?,?,?,?,?)",
                         (pid, q["evening_id"], q["season_id"], "l1", q["day"], total))
        dbmod.audit(conn, "question.grade", who, question_id,
                    {"correct_option": None}, {"correct_option": correct_option,
                                               "base_points": base, "multiplier": q["multiplier"],
                                               "points": awarded, "players": len(winners)})
    total = conn.execute("SELECT COUNT(*) c FROM question WHERE evening_id=? AND "
                         "correct_option IS NULL", (q["evening_id"],)).fetchone()["c"]
    return {"question_id": question_id, "correct_option": correct_option,
            "players_scored": len(winners), "points_this_question": awarded,
            "winners": winners, "questions_left_ungraded": total,
            "evening_complete": total == 0}


def grade_all_correct(conn, evening_id: int, actor_id: int) -> list[dict]:
    """Fallback for a botched night: pay nobody, close cleanly, log loudly."""
    out = []
    qs = conn.execute("SELECT id FROM question WHERE evening_id=?", (evening_id,)).fetchall()
    with conn:
        conn.execute("UPDATE evening SET graded_by=?, graded_at=?, status='graded' WHERE id=?",
                     (actor_id, dbmod.now(), evening_id))
    for q in qs:
        out.append(grade_question(conn, q["id"], None))
    dbmod.audit(conn, "evening.void_grading", actor_id, evening_id, None,
                {"note": "all questions marked 'no correct option'"})
    return out


def finalize_l1(conn, evening_id: int, actor_id: int) -> dict:
    left = conn.execute("SELECT COUNT(*) c FROM question WHERE evening_id=? AND "
                        "correct_option IS NULL", (evening_id,)).fetchone()["c"]
    if left:
        raise ValueError(f"{left} question(s) still ungraded - grade them or use "
                         "'No correct option' on each")
    with conn:
        conn.execute("UPDATE evening SET status='graded', graded_by=?, graded_at=? WHERE id=?",
                     (actor_id, dbmod.now(), evening_id))
        dbmod.audit(conn, "evening.finalize", actor_id, evening_id, None, {"league": "l1"})
    return standings_for_evening(conn, evening_id)


# --------------------------------------------------------------------------- #
# L2/L3: one number in a range, bot applies the multiplier
# --------------------------------------------------------------------------- #

def create_submission(conn, evening_id: int, player_id: int, text: str) -> dict:
    ev = conn.execute("SELECT * FROM evening WHERE id=?", (evening_id,)).fetchone()
    if not ev:
        raise ValueError("evening not found")
    if ev["league"] != "l1" and ev["status"] not in ("open", "locked"):
        return {"accepted": False, "why": "closed", "message": "That evening is closed."}
    if ev["league"] == "l1":
        raise ValueError("League 1 uses questions, not submissions")
    words = len((text or "").split())
    with conn:
        before = conn.execute("SELECT * FROM submission WHERE evening_id=? AND player_id=?",
                              (evening_id, player_id)).fetchone()
        conn.execute(
            "INSERT INTO submission(evening_id,player_id,text,word_count,submitted_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(evening_id,player_id) DO UPDATE SET "
            "text=excluded.text, word_count=excluded.word_count, edited_at=excluded.submitted_at",
            (evening_id, player_id, text, words, dbmod.now()))
        row = conn.execute("SELECT * FROM submission WHERE evening_id=? AND player_id=?",
                           (evening_id, player_id)).fetchone()
        after_close = 0
        if before and parse_iso(ev["closes_at"]) < ist(dt.datetime.now(IST)):
            after_close = 1
            conn.execute("UPDATE submission SET edit_after_close=1 WHERE id=?", (row["id"],))
        dbmod.audit(conn, "submission.write", player_id, row["id"],
                    None, {"words": words, "rework": bool(before)})
    dupes = find_duplicates(conn, evening_id)
    return {"accepted": True, "submission_id": row["id"], "word_count": words,
            "rework": bool(before), "edit_after_close": after_close,
            "duplicate_of": dupes.get(row["id"])}


def find_duplicates(conn, evening_id: int, threshold: float = 0.86) -> dict[int, int]:
    """Cheap, explainable similarity: normalised token sets. Deterministic and
    debuggable - do not swap this for an embedding model nobody can audit."""
    rows = conn.execute("SELECT id, player_id, text FROM submission WHERE evening_id=? "
                        "ORDER BY submitted_at", (evening_id,)).fetchall()
    seen: dict[frozenset, int] = {}
    out: dict[int, int] = {}
    for r in rows:
        tokens = frozenset(w for w in "".join(
            c.lower() if c.isalnum() or c.isspace() else " " for c in r["text"]).split()
            if len(w) > 2)
        if not tokens:
            continue
        for other, sid in seen.items():
            j = len(tokens & other) / max(1, len(tokens | other))
            if j >= threshold:
                out[r["id"]] = sid
                break
        else:
            seen[tokens] = r["id"]
    return out


def award_submission(conn, submission_id: int, points_in: int, actor_id: int,
                     note: str | None = None, band: str | None = None) -> dict:
    """The single human judgement in L2/L3: one integer 0-25.

    Band name is optional here; when the UI sends a band we keep both so the
    results post can print 'GOOD 15' rather than a bare number.
    """
    if points_in < L23_MIN or points_in > L23_MAX:
        raise ValueError(f"points must be within the published range "
                         f"{L23_MIN}-{L23_MAX}")
    s = conn.execute(
        "SELECT s.*, e.season_id, e.league, e.day, e.multiplier, e.status AS ev_status, "
        "se.coins_per_point AS coins_rate FROM submission s "
        "JOIN evening e ON e.id = s.evening_id "
        "JOIN season se ON se.id = e.season_id WHERE s.id=?", (submission_id,)).fetchone()
    if not s:
        raise ValueError("submission not found")
    if s["status"] == "void":
        raise ValueError("submission is void and cannot be awarded")
    if s["edit_after_close"]:
        # the rule you published: an edit after the deadline halves the award
        points_in = max(0, int(points_in * sc.EDIT_OVERRIDE_MULTIPLIER + 0.5))
    pts = max(0, int(points_in * s["multiplier"] + 0.5))
    with conn:
        conn.execute("UPDATE submission SET points_in=?, awarded_by=?, awarded_at=?, note=? "
                     "WHERE id=?", (points_in, actor_id, dbmod.now(), note, submission_id))
        # idempotent: one award per submission, replaced on re-grade
        conn.execute("DELETE FROM ledger WHERE evening_id=? AND player_id=? "
                     "AND league != 'adjust'", (s["evening_id"], s["player_id"]))
        conn.execute("INSERT OR REPLACE INTO award(evening_id,player_id,entry_id,raw,bonus,"
                     "multiplier,points,coins,reason,applied_by,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (s["evening_id"], s["player_id"], submission_id, float(points_in), 0.0,
                      s["multiplier"], pts, pts * s["coins_rate"],
                      f"l23:{band or 'manual'}", actor_id, dbmod.now()))
        conn.execute("INSERT OR REPLACE INTO ledger(player_id,evening_id,season_id,league,"
                     "day,points) VALUES(?,?,?,?,?,?)",
                     (s["player_id"], s["evening_id"], s["season_id"], s["league"],
                      s["day"], pts))
        dbmod.audit(conn, "submission.award", actor_id, submission_id,
                    None, {"points_in": points_in, "points": pts, "band": band})
    left = conn.execute("SELECT COUNT(*) c FROM submission WHERE evening_id=? AND "
                        "points_in IS NULL AND status='submitted'",
                        (s["evening_id"],)).fetchone()["c"]
    return {"submission_id": submission_id, "points_in": points_in, "band": band,
            "multiplier": s["multiplier"], "points": pts, "submissions_left": left,
            "evening_complete": left == 0}


def band_for(rubric_total: int) -> str:
    """Name the band for a 0-25 number, derived from the ENGINE's thresholds.

    Same mapping in both directions: 21-25 Excellent, 13-20 Good, 6-12 Average,
    0-5 Poor. Staff press a band button so the number and the label normally agree;
    this is what keeps a CUSTOM value honest on the results card.
    """
    for threshold, band in sc.BAND_THRESHOLDS:
        if rubric_total >= threshold:
            return band
    return "Poor"


# --------------------------------------------------------------------------- #
# PAYOUTS  -  the bot never holds currency
#
# Contract: the bot computes what is owed and queues it in #checkout. A human
# sends the coins (or XP) in your economy bot, then presses CLEAR. There is no
# code path here that can mint, transfer or refund a currency - which is exactly
# why this design survives a buggy deploy.
# --------------------------------------------------------------------------- #

PAYOUT_KINDS = ("coins", "xp")
CHECKOUT_REASON = "season awards"


def queue_payouts(conn, season: int | None = None) -> dict:
    """Turn finished standings into pending checkouts. Idempotent, so staff can
    re-run it any number of times before clearing and never duplicate a payout."""
    sid = season or season_id(conn)
    created, skipped = [], 0
    with conn:
        for kind in PAYOUT_KINDS:
            rate = conn.execute(
                f"SELECT {'coins_per_point' if kind=='coins' else 'xp_per_point'} r "
                f"FROM season WHERE id=?", (sid,)).fetchone()
            if not rate or int(rate["r"]) <= 0:
                continue                      # that economy is not enabled
            for row in standings(conn, "season", season=sid, limit=500):
                amount = row["points"] * int(rate["r"])
                if amount <= 0:
                    continue
                before = conn.execute(
                    "SELECT id FROM payout WHERE season_id=? AND player_id=? AND kind=? "
                    "AND reason=?", (sid, row["player_id"], kind, CHECKOUT_REASON)).fetchone()
                conn.execute(
                    "INSERT INTO payout(season_id,player_id,kind,points,rate,amount,reason,"
                    "status,created_at,content_hash) VALUES(?,?,?,?,?,?,?,'pending',?,?) "
                    "ON CONFLICT(season_id,player_id,kind,reason) DO NOTHING",
                    (sid, row["player_id"], kind, row["points"], int(rate["r"]), amount,
                     CHECKOUT_REASON, dbmod.now(), f"{amount}:{kind}"))
                now_row = conn.execute(
                    "SELECT * FROM payout WHERE season_id=? AND player_id=? AND kind=? AND reason=?",
                    (sid, row["player_id"], kind, CHECKOUT_REASON)).fetchone()
                if before is None:
                    created.append(dict(now_row))
                else:
                    skipped += 1
        dbmod.audit(conn, "payout.queue", None, sid, None,
                    {"created": len(created), "already_queued": skipped})
    return {"created": len(created), "already_queued": skipped,
            "total_pending": len(pending_payouts(conn)), "season_id": sid}


def pending_payouts(conn, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        # Coins first: they are the rows a human has to go and actually send, so
        # the top of the channel is where the work is. Amount descending within
        # each kind, so nobody waits for a 10-coin payout to clear a 5,000 one.
        "SELECT p.*, s.name season_name FROM payout p JOIN season s ON s.id=p.season_id "
        "WHERE p.status='pending' "
        "ORDER BY (p.kind='coins') DESC, p.amount DESC, p.id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def payout_batches(conn, per_message: int = 10) -> list[list[dict]]:
    """Chunk the queue so a view never exceeds 25 buttons (10 rows + 2 nav here)."""
    rows = pending_payouts(conn)
    return [rows[i:i + per_message] for i in range(0, max(1, len(rows)), per_message)]


def clear_payout(conn, payout_id: int, actor_id: int) -> dict:
    """'I have sent it.' The only way anything leaves the queue.

    Status-guarded, so two staff members pressing CLEAR at once cannot both
    consume it - the second gets 'already cleared', not a double send.
    """
    with conn:
        row = conn.execute("SELECT * FROM payout WHERE id=?", (payout_id,)).fetchone()
        if not row:
            raise ValueError("payout not found")
        if row["status"] == "cleared":
            return {"cleared": False, "already": True, "player_id": row["player_id"],
                    "amount": row["amount"], "kind": row["kind"]}
        conn.execute("UPDATE payout SET status='cleared', cleared_by=?, cleared_at=? WHERE id=? "
                     "AND status='pending'", (actor_id, dbmod.now(), payout_id))
        if row["kind"] == "coins":
            dbmod.add_coins(conn, row["player_id"], row["amount"],
                            f"season {row['season_id']} payout #{payout_id}")
        dbmod.audit(conn, "payout.clear", actor_id, payout_id, {"status": "pending"},
                    {"status": "cleared", "player": row["player_id"], "amount": row["amount"],
                     "kind": row["kind"]})
        left = conn.execute("SELECT COUNT(*) c FROM payout WHERE status='pending'").fetchone()["c"]
    return {"cleared": True, "already": False, "player_id": row["player_id"],
            "amount": row["amount"], "kind": row["kind"], "remaining": left}


def payout_summary(conn, player_id: int) -> dict:
    pend = conn.execute("SELECT kind, COALESCE(SUM(amount),0) a FROM payout WHERE "
                        "player_id=? AND status='pending' GROUP BY kind",
                        (player_id,)).fetchall()
    life = conn.execute("SELECT COALESCE(SUM(points),0) p, COUNT(*) n FROM ledger WHERE "
                        "player_id=?", (player_id,)).fetchone()
    return {"pending": {r["kind"]: r["a"] for r in pend},
            "lifetime_points": life["p"], "nights": life["n"]}


# --------------------------------------------------------------------------- #
# standings / season end
# --------------------------------------------------------------------------- #

def standings_for_evening(conn, evening_id: int) -> dict:
    rows = conn.execute(
        "SELECT l.player_id, SUM(l.points) pts, COUNT(*) n FROM ledger l "
        "WHERE l.evening_id=? GROUP BY l.player_id ORDER BY pts DESC, l.player_id LIMIT 10",
        (evening_id,)).fetchall()
    return {"evening_id": evening_id,
            "top": [(r["player_id"], r["pts"]) for r in rows]}


def standings(conn, scope: str = "league", season: int | None = None,
              league: str | None = None, limit: int = 25) -> list[dict]:
    """Derived view over the ledger - there is no stored table to go stale.

    scope='league'  one league, this season   (needs the night floor for a podium)
    scope='season'  all leagues, this season   (monthly points)
    scope='total'   all leagues, all seasons   (lifetime, never resets)
    """
    if scope == "total":
        floor = 0
        rows = conn.execute(
            "SELECT player_id, SUM(points) AS points, COUNT(DISTINCT day||league) AS nights, "
            "MAX(points) AS best_evening, COUNT(DISTINCT season_id) AS seasons FROM ledger "
            "GROUP BY player_id ORDER BY points DESC, best_evening DESC, player_id ASC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) | {"rank": i + 1} for i, r in enumerate(rows)]
    season = season or season_id(conn)
    floor = int(dbmod.cfg(conn, "standings_night_floor", sc.STANDINGS_NIGHT_FLOOR)) \
        if scope == "league" else 0
    where, params = ["l.season_id=?"], [season]
    if scope == "league" and league:
        where.append("l.league=?")
        params.append(league)
    rows = conn.execute(
        f"SELECT l.player_id, SUM(l.points) AS points, COUNT(DISTINCT l.evening_id) AS nights, "
        f"MAX(l.points) AS best_evening FROM ledger l WHERE {' AND '.join(where)} "
        f"GROUP BY l.player_id HAVING nights >= ? ORDER BY points DESC, best_evening DESC, "
        f"nights DESC, l.player_id ASC LIMIT ?", (*params, floor, limit)).fetchall()
    return [dict(r) | {"rank": i + 1} for i, r in enumerate(rows)]


def league_table_size(conn, league: str, season: int | None = None) -> dict:
    """How many players the floor excludes - so the panel can say so honestly
    instead of silently hiding half the server."""
    season = season or season_id(conn)
    floor = int(dbmod.cfg(conn, "standings_night_floor", sc.STANDINGS_NIGHT_FLOOR))
    rows = conn.execute(
        "SELECT COUNT(*) AS all_players, "
        "SUM(CASE WHEN nights >= ? THEN 1 ELSE 0 END) AS qualified FROM ("
        "  SELECT player_id, COUNT(*) nights FROM ledger WHERE season_id=? AND league=?"
        "  GROUP BY player_id)", (floor, season, league)).fetchone()
    all_n = rows["all_players"] or 0
    qual = rows["qualified"] or 0
    return {"all": all_n, "qualified": qual, "floor": floor, "excluded": max(0, all_n - qual)}


def final_qualifiers(conn, league: str, n: int = 8) -> list[dict]:
    return standings(conn, "league", league=league, limit=n)


def season_report(conn, season: int | None = None) -> dict:
    """`season` must be passed by anything acting ON a specific season. Left to
    `season_id()` this silently reports the NEWEST season, so closing Season 1 after
    Season 2 already exists reports an empty table and crowns nobody."""
    sid = season or season_id(conn)
    out: dict = {"season_id": sid, "leagues": {},
                 "overall": standings(conn, "season", season=sid, limit=25)}
    for league in ("l1", "l2", "l3"):
        out["leagues"][league] = standings(conn, "league", league=league, season=sid, limit=25)
    out["minted_coins"] = conn.execute(
        "SELECT COALESCE(SUM(a.coins),0) c FROM award a JOIN evening e ON e.id=a.evening_id "
        "WHERE e.season_id=?", (sid,)).fetchone()["c"]
    out["players"] = conn.execute(
        "SELECT COUNT(DISTINCT player_id) c FROM ledger WHERE season_id=?",
        (sid,)).fetchone()["c"]
    out["nights_played"] = conn.execute(
        "SELECT COUNT(*) c FROM evening WHERE season_id=? AND status='graded'",
        (sid,)).fetchone()["c"]
    return out


LEAGUE_ROLE_COLUMNS = {"l1": "role_trivia", "l2": "role_strategy", "l3": "role_hangar"}


def _role_ids_for_season(conn, season_pk: int, league: str) -> list[int]:
    """Which role ids could hold this season's `league` champion.

    Two sources, unioned, because either alone is a lie:
      * `hall_of_fame.role_id` - what the bot actually attached. Historically this
        was NEVER written, and keying removal on it is exactly why champion roles
        piled up: the query found nothing, so nothing was ever removed.
      * `season.role_*` - what the season was configured with. Covers seasons whose
        HoF row predates stamping, and covers config the bot no longer knows about.
    """
    out: list[int] = []
    row = conn.execute(f"SELECT {LEAGUE_ROLE_COLUMNS[league]} r FROM season WHERE id=?",
                       (season_pk,)).fetchone()
    if row and row["r"]:
        out.append(int(row["r"]))
    for h in conn.execute("SELECT role_id FROM hall_of_fame WHERE season_id=? AND league=? "
                          "AND placement=1 AND role_id IS NOT NULL", (season_pk, league)):
        if int(h["role_id"]) not in out:
            out.append(int(h["role_id"]))
    return out


def stale_roles(conn, up_to_season: int) -> list[dict]:
    """Every champion role the bot may have granted in ANY earlier season.

    Scans all closed seasons, not just the immediately-previous one: with a
    one-season lookahead a skipped, failed or re-run rollover leaves a 'Season 1
    Champion' on someone's profile forever, which is precisely the pile-up that
    makes seasonal roles worthless.
    """
    stale: list[dict] = []
    # Only seasons the bot itself crowned (they have a Hall of Fame champion row).
    # A role a human granted by hand is not the bot's to revoke - it is reported as
    # `manual` instead, so an admin's deliberate gesture survives a rollover.
    champs = conn.execute(
        "SELECT h.season_id, h.league, h.player_id, s.name FROM hall_of_fame h "
        "JOIN season s ON s.id = h.season_id WHERE h.placement=1 AND h.season_id<? "
        "ORDER BY h.season_id, h.league", (up_to_season,)).fetchall()
    for ch in champs:
        ids = _role_ids_for_season(conn, ch["season_id"], ch["league"])
        if not ids:
            stale.append({"action": "manual", "player_id": ch["player_id"], "role_id": None,
                          "why": f"{ch['name']} {ch['league']} champion - no role configured, "
                                 f"nothing to remove (if a human granted it, leave it)"})
            continue
        for role in ids:
            stale.append({"action": "remove", "player_id": ch["player_id"], "role_id": role,
                          "why": f"{ch['name']} {ch['league']} champion - seasonal role, "
                                 f"must not outlive the season"})
    return stale


def role_diff(conn, season: int | None = None) -> list[dict]:
    """Dry run. Returns exactly what will change, touches nothing.

    `season` is the season BEING CLOSED. Resolving it from the active/newest season
    instead is how a rollover ends up awarding the next season's champion into the
    old season's role, or awarding nobody at all.
    """
    sid = season or season_id(conn)
    actions: list[dict] = []
    for league, col in LEAGUE_ROLE_COLUMNS.items():
        top = standings(conn, "league", league=league, season=sid, limit=1)
        role = conn.execute(f"SELECT {col} r FROM season WHERE id=?", (sid,)).fetchone()["r"]
        if not role:
            actions.append({"action": "skip", "player_id": None, "role_id": None,
                            "why": f"{league}: no role_id configured for this season"})
        elif not top or top[0]["points"] <= 0:
            # never invent a champion for a league that produced no scored nights
            actions.append({"action": "skip", "player_id": None, "role_id": None,
                            "why": f"{league}: nobody has points - award manually or void the league"})
        else:
            actions.append({"action": "add", "player_id": top[0]["player_id"], "role_id": role,
                            "league": league, "why": f"{league} champion S{sid}"})
    # Removals are computed unconditionally - a season that produces NO new champion
    # (every league skipped above) must still strip last season's roles. Tying removal
    # to a successful award is the other half of the pile-up bug.
    actions.extend(stale_roles(conn, sid))
    return actions


def _stamp_hof_role(conn, player_id: int, league: str, role_id: int, season: int) -> None:
    """Record which role the bot attached, so the HoF row is self-describing and a
    later config edit cannot orphan the old assignment."""
    conn.execute("UPDATE hall_of_fame SET role_id=? WHERE season_id=? AND league=? AND "
                 "placement=1 AND player_id=?",
                 (role_id, season, league, player_id))


async def finalize_season(conn, actor_id: int, apply_roles: bool = False,
                          guild=None) -> dict:
    """Idempotent: hall_of_fame has UNIQUE(season_id,league,placement), so re-running
    this cannot double-post a champion."""
    # The season being closed: the active one, or - if a rollover already ran and
    # nothing is active - the newest. Every step below uses THIS id; none of them is
    # allowed to re-resolve it, or a re-run after a rollover acts on the wrong season.
    sid = closing_season_id(conn)
    if not sid:
        raise ValueError("no season to close - create one first")
    report = season_report(conn, sid)
    hall_rows = []
    for league in ("l1", "l2", "l3"):
        # only players who actually scored make the wall - otherwise a 3-person
        # league hands 3rd place (and a HoF entry) to someone on 0 points.
        for row in [r for r in report["leagues"][league] if r["points"] > 0][:3]:
            hall_rows.append((sid, league, row["player_id"], row["rank"], row["points"]))
    with conn:
        for _sid, league, pid, place, pts in hall_rows:
            # ON CONFLICT UPDATE (not INSERT OR REPLACE): a re-run must never
            # destroy the post_id / role_id already attached to a champion.
            conn.execute(
                "INSERT INTO hall_of_fame(season_id,league,player_id,placement,points) "
                "VALUES(?,?,?,?,?) ON CONFLICT(season_id,league,placement) DO UPDATE SET "
                "player_id=excluded.player_id, points=excluded.points",
                (_sid, league, pid, place, pts))
        conn.execute("UPDATE season SET status='closed' WHERE id=?", (sid,))
        dbmod.audit(conn, "season.finalize", actor_id, sid, None,
                    {"champions": [(r[1], r[2]) for r in hall_rows if r[3] == 1],
                     "apply_roles": apply_roles})
    diff = role_diff(conn, sid)
    applied = []
    if apply_roles and guild is not None:
        applied = await _apply_roles(guild, diff, conn, sid)
    return {"season_id": sid, "overall": report["overall"][:10], "leagues": report["leagues"],
            "role_diff": diff, "roles_applied": applied, "hall_of_fame": hall_rows,
            "dry_run": not apply_roles}


async def _apply_roles(guild, diff: list[dict], conn=None, season: int | None = None) -> list[str]:
    """Actually moves roles. Returns a human-readable log; every skip is reported,
    never swallowed - a silently-failed role assignment is the single most visible
    way this kind of bot loses a community's trust."""
    done: list[str] = []
    for act in diff:
        if act["action"] not in ("add", "remove") or not act.get("player_id"):
            continue          # skip / manual: nothing to do, and never look up a
                              # None member - that is a wasted API call per skip
        member = guild.get_member(act["player_id"]) or await _fetch(guild, act["player_id"])
        role = guild.get_role(act["role_id"])
        if not member or not role:
            done.append(f"⏭️ skipped {act['action']} <@{act['player_id']}>: "
                        f"{'member not found' if not member else 'role not found'}")
            continue
        try:
            if act["action"] == "add" and role not in member.roles:
                await member.add_roles(role, reason=f"Hub Knowledge Season · {act['why']}")
                done.append(f"➕ {role.name} → {member}")
                if conn is not None and act.get("league") and season:
                    _stamp_hof_role(conn, member.id, act["league"], role.id, season)
            elif act["action"] == "remove" and role in member.roles:
                await member.remove_roles(role, reason=f"Hub Knowledge Season · {act['why']}")
                done.append(f"➖ {role.name} ← {member}")
            else:
                done.append(f"·  {act['action']} {role.name} {member} already correct")
        except discord.HTTPException as e:      # missing Manage Roles, hierarchy, etc.
            done.append(f"⚠️ FAILED {act['action']} {role.name} {member}: {e}")
    return done


async def _fetch(guild, user_id: int):
    try:
        return await guild.fetch_member(user_id)
    except Exception:                            # noqa: BLE001
        return None


def points_history(conn, player_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT a.*, e.day, e.league, e.is_sunday FROM award a JOIN evening e "
        "ON e.id=a.evening_id WHERE a.player_id=? ORDER BY a.id DESC LIMIT 50",
        (player_id,)).fetchall()
    return [dict(r) for r in rows]


def adjust_points(conn, player_id: int, delta: int, reason: str, actor_id: int) -> dict:
    """A correction, never an edit of the graded row: the graded points stay
    exactly as the bot computed them and the delta sits beside them, so an
    appeal can be answered by showing both. Every later award for that night
    replaces the night's graded row and leaves this one alone (the deletes all
    carry `league != 'adjust'`), so a correction cannot be erased by re-grading.

    It lives on the sentinel evening_id=0 - the ledger is keyed one row per
    player per evening, so this is a single running correction total per player;
    the itemised history is in `audit`."""
    delta = int(delta)
    if not delta:
        raise ValueError("delta is 0 - nothing to correct")
    if not (reason or "").strip():
        raise ValueError("a correction needs a reason (it is what you show the player)")
    sid = season_id(conn)
    with conn:
        conn.execute("INSERT INTO award(evening_id,player_id,raw,bonus,multiplier,points,coins,"
                     "reason,applied_by,ts) VALUES(0,?,?,?,?,?,?,?,?,?)",
                     (player_id, float(delta), 0.0, 1.0, delta,
                      delta * conn.execute("SELECT coins_per_point FROM season WHERE id=?",
                                           (sid,)).fetchone()["coins_per_point"],
                      f"adjust:{reason}", actor_id, dbmod.now()))
        conn.execute("INSERT INTO ledger(player_id,evening_id,season_id,league,day,points) "
                     "VALUES(?,0,?,'adjust',?,?)", (player_id, sid, "adjust", delta))
        dbmod.audit(conn, "points.adjust", actor_id, player_id, None,
                    {"delta": delta, "reason": reason})
    return {"player_id": player_id, "delta": delta, "reason": reason}
