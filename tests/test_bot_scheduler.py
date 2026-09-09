"""Scheduler + restart-recovery tests. No gateway: channels are faked."""
import asyncio
import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
import discord
import db as D
import services as V
import ui

def _fresh(path):
    """Delete a DB AND its sidecar files.

    WAL/journal siblings survive deleting the main file and silently re-apply the
    OLD schema - which is how "the code is fine but the test fails" happens, both
    here and on any server where someone rm's hub.db but not hub.db-wal.
    """
    path = pathlib.Path(path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = pathlib.Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    return path


ok = 0
def check(name, cond, detail=""):
    global ok
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    ok += 1
    print(f"pass  {name}")

class Sent:
    _n = 0
    def __init__(self, embed=None, view=None, content=None):
        Sent._n += 1
        self.id = 9000 + Sent._n
        self.embed = embed
        self.view = view
        # content was dropped entirely, so any TEXT-only message (a staff warning,
        # an apology card) was invisible to every check in this suite
        self.content = content
    async def edit(self, **kw):
        if "embed" in kw: self.embed = kw["embed"]

class FakeChannel:
    def __init__(self, cid): self.id = cid; self.messages = {}
    async def send(self, content=None, embed=None, view=None):
        m = Sent(embed, view, content); self.messages[m.id] = m; return m
    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")
        return self.messages[mid]

class FakeUser:
    id = 4242
    def __init__(self): pass

# --- build a bot-shaped object without a token --------------------------- #
import main as M
DB = ROOT / ".pytest_tmp" / "sched.db"
DB.parent.mkdir(exist_ok=True)
for suffix in ("", "-wal", "-shm", "-journal"):     # a stale -wal resurrects old rows
    path = pathlib.Path(str(DB) + suffix)
    if path.exists():
        path.unlink()
conn = D.connect(DB)

bot = M.HubBot.__new__(M.HubBot)          # no gateway needed for tick()/post_evening()
bot.conn = conn
bot.hub_conn = conn
bot.hub_channel_id = 555
bot._hub_channels = {}
# no bot.user: tick()/post_evening() deliberately never touch the client user,
# which is part of why they are safe to call right after a restart.
def get_channel(cid, _b=bot):
    return _b._hub_channels.setdefault(cid, FakeChannel(cid))
bot.get_channel = get_channel
D.set_cfg(conn, "channel:l1", 101)
D.set_cfg(conn, "channel:l2", 102)
D.set_cfg(conn, "channel:l3", 103)
for c in (101, 102, 103):
    bot._hub_channels[c] = FakeChannel(c)

# --- calendar ------------------------------------------------------------- #
# v4 rotation: exactly ONE league on each weekday, all three on Sunday.
D.set_cfg(conn, "answer_seconds", 60)   # short window: this suite races the clock
res = V.create_season(conn, "Season 1", "2026-09-14", weeks=1)
check("weeks=1 makes 9 evenings (6 weekday nights + 3 on Sunday)",
      res["evenings"] == 9 and res["nights_per_league"] == {"l1": 3, "l2": 3, "l3": 3},
      str(res))
day, sun = "2026-09-14", "2026-09-20"          # Mon 14 Sep, Sun 20 Sep
l1 = conn.execute("SELECT * FROM evening WHERE day=? AND league='l1'", (day,)).fetchone()
for absent in ("l2", "l3"):
    check(f"{absent} has NO Monday evening under the rotation",
          conn.execute("SELECT id FROM evening WHERE day=? AND league=?",
                       (day, absent)).fetchone() is None)
for o, (prompt, opts, tier) in enumerate([
        ("Which is a support mech?", ["Orion", "Sasquatch", "Blacklight"], "easy"),
        ("Which is legendary?", ["Dualars", "Rat", "Vulcan"], "medium"),
        ("Hardest one?", ["Skyship", "Aurora", "Cold Store"], "hard")], start=1):
    V.add_question(conn, l1["id"], o, prompt, opts, tier)     # pre-authored, night not open yet
check("questions can be authored BEFORE the evening opens (pre-authoring is the workflow)",
      l1["status"] == "scheduled" and conn.execute(
          "SELECT COUNT(*) c FROM question WHERE evening_id=?", (l1["id"],)).fetchone()["c"] == 3)

# --- tick posts on time --------------------------------------------------- #
t0 = V.parse_iso(l1["opens_at"]) + dt.timedelta(seconds=1)
out = asyncio.run(bot.tick(now=t0))
check("tick opens the ONE league due on a Monday",
      out["opened"] == [l1["id"]], str(out))
chan = bot._hub_channels[101]
check("League 1 posts 3 question cards + 3 staff rows", len(chan.messages) == 6,
      str(len(chan.messages)))
card = [m for m in chan.messages.values() if isinstance(m.view, ui.AnswerView)]
staff_rows = [m for m in chan.messages.values() if isinstance(m.view, ui.QuestionGradeView)]
check("3 player cards carry an AnswerView, 3 staff rows a QuestionGradeView",
      len(card) == 3 and len(staff_rows) == 3, f"{len(card)} / {len(staff_rows)}")
check("A-D slots exist and the clear button sits on its own row",
      all(len(m.view.children) == 5 for m in card))
check("player cards never expose staff controls",
      all(not isinstance(m.view, ui.QuestionGradeView) for m in card))
check("option labels are the real answers",
      [m.view.children[0].label for m in card] == ["Orion", "Dualars", "Skyship"],
      str([m.view.children[0].label for m in card]))
check("unused option slot (E) is disabled on a 3-option question",
      card[0].view.children[3].disabled is True)
kids = staff_rows[0].view.children
verdicts = [c for c in kids if (c.label or "").startswith("✅")]
check("staff row shows exactly one VERDICT button per real option (3 here)",
      len(verdicts) == 3 and all(not c.disabled for c in verdicts),
      str([(c.label, c.disabled) for c in kids]))
check("the unused D slot is not merely absent but visibly dead",
      any((c.label or "") == "—" and c.disabled for c in kids), str([c.label for c in kids]))
check("the points control is on the staff row and keyed by id, not position",
      any("Points" in (c.label or "") for c in staff_rows[0].view.children)
      and (staff_rows[0].view.children[-1].custom_id or "").endswith(":none"))
check("question message ids are stored (so restart can still find them)",
      conn.execute("SELECT COUNT(*) c FROM question WHERE message_id IS NOT NULL"
                   " AND evening_id=?", (l1["id"],)).fetchone()["c"] == 3)
for other in (102, 103):
    check(f"nothing is posted to channel {other} on a Monday night",
          not bot._hub_channels[other].messages, str(bot._hub_channels[other].messages))

# --- the answer clock ----------------------------------------------------- #
qids = [r["id"] for r in conn.execute("SELECT id FROM question WHERE evening_id=? ORDER BY ordinal",
                                      (l1["id"],)).fetchall()]
check("submit accepted while open",
      V.submit_answer(conn, qids[0], 77, 1)["accepted"] is True)
t1 = V.parse_iso(l1["closes_at"]) + dt.timedelta(seconds=1)
out2 = asyncio.run(bot.tick(now=t1))
check("tick auto-locks the evening", l1["id"] in out2["locked"], str(out2))
check("status is locked without a human present",
      conn.execute("SELECT status FROM evening WHERE id=?", (l1["id"],)).fetchone()["status"]
      == "locked")
check("late answers are rejected after auto-lock",
      V.submit_answer(conn, qids[0], 78, 1)["accepted"] is False)
check("the player's answer survived the lock",
      conn.execute("SELECT COUNT(*) c FROM entry WHERE question_id=?", (qids[0],)).fetchone()["c"]
      == 1)
check("question cards were refreshed after lock, not duplicated", len(chan.messages) == 6)

# --- Sunday: three simultaneous posts, zero special treatment -------------- #
sun_l1 = conn.execute("SELECT * FROM evening WHERE day=? AND league='l1'", (sun,)).fetchone()
for lg in ("l1", "l2", "l3"):
    check(f"Sunday {lg} runs but is not a reward tier",
          conn.execute("SELECT difficulty, multiplier FROM evening WHERE day=? AND league=?",
                       (sun, lg)).fetchone()[0:2] == ("normal", 1.0))
D.set_cfg(conn, f"prompt:{sun}:l2", "Skyship 11, enemy Orion/Citadel/Surge - your plan?")
D.set_cfg(conn, f"prompt:{sun}:l3", "Which hangar upgrade pays for itself first, and when?")
V.add_question(conn, sun_l1["id"], 1, "Sunday freebie?", ["Yes", "No"], "easy")
for c in (101, 102, 103):
    bot._hub_channels[c] = FakeChannel(c)
t2 = V.parse_iso(sun_l1["opens_at"]) + dt.timedelta(seconds=1)
out3 = asyncio.run(bot.tick(now=t2))
sun_ids = sorted(r["id"] for r in conn.execute(
    "SELECT id FROM evening WHERE day=?", (sun,)))
check("all three Sunday nights are opened", set(sun_ids) <= set(out3["opened"]), str(out3))
# Tick opens EVERYTHING due, so the weekday nights never run in this fixture open
# here too. That is the correct behaviour - a night past its opens_at is rescued,
# not silently skipped - and it is worth pinning down explicitly.
check("a tick that finds several overdue nights opens them all, in day order",
      out3["opened"] == sorted(out3["opened"]) and set(sun_ids) <= set(out3["opened"])
      and all(conn.execute("SELECT status FROM evening WHERE id=?", (eid,)).fetchone()["status"]
              in ("open", "locked", "graded") for eid in out3["opened"]), str(out3))
# Channel 102 also received the overdue Monday/Tuesday l2 nights, so count per
# evening rather than per channel: the thing that matters is that every league
# that was due got exactly one card, and that card is a submission prompt.
def last_card(chan):
    ids = sorted(chan.messages)
    return chan.messages[ids[-1]] if ids else None
for lg, cid in (("l2", 102), ("l3", 103)):
    ev = conn.execute("SELECT * FROM evening WHERE day=? AND league=?", (sun, lg)).fetchone()
    check(f"Sunday {lg} posted one card and stored its message id",
          ev["message_id"] is not None
          and ev["message_id"] in bot._hub_channels[cid].messages, str(ev["message_id"]))
    check(f"Sunday {lg}'s card is a SubmitView (players answer with a modal, not a button)",
          isinstance(last_card(bot._hub_channels[cid]).view, ui.SubmitView))
l2 = conn.execute("SELECT * FROM evening WHERE day=? AND league='l2'", (sun,)).fetchone()
check("that card carries a Submit button",
      isinstance(list(bot._hub_channels[102].messages.values())[0].view, ui.SubmitView))
check("the card stores its message id for in-place refresh",
      conn.execute("SELECT message_id FROM evening WHERE id=?",
                   (l2["id"],)).fetchone()["message_id"] is not None)
check("a Sunday L2 submission is accepted",
      V.create_submission(conn, l2["id"], 77, "Hold high, trade first, rotate east at 40 seconds"
                          " with the support mech")["accepted"] is True)
t3 = V.parse_iso(l2["closes_at"]) + dt.timedelta(minutes=2)
asyncio.run(bot.tick(now=t3))
check("the Sunday L2 night locks at its own deadline, not the 60s quiz timer",
      conn.execute("SELECT status FROM evening WHERE id=?",
                   (l2["id"],)).fetchone()["status"] == "locked")


# --- downtime recovery: an evening missed by an outage is still rescuable --- #
# Isolated DB: on the shared one, earlier ticks had already opened AND locked
# this evening, so manually resetting its status was fighting the real flow.
DB3 = _fresh(ROOT / ".pytest_tmp" / "sched3.db")
c3 = D.connect(DB3)
# Dates sit on TODAY because submit_answer and the auto-lock read the real wall
# clock; a simulated 2026-09-15 would be "the future" to them. (Follow-up: inject
# a clock into services so the whole file can run on a frozen time.)
today = dt.date.today().isoformat()
D.set_cfg(c3, "answer_seconds", 60)   # short window: this suite races the clock
V.create_season(c3, "S3", today, weeks=1)
# Whichever league owns today's night (Mon/Thu/Sun = l1 under the v4 rotation),
# so this does not silently rot into a different league every weekday.
ghost = c3.execute("SELECT * FROM evening WHERE day=? ORDER BY league LIMIT 1",
                   (today,)).fetchone()
ghost_league = ghost["league"]
if ghost_league == "l1":
    for o, (pr, op, ti) in enumerate([("Q1?", ["a", "b"], "easy"), ("Q2?", ["a", "b"], "medium"),
                                      ("Q3?", ["a", "b"], "medium")], start=1):
        V.add_question(c3, ghost["id"], o, pr, op, ti)
else:
    D.set_cfg(c3, f"prompt:{today}:{ghost_league}", "Recover this prompt after the outage")
D.set_cfg(c3, f"channel:{ghost_league}", 101)
bot.conn = c3
bot.hub_conn = c3
bot._hub_channels[101] = FakeChannel(101)

# the bot was down at 16:00 and comes back 5 hours later
now_late = V.parse_iso(ghost["opens_at"]) + dt.timedelta(hours=5)
if now_late < dt.datetime.now(V.IST):        # evening already truly due -> backfill sees it
    now_late = dt.datetime.now(V.IST) + dt.timedelta(minutes=2)
check("the evening was never posted during the outage",
      c3.execute("SELECT status, message_id FROM evening WHERE id=?",
                 (ghost["id"],)).fetchone()["status"] == "scheduled")
asyncio.run(bot.backfill(now=now_late))
newchan = bot._hub_channels[101]
expect = 7 if ghost_league == "l1" else 2      # 3 cards + 3 staff rows + warning, or 1 + warning
check("recovery posts the missed evening instead of skipping it",
      len(newchan.messages) == expect, f"{len(newchan.messages)} != {expect} for {ghost_league}")
warn = [m for m in newchan.messages.values()
        if m.embed and "Late start" in (m.embed.description or "")]
check("recovery posts its own warning, not a buried footnote", len(warn) == 1)
check("the warning promises a fresh full window",
      warn and "fresh full window" in warn[0].embed.description)
check("the warning is the FIRST thing posted",
      bool(warn) and list(newchan.messages).index(warn[0].id) == 0)
gstatus = c3.execute("SELECT status FROM evening WHERE id=?", (ghost["id"],)).fetchone()["status"]
check("recovered evening is OPEN, not dead-on-arrival", gstatus == "open", gstatus)
row = c3.execute("SELECT opens_at, closes_at FROM evening WHERE id=?", (ghost["id"],)).fetchone()
window = (V.parse_iso(row["closes_at"]) - V.parse_iso(row["opens_at"])).total_seconds()
orig = (V.parse_iso(ghost["closes_at"]) - V.parse_iso(ghost["opens_at"])).total_seconds()
check("the recovered night keeps its ORIGINAL window length - 60s for a quiz, "
      "hours for a submission league", window == orig, f"{window}s vs {orig}s")
check("and that window still ends in the future, so people can actually take part",
      V.parse_iso(row["closes_at"]) > now_late, f"{row['closes_at']} vs {now_late}")
if ghost_league == "l1":
    gq = c3.execute("SELECT id, answer_deadline FROM question WHERE evening_id=?"
                    " ORDER BY ordinal", (ghost["id"],)).fetchall()[0]
    check("question deadlines moved with it (bonus measured from card time)",
          V.parse_iso(gq["answer_deadline"]) > now_late)
    check("players can actually answer a recovered question",
          V.submit_answer(c3, gq["id"], 79, 0)["accepted"] is True)
    graded = V.grade_question(c3, gq["id"], 0, actor_id=1)
    check("grading a recovered question pays out", graded["players_scored"] == 1, str(graded))
else:
    rec = c3.execute("SELECT message_id FROM evening WHERE id=?", (ghost["id"],)).fetchone()
    check("the recovered prompt card is a real, editable message",
          rec["message_id"] is not None)
    sub = V.create_submission(c3, ghost["id"], 79,
                             "Hold the high ground, trade the first exchange, rotate east at 40s")
    check("players can actually submit to a recovered night", sub["accepted"] is True, str(sub))
    check("and the window that just opened is still usable (not expired on arrival)",
          V.parse_iso(c3.execute("SELECT closes_at FROM evening WHERE id=?",
                                 (ghost["id"],)).fetchone()["closes_at"]) > now_late)

# a second boot must not re-post the same evening twice
msgs_before = len(newchan.messages)
asyncio.run(bot.backfill(now=now_late + dt.timedelta(minutes=2)))
check("an already-graded/re-opened evening is not double-posted",
      len(newchan.messages) == msgs_before, f"{msgs_before} -> {len(newchan.messages)}")

# --- restart: reopen the DB, nothing in memory is required ---------------- #
c3.close()
c3 = D.connect(DB3)
check("after a hard restart the recovered evening is still open",
      c3.execute("SELECT status FROM evening WHERE id=?", (ghost["id"],)).fetchone()["status"]
      == "open")
if ghost_league == "l1":
    check("and answers recorded before the restart are intact",
          c3.execute("SELECT COUNT(*) c FROM entry WHERE question_id=?",
                     (gq["id"],)).fetchone()["c"] == 1)
else:
    check("and the submission recorded before the restart is intact",
          c3.execute("SELECT text FROM submission WHERE evening_id=?",
                     (ghost["id"],)).fetchone()["text"].startswith("Hold the high ground"))

check("views are re-registrable from the class list alone",
      all(hasattr(c, "timeout") for c in ui.PERSISTENT_VIEWS) and len(ui.PERSISTENT_VIEWS) >= 6)

# --- the midnight restart case (date arithmetic used to skip yesterday) -----
DB2 = ROOT / ".pytest_tmp" / "sched2.db"
DB2 = _fresh(ROOT / ".pytest_tmp" / "sched2.db")
c2 = D.connect(DB2)
D.set_cfg(c2, "answer_seconds", 60)   # short window: this suite races the clock
V.create_season(c2, "S2", "2026-09-14", weeks=1)
g2ev = c2.execute("SELECT * FROM evening WHERE day='2026-09-14' AND league='l1'").fetchone()
V.add_question(c2, g2ev["id"], 1, "Late night?", ["yes", "no"], "easy")
bot2 = M.HubBot.__new__(M.HubBot)
bot2.conn = c2
bot2.hub_conn = c2
bot2.hub_channel_id = 700
bot2._hub_channels = {700: FakeChannel(700)}
bot2.get_channel = lambda cid, _b=bot2: _b._hub_channels.setdefault(cid, FakeChannel(cid))
D.set_cfg(c2, "channel:l1", 700)
# restart at 00:30 the NEXT day - 8.5h after the evening opened
midnight = V.parse_iso(g2ev["opens_at"]) + dt.timedelta(minutes=1)
n = asyncio.run(bot2.backfill(now=midnight))
check("a 00:30-style restart still finds YESTERDAY's evening (no date-boundary skip)",
      n >= 1, str(n))
check("and the evening is open, not lost",
      c2.execute("SELECT status FROM evening WHERE id=?", (g2ev["id"],)).fetchone()["status"]
      == "open")
# a second boot while it is open and ungraded must re-window it, not re-post it
msgs_before = len(bot2._hub_channels[700].messages)
# Mon 14 Sep is a League 1 night under the v4 rotation, so exactly ONE league
# missed this outage - and a SECOND boot must add nothing further.
warn_before = sum(1 for m in bot2._hub_channels[700].messages.values()
                  if m.embed and "Late start" in (m.embed.description or ""))
check("every league that missed the outage is announced once (1 on a weekday night)",
      warn_before == 1, str(warn_before))
asyncio.run(bot2.backfill(now=midnight + dt.timedelta(minutes=30)))
after = bot2._hub_channels[700].messages
check("an already-posted open evening is NOT double-posted",
      len(after) == msgs_before, f"{msgs_before} -> {len(after)}")
check("nor is the outage warning repeated on a second boot",
      sum(1 for m in after.values() if m.embed and "Late start" in (m.embed.description or ""))
      == warn_before)
check("it stays open with a usable window",
      c2.execute("SELECT status FROM evening WHERE id=?", (g2ev["id"],)).fetchone()["status"]
      == "open"
      and c2.execute("SELECT closes_at FROM evening WHERE id=?",
                    (g2ev["id"],)).fetchone()["closes_at"]
      > (midnight + dt.timedelta(minutes=30)).isoformat(timespec="seconds"))
# an evening older than 24h is deliberately NOT resurrected
far = dt.datetime(2026, 9, 20, 17, 0, tzinfo=V.IST)
cutoff = (far - dt.timedelta(hours=24)).isoformat(timespec="seconds")
before = c2.execute("SELECT COUNT(*) c FROM evening WHERE status='scheduled' "
                    "AND opens_at < ?", (cutoff,)).fetchone()["c"]
# 9 nights in a 1-week season, 1 already recovered above; v4 leaves 5 of the rest
# older than 24h (the Sunday grands are inside the window at this clock).
check("there ARE ancient unposted evenings for this test to be meaningful", before >= 1,
      str(before))
asyncio.run(bot2.backfill(now=far))
still = c2.execute("SELECT COUNT(*) c FROM evening WHERE status='scheduled' "
                   "AND opens_at < ?", (cutoff,)).fetchone()["c"]
check("evenings older than 24h are never resurrected (no 5-day-old cards appearing)",
      still == before, f"{before} -> {still}")

# --- Sunday: the one night where a missed outage means THREE late evenings ----- #
DB4 = _fresh(ROOT / ".pytest_tmp" / "sched4.db")
c4 = D.connect(DB4)
D.set_cfg(c4, "answer_seconds", 60)   # short window: this suite races the clock
V.create_season(c4, "S4", "2026-09-14", weeks=1)
# set_cfg, not a raw INSERT: config values are JSON-encoded and cfg() is the only
# correct reader. A hand-written "702" makes int() blow up and the league silently
# falls back to the hub channel - which is precisely the bug this check exists to catch.
for cid, lg in ((701, "l1"), (702, "l2"), (703, "l3")):
    D.set_cfg(c4, f"channel:{lg}", cid)
sun4 = c4.execute("SELECT * FROM evening WHERE day='2026-09-20' ORDER BY league").fetchall()
check("the season's last night has all three leagues to recover",
      len(sun4) == 3, str([r["league"] for r in sun4]))
bot4 = M.HubBot.__new__(M.HubBot)
bot4.conn = c4
bot4.hub_conn = c4
bot4.hub_channel_id = 701
bot4._hub_channels = {c: FakeChannel(c) for c in (701, 702, 703)}
bot4.get_channel = lambda cid, _b=bot4: _b._hub_channels.setdefault(cid, FakeChannel(cid))
D.set_cfg(c4, "prompt:2026-09-20:l2", "Recover this plan prompt")
D.set_cfg(c4, "prompt:2026-09-20:l3", "Recover this hangar prompt")
V.add_question(c4, sun4[0]["id"], 1, "Sunday card?", ["yes", "no"], "easy")
late4 = V.parse_iso(sun4[0]["opens_at"]) + dt.timedelta(hours=3)
asyncio.run(bot4.backfill(now=late4))
warns = 0
for cid, ev in zip((701, 702, 703), sun4):
    chan = bot4._hub_channels[cid]
    got = [m for m in chan.messages.values()
           if m.embed and "Late start" in (m.embed.description or "")]
    warns += len(got)
    fresh = c4.execute("SELECT status, closes_at FROM evening WHERE id=?",
                       (ev["id"],)).fetchone()
    check(f"Sunday {ev['league']} was recovered, opened and re-windowed",
          fresh["status"] == "open" and fresh["closes_at"] > late4.isoformat(timespec="seconds"),
          str(dict(fresh)))
check("a missed Grand Day warns every league once, not once in total", warns == 3, str(warns))
# L1 = warning + question card + staff grading row (3); L2/L3 = warning + prompt
# card (2). The point of this check is the ROUTING, not the count: a league whose
# config was missed silently posts to the hub channel instead, which would show up
# as two cards on 701 and none on 702.
chan_msgs = {c: list(bot4._hub_channels[c].messages.values()) for c in (701, 702, 703)}
check("each league's cards went to its own channel, nothing to the hub fallback",
      len(chan_msgs[702]) == 2 and len(chan_msgs[703]) == 2 and len(chan_msgs[701]) == 3,
      str({c: len(v) for c, v in chan_msgs.items()}))
check("the two submission nights got a SubmitView, the quiz night did not",
      all(isinstance(m.view, ui.SubmitView) for m in chan_msgs[702] + chan_msgs[703]
          if m.view is not None)
      and not any(isinstance(m.view, ui.SubmitView) for m in chan_msgs[701] if m.view))

# --- an unposted night must stay resumable (regression: open_evening ran BEFORE
#     the content check, so an empty L1 night became 'open', accepted submissions
#     against nothing, and the scheduler never retried it - its query only looks at
#     'scheduled' rows) -------------------------------------------------------- #
# Thu 17 Sep is an ordinary weekday L1 night in this fixture. Drive the SAME night
# both ways rather than borrowing one an earlier check already consumed.
# bot.conn was pointed at bot4's DB by the recovery checks above; rebind to THIS
# fixture's connection or every call below operates on a closed database.
bot.conn, bot.hub_conn = conn, conn
night = conn.execute("SELECT * FROM evening WHERE day='2026-09-17' AND league='l1'").fetchone()
assert night is not None, "fixture moved: no Thursday L1 night"
nid = night["id"]
conn.execute("DELETE FROM question WHERE evening_id=?", (nid,))
conn.execute("UPDATE evening SET status='scheduled', channel_id=NULL WHERE id=?", (nid,))
ok_empty = asyncio.run(bot.post_evening(dict(conn.execute(
    "SELECT * FROM evening WHERE id=?", (nid,)).fetchone())))
st_empty = conn.execute("SELECT status FROM evening WHERE id=?", (nid,)).fetchone()["status"]
check("an L1 night with no questions does NOT open",
      ok_empty is False and st_empty == "scheduled", f"{ok_empty}/{st_empty!r}")
check("...and it is not recorded as posted, so no channel/message ids were burned",
      conn.execute("SELECT message_id, channel_id FROM evening WHERE id=?",
                   (nid,)).fetchone()[:] == (None, None), "still untouched")
try:
    V.submit_answer(conn, -1, 4242, 0)
    check("a submission against a night with no question raises, never accepts", False)
except ValueError as e:
    check("a submission against a night with no question raises, never accepts",
          "not found" in str(e), str(e))
q_fix = V.add_question(conn, nid, None, "Rescued?", ["Yes", "No"], "easy")
ok_fix = asyncio.run(bot.post_evening(dict(conn.execute(
    "SELECT * FROM evening WHERE id=?", (nid,)).fetchone())))
st_fix = conn.execute("SELECT status FROM evening WHERE id=?", (nid,)).fetchone()["status"]
check("once staff author it, the SAME night opens on the next attempt",
      ok_fix is True and st_fix == "open", f"{ok_fix}/{st_fix}")
check("nothing was scored while it sat unposted",
      conn.execute("SELECT COUNT(*) c FROM ledger WHERE evening_id=?",
                   (nid,)).fetchone()["c"] == 0, "clean slate")
# An unwired night: no per-league config, no hub fallback, and no previous
# channel_id left over from an earlier post (that fallback is deliberate - it is how
# a night re-posts itself after the bot is moved between servers). There IS a
# staff channel, because that is what the warning is for.
fri = conn.execute("SELECT * FROM evening WHERE day='2026-09-18' AND league='l2'").fetchone()
assert fri is not None, "fixture moved: no Friday L2 night"
fid = fri["id"]
STAFF_CH = 777
bot._hub_channels[STAFF_CH] = FakeChannel(STAFF_CH)
D.set_cfg(conn, V._hubkey("channel", "staff"), STAFF_CH)
_saved = {lg: D.cfg(conn, f"channel:{lg}") for lg in ("l1", "l2", "l3")}
_hub = bot.hub_channel_id
for lg in ("l1", "l2", "l3"):
    conn.execute("DELETE FROM config WHERE key=?", (f"channel:{lg}",))
    conn.execute("DELETE FROM config WHERE key=?", (f"hub:chan_league_{lg}",))
bot.hub_channel_id = None
conn.execute("UPDATE evening SET status='scheduled', channel_id=NULL, message_id=NULL "
             "WHERE id=?", (fid,))

warns = lambda: len(bot._hub_channels[STAFF_CH].messages)
_before = warns()
_probe = dict(conn.execute("SELECT * FROM evening WHERE id=?", (fid,)).fetchone())
_resolved = asyncio.run(bot._resolve_channel(_probe))
r1 = asyncio.run(bot.post_evening(_probe))
r2 = asyncio.run(bot.post_evening(dict(conn.execute(
    "SELECT * FROM evening WHERE id=?", (fid,)).fetchone())))
check("an unwired night refuses to open rather than posting into the void",
      r1 is False and r2 is False and conn.execute(
          "SELECT status FROM evening WHERE id=?", (fid,)).fetchone()["status"]
      == "scheduled", f"{r1}/{r2}")
check("a night that cannot post WARNS staff exactly once",
      warns() == _before + 1, f"{_before} -> {warns()} (2 failed posts)")
check("and the warning names the fix, not just the failure",
      any("/setup mode:STATUS" in (m.embed.description if m.embed else "")
          or "/setup mode:STATUS" in (m.content or "")
          for m in bot._hub_channels[STAFF_CH].messages.values()),
      str([m.content for m in bot._hub_channels[STAFF_CH].messages.values()]))
# a third attempt inside the hour is silent, because tick runs every 20 seconds
r3 = asyncio.run(bot.post_evening(dict(conn.execute(
    "SELECT * FROM evening WHERE id=?", (fid,)).fetchone())))
check("retries inside the hour stay quiet (180 pings an hour is not a warning)",
      r3 is False and warns() == _before + 1, f"{_before} -> {warns()}")
for lg, v in _saved.items():
    if v is not None:
        D.set_cfg(conn, f"channel:{lg}", v)
bot.hub_channel_id = _hub
D.set_cfg(conn, "channel:l2", 102)


print(f"\n{ok} scheduler checks passed.")
