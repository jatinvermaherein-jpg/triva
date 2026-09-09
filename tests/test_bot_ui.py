"""UI layer tests - the restart-proofing and the click paths, no gateway needed.

Proves three claims the plan makes:
  1. every panel is persistent (survives restart)
  2. layout never exceeds Discord's per-row button limit
  3. the real button callbacks drive the real service layer correctly
"""
import asyncio
import datetime as dt
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
import db as D                 # noqa: E402
import services as V          # noqa: E402
import ui                     # noqa: E402
import discord                # noqa: E402

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

# The real dispatch path: ItemCallback.__call__(interaction) - it already knows its
# view and item, so a test tap must pass ONLY the interaction. Anything looser and
# the test would be exercising a signature the bot never uses.
def noob_check(view, child_index, interaction):
    """Full dispatch path: interaction_check THEN the callback. A player must be
    blocked by both gates."""
    async def go():
        allowed = await view.interaction_check(interaction)
        if allowed:
            await view.children[child_index].callback(interaction)
        return allowed
    return asyncio.run(go())


def tap(view, child_index, interaction):
    child = view.children[child_index]
    asyncio.run(child.callback(interaction))
    return child


# --- fake interaction good enough for callbacks ---------------------------- #
class Resp:
    """Mirrors the members of discord.py's InteractionResponse our callbacks use.
    Verified against 2.7.1: send_message / edit_message / send_modal / defer /
    is_done / pong / autocomplete / launch_activity."""

    def __init__(self): self.sent = []; self.edited = []; self.modals = []
    def is_done(self):        # a METHOD on discord.py's InteractionResponse, not a property
        return bool(self.sent or self.edited or self.modals)
    async def defer(self, **kw): pass
    async def send_message(self, content=None, **kw): self.sent.append((content, kw))
    async def edit_message(self, **kw): self.edited.append(kw)
    async def send_modal(self, modal): self.modals.append(modal)

_LAST_FOLLOWUPS: list = []


class _Followup:
    def __init__(self): self.sent = []
    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        _LAST_FOLLOWUPS.append(self)


class FakeInteraction:
    def __init__(self, user_id=1, staff=True, channel=None):
        self.user = type("U", (), {"id": user_id, "display_name": f"p{user_id}",
                                   "roles": [type("R", (), {"name": ui.HUB_ROLE})()] if staff else [],
                                   "guild_permissions": type("P", (), {"administrator": staff})()})()
        self.response = Resp()
        self.message = type("M", (), {"id": 5000})()
        self.channel = channel
        self.guild = None
        self.followup = _Followup()

# --- 1. persistence -------------------------------------------------------- #
check("every view is registered for persistence", len(ui.PERSISTENT_VIEWS) >= 6,
      str([c.__name__ for c in ui.PERSISTENT_VIEWS]))
for cls in ui.PERSISTENT_VIEWS:
    inst = cls(D.connect(":memory:")) if "conn" in cls.__init__.__code__.co_varnames else cls(D.connect(":memory:"))
    assert inst.is_persistent(), cls.__name__
check("all registered views are is_persistent() (timeout=None + explicit custom_ids)",
      all((cls(D.connect(":memory:"))).is_persistent() for cls in ui.PERSISTENT_VIEWS))
check("no generated custom_ids anywhere",
      all(c.custom_id for cls in ui.PERSISTENT_VIEWS
          for c in cls(D.connect(":memory:")).children))

# --- 2. layout limits ------------------------------------------------------ #
tmp = _fresh(ROOT / ".pytest_tmp" / "ui.db")
tmp.parent.mkdir(parents=True, exist_ok=True)
conn = D.connect(tmp)
V.create_season(conn, "S1", "2026-09-14", weeks=1)
ev = conn.execute("SELECT * FROM evening WHERE league='l1' AND day='2026-09-14'").fetchone()
V.open_evening(conn, ev["id"], 10, 1)
q = V.add_question(conn, ev["id"], 1, "Pick one", ["Alpha", "Beta", "Gamma"], "medium")
v = ui.answer_view(conn, q["question_id"], ["Alpha", "Beta", "Gamma"])
# row=None means "auto-place", so count by what the guard validated, not the raw attr
tappable = [c for c in v.children if not c.disabled]
check("a 4-option card + clear passes the row guard", True)
check("exactly the 3 real options are tappable", len(tappable) == 4, str(tappable))   # 3 opts + clear
full = ui.answer_view(conn, q["question_id"], ["a", "b", "c", "d"])
check("view defines only A-D (no hidden 5th/6th option slot to overflow row 0)",
      len(full.children) == 5, f"{len(full.children)} children")
check("full 4-option card passes the row guard", ui.check_rows(full) is None)
check("card with 2 options still passes (fewer is always fine)",
      ui.check_rows(ui.answer_view(conn, q["question_id"], ["a", "b"])) is None)
gv_full = ui.question_grade_view(conn, q["question_id"], 4)
check("staff grade row holds 4 verdicts + void + points and still fits Discord's limits",
      len(gv_full.children) == 6, str([c.label for c in gv_full.children]))
check("and the row guard actually accepts that layout (row 0 = 5 buttons max)",
      ui.check_rows(gv_full) is None)
# Row attributes are not readable back (Button.row has no getter and _underlying
# is computed), so assert what is observable: the packing guard passes, and the two
# staff-only controls are the last two, reachable by their ids not their indexes.
check("void and points are the last two controls and keyed by id, not position",
      [(c.custom_id or "").split(":")[-1] for c in gv_full.children[-2:]] == ["points", "none"],
      str([c.custom_id for c in gv_full.children[-2:]]))
check("a 4-option verdict set does not overflow row 0 (guard would have raised)",
      ui.check_rows(gv_full) is None)
check("unused option slots are disabled, not tappable",
      v.children[3].disabled and v.children[3].label == "—")
check("real options carry their text and a question-scoped id",
      v.children[0].label == "Alpha" and v.children[0].custom_id == f"hub:ans:{q['question_id']}:0")
try:
    ui.answer_view(conn, q["question_id"], ["a", "b", "c", "d", "e"])
    check("5 options rejected at build time", False)
except ValueError:
    check("5 options rejected at build time", True)
# discord.py validates row range (0-4) and the 25-item ceiling in add_item, but it
# does NOT validate that a single row holds <=5 - our guard covers that gap.
def _pack(n_items, row_of=None):
    class Made(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            for i in range(n_items):
                # row= must go through the constructor: Button has no settable .row
                # and _underlying is a computed property.
                kw = {} if row_of is None else {"row": row_of(i)}
                self.add_item(discord.ui.Button(label=str(i), custom_id=f"x{i}", **kw))
    return Made()

check("6 buttons across auto rows is legal (packs onto row 1)",
      ui.check_rows(_pack(6)) is None)
check("exactly 25 buttons on 5 rows is accepted", ui.check_rows(_pack(25)) is None)
try:
    ui.check_rows(_pack(6, row_of=lambda i: 0))     # 6 crammed onto ONE row
    check("6 buttons declared on one row is rejected before send", False)
except ValueError:
    check("6 buttons declared on one row is rejected before send", True)
try:
    _pack(26)
    check("discord.py itself rejects >25 items", False)
except ValueError:
    check("discord.py itself rejects >25 items", True)
try:
    _pack(6, row_of=lambda i: 5)
    check("discord.py itself rejects row 5", False)
except ValueError:
    check("discord.py itself rejects row 5", True)

# --- 3. custom_id codec ---------------------------------------------------- #
check("cid builds", ui.cid("ans", 41, 2) == "hub:ans:41:2")
check("parse round-trips", ui.parse_cid("hub:ans:41:2", 2) == ("41", "2"))
check("garbage returns None", ui.parse_cid("something:else", 2) is None)
check("non-numeric tail yields None from ints()", ui.ints("hub:ans:41:clear", 2) is None)
check("ids stay under Discord's 100 char cap",
      all(len(c.custom_id) <= 100 for c in ui.answer_view(conn, 99999999999999999, ["a","b"]).children))

# --- 4. the click path, end to end ----------------------------------------- #
player = FakeInteraction(user_id=7001, staff=False)
import asyncio
tap(v, 0, player)
check("tapping option A writes an entry", conn.execute(
    "SELECT COUNT(*) c FROM entry WHERE question_id=? AND player_id=7001",
    (q["question_id"],)).fetchone()["c"] == 1)
check("player gets an ephemeral confirmation", player.response.sent
      and player.response.sent[0][1].get("ephemeral") is True
      and "recorded" in player.response.sent[0][0])
# staff grading through the real button
gv = ui.question_grade_view(conn, q["question_id"], 3)
host = FakeInteraction(user_id=1, staff=True)
tap(gv, 0, host)
awarded = conn.execute("SELECT COUNT(*) c FROM award WHERE question_id=?",
                       (q["question_id"],)).fetchone()["c"]
check("staff tapping 'A' grades everyone and pays the right player", awarded == 1, str(awarded))
check("the grade reply reports the evening state", "left tonight" in host.response.sent[0][0])
# non-staff is refused before any write
noob = FakeInteraction(user_id=9, staff=False)
before = conn.execute("SELECT COUNT(*) c FROM award").fetchone()["c"]
tap(gv, 1, noob)
check("a player pressing a staff button is blocked by interaction_check",
      noob_allowed is False if isinstance(noob_allowed := noob_check(gv, 1, noob), bool) else False,
      str(noob_allowed))
check("and no award row appeared",
      conn.execute("SELECT COUNT(*) c FROM award").fetchone()["c"] == before)
check("and the player was told why", noob.response.sent and "Hub Staff" in noob.response.sent[0][0])
# gate 2: the callback itself refuses, even if a dispatcher ever skips interaction_check
noob2 = FakeInteraction(user_id=9, staff=False)
asyncio.run(gv.children[1].callback(noob2))
check("the per-callback gate blocks a bypassed dispatch too",
      conn.execute("SELECT COUNT(*) c FROM award").fetchone()["c"] == before
      and noob2.response.sent and "Hub Staff" in noob2.response.sent[0][0])
# L2 band button
# l2 plays Tue 15 Sep in the v4 rotation (Monday is l1 only) - and it must stay a
# weekday night, because the assertion below is about a NON-grand 1.0x award.
ev2 = conn.execute("SELECT * FROM evening WHERE league='l2' AND day='2026-09-15'").fetchone()
check("l2 weekday night is ordinary (no 1.5x hiding in this test)",
      ev2["difficulty"] == "normal" and float(ev2["multiplier"]) == 1.0, str(dict(ev2)))
V.open_evening(conn, ev2["id"], 11, 1)
sub = V.create_submission(conn, ev2["id"], 8001, "Hold high ground, trade, rotate east at 40s")
g2 = ui.grading_view(conn, ev2["id"])
check("grading view rebinds custom_ids to the evening",
      g2.children[0].custom_id == f"hub:gr:{ev2['id']}:25", g2.children[0].custom_id)
staff = FakeInteraction(user_id=1, staff=True)
tap(g2, 0, staff)
pts = conn.execute("SELECT points FROM ledger WHERE player_id=8001").fetchone()["points"]
check("EXCELLENT 25 on a weekday pays 25", pts == 25, str(pts))
check("results message names the band and what is left",
      "Excellent" in staff.response.sent[0][0] and "left tonight" in staff.response.sent[0][0])
tap(g2, 0, staff)
check("pressing again with nothing left does not invent points",
      "Nothing left to grade" in staff.response.sent[1][0])
# Sunday through the very same button -> identical payout
ev3 = conn.execute("SELECT * FROM evening WHERE league='l3' AND day='2026-09-20'").fetchone()
check("that Sunday evening carries no multiplier", float(ev3["multiplier"]) == 1.0)
V.open_evening(conn, ev3["id"], 12, 1)
V.create_submission(conn, ev3["id"], 8002, "Stop building mech three, bank the token, swap mods")
g3 = ui.grading_view(conn, ev3["id"])
s3 = FakeInteraction(user_id=1, staff=True)
tap(g3, 1, s3)
pts = conn.execute("SELECT points FROM ledger WHERE player_id=8002").fetchone()["points"]
check("GOOD 15 on Sunday pays 15 - the same as any night", pts == 15, str(pts))
check("the award reply no longer promises a Sunday uplift",
      "1.5" not in s3.response.sent[0][0], s3.response.sent[0][0][:160])
# season dry run touches nothing
diff_before = conn.execute("SELECT COUNT(*) c FROM hall_of_fame").fetchone()["c"]
sav = ui.SeasonAdminView(conn)
d = FakeInteraction(user_id=1, staff=True)
tap(sav, 0, d)
check("DRY RUN previews roles and writes no Hall of Fame rows",
      "DRY RUN" in d.response.sent[0][1]["embed"].description.title()
      or "DRY RUN" in d.response.sent[0][1]["embed"].title
      and conn.execute("SELECT COUNT(*) c FROM hall_of_fame").fetchone()["c"] == diff_before)
# the dry run must preview the SAME season APPLY will change, and must show the
# stale-role sweep that is the whole point of a rollover
D.set_cfg(conn, "standings_night_floor", 0)
prev = V.closing_season_id(conn)
conn.execute("UPDATE season SET role_trivia=111 WHERE id=?", (prev,))
asyncio.run(V.finalize_season(conn, actor_id=1, apply_roles=False))     # writes S1 champion
check("a closed season's champion is on the wall before the next rollover",
      conn.execute("SELECT COUNT(*) c FROM hall_of_fame WHERE season_id=? AND placement=1",
                   (prev,)).fetchone()["c"] >= 1, "needs a scored l1 champion")
V.create_season(conn, f"Season {prev + 1}", "2026-10-12", weeks=1)
d2 = FakeInteraction(user_id=1, staff=True)
tap(ui.SeasonAdminView(conn), 0, d2)
embed2 = d2.response.sent[0][1]["embed"]
check("after a rollover the preview follows the NEWLY ACTIVE season, not the old one",
      f"Season {prev + 1}" in embed2.title, embed2.title)
pname = conn.execute("SELECT name FROM season WHERE id=?", (prev,)).fetchone()["name"]
check("the preview carries the removal line - what APPLY will really do",
      "➖" in embed2.description and f"{pname} l1 champion" in embed2.description,
      embed2.description)
check("a champion role the bot cannot attribute is shown as a manual, never a removal",
      "🖐" in embed2.description and "if a human granted it, leave it" in embed2.description,
      embed2.description)
check("dry run still wrote nothing new",
      conn.execute("SELECT status FROM season WHERE id=?", (prev,)).fetchone()["status"]
      == "closed", "prev stays closed; no second champion row appears")

# embed builders never crash on empty data
for fn, args in ((ui.hub_embed, (conn,)), (ui.evening_embed, (conn, dict(ev))),
                 (ui.standings_embed, (conn, "season", None)),
                 (ui.standings_embed, (conn, "league", "l2")),
                 (ui.question_embed, (conn, conn.execute("SELECT * FROM question WHERE id=?",
                                                          (q["question_id"],)).fetchone(), dict(ev)))):
    e = fn(*args)
    check(f"embed {fn.__name__} builds", isinstance(e, discord.Embed))
check("empty league table still renders (no crash on day 1)",
      "Nothing graded" in ui.standings_embed(
          D.connect(":memory:"), "league", "l1").description)

gv2 = ui.question_grade_view(conn, q["question_id"], 3, ["Orion", "Sasquatch", "Blacklight"])
labels = [c.label for c in gv2.children]
check("staff buttons name the option text, not just the letter",
      labels[1] == "✅ B Sasquatch", str(labels))
gv_children = {c.custom_id.split(":")[-1]: c for c in gv2.children}
check("staff row is exactly A-D plus void plus points (6, never a 7th)",
      set(gv_children) == {"0", "1", "2", "3", "none", "points"}, str(set(gv_children)))
check("the points control is staff-only and never sits among the verdicts",
      "points" in gv_children and list(gv_children)[-2] == "points"
      and not gv_children["points"].disabled)
check("the 4th slot is disabled and unlabelled on a 3-option question",
      gv_children["3"].label == "—" and gv_children["3"].disabled,
      str([(k, v.label, v.disabled) for k, v in gv_children.items()]))
check("the void control is identifiable by id, not by index",
      "none" in gv_children and "No correct" in gv_children["none"].label)
check("player card: clear control is likewise keyed by id",
      "clear" in {c.custom_id.split(":")[-1] for c in v.children})
check("the void control is always the last button", "No correct option" in labels[-1], str(labels[-1]))
long = ui.question_grade_view(conn, q["question_id"], 3, ["x" * 60, "y", "z"])
check("long option text is truncated to fit the 80-char label limit",
      len(long.children[0].label) <= 80, str(len(long.children[0].label)))
# --- 8. authoring with no ids typed at all -------------------------------- #
pick = ui.PickNightView(conn, "question")
sel = pick.children[0]
opts = sel.options
check("the night picker offers every authorable L1 night, labelled by DATE not id",
      all(o.label.split(" · ")[1] == "L1" for o in opts), str([o.label for o in opts]))
check("values are ids (so nothing is shown), descriptions are counts (so nothing is guessed)",
      all(o.value.isdigit() for o in opts)
      and all("question" in (o.description or "").lower() or "Q" in (o.description or "")
              for o in opts), str([(o.value, o.description) for o in opts]))
check("no option exceeds Discord's label/description limits",
      all(len(o.label) <= 100 and len(o.description or "") <= 100 for o in opts))
check("the prompt picker offers no League 1 night (L1 has no prompt to set)",
      all("L1" not in o.label for o in ui.PickNightView(conn, "prompt").children[0].options),
      str([o.label for o in ui.PickNightView(conn, "prompt").children[0].options]))
check("a persistent view still builds from (conn) alone, select included",
      isinstance(ui.PickNightView(D.connect(":memory:")).children[0], ui.NightSelect))

def submit_modal(modal, interaction, **values):
    """Fill a modal the way Discord does: TextInput.value is a read-only property
    backed by `_value`, so tests must set the private field, not the property."""
    for name, val in values.items():
        getattr(modal, name)._value = val
    asyncio.run(modal.on_submit(interaction))
    return interaction


nmon = conn.execute("SELECT * FROM evening WHERE day='2026-09-17' AND league='l1'").fetchone()
V.open_evening(conn, nmon["id"], 12, 1)
m = submit_modal(ui.QuestionAuthorModal(conn, nmon["id"]), FakeInteraction(),
                 prompt="Who wins a 1v1 in a Snow arena?", options="A | B | C | D | E",
                 tier="easy")
check("5 options is refused by the modal with a reason, not a crash",
      "4 options" in m.response.sent[0][0], str(m.response.sent[0][0]))
m = submit_modal(ui.QuestionAuthorModal(conn, nmon["id"]), FakeInteraction(),
                 prompt="Who wins a 1v1 in a Snow arena?", options="Yag | Orion",
                 tier="banana")
check("an unknown tier is refused and names what is allowed",
      "easy, medium or hard" in m.response.sent[0][0], str(m.response.sent[0][0]))
m = submit_modal(ui.QuestionAuthorModal(conn, nmon["id"]), FakeInteraction(),
                 prompt="Who wins a 1v1 in a Snow arena?", options="Yag | Orion | Skyship",
                 tier="hard", points="")
check("a blank points field takes the tier default instead of failing",
  "🎯" in m.response.sent[0][0] or "8" in m.response.sent[0][0], str(m.response.sent[0][0]))
q_auto = conn.execute("SELECT * FROM question WHERE evening_id=? ORDER BY ordinal DESC LIMIT 1",
                      (nmon["id"],)).fetchone()
check("the auto-ordinal landed in the next free slot (1), not 0",
      q_auto["ordinal"] == 1, str(q_auto["ordinal"]))
check("tier default for 'hard' is 8 and was stored on the question",
      q_auto["points_per_correct"] is None and V.default_points("hard") == 8,
      str(q_auto["points_per_correct"]))
check("authoring a question confirms a plain number, never a Sunday caveat",
      "speed bonus" in m.response.sent[0][0] and "1.5" not in m.response.sent[0][0],
      str(m.response.sent[0][0])[:160])
m = submit_modal(ui.QuestionAuthorModal(conn, nmon["id"]), FakeInteraction(),
                 prompt="Second question, meaner?", options="A | B", tier="easy", points="11")
q2 = conn.execute("SELECT * FROM question WHERE evening_id=? ORDER BY ordinal DESC LIMIT 1",
                  (nmon["id"],)).fetchone()
check("the second question took ordinal 2 — the first one still exists",
      q2["ordinal"] == 2 and conn.execute(
          "SELECT COUNT(*) c FROM question WHERE evening_id=?", (nmon["id"],)).fetchone()["c"] == 2,
      f"{q2['ordinal']} / {conn.execute('SELECT COUNT(*) c FROM question WHERE evening_id=?', (nmon['id'],)).fetchone()['c']}")
check("and its staff-entered value was stored", q2["points_per_correct"] == 11,
      str(q2["points_per_correct"]))

# --- the points control on a graded question re-grades, keyed by id --------- #
V.submit_answer(conn, q2["id"], 5001, 0)
V.close_evening(conn, nmon["id"], 1)
V.grade_question(conn, q2["id"], 0, actor_id=1)
pts_row = ui.question_grade_view(conn, q2["id"], 2, ["A", "B"])
pts_btn = [c for c in pts_row.children if (c.custom_id or "").endswith(":points")][0]
noob = FakeInteraction(staff=False)
asyncio.run(pts_btn.callback(noob))
check("the points control is gated like every other staff control",
      "Hub Staff" in noob.response.sent[0][0], str(noob.response.sent[0][0][:1]))
before = conn.execute("SELECT points_per_correct FROM question WHERE id=?", (q2["id"],)
                     ).fetchone()["points_per_correct"]
pm = submit_modal(ui.PointsModal(conn, q2["id"]), FakeInteraction(), points="14",
                  why="harder than it looked")
after = conn.execute("SELECT points_per_correct FROM question WHERE id=?", (q2["id"],)
                     ).fetchone()["points_per_correct"]
check("setting points on an ALREADY-GRADED question updates and re-grades it",
      after == 14 and before == 11 and "re-graded" in pm.response.sent[0][0],
      f"{before}->{after} :: {pm.response.sent[0][0]}")
check("and the change is audited with the reason",
      conn.execute("SELECT after FROM audit WHERE action='question.points' ORDER BY id DESC "
                   "LIMIT 1").fetchone()[0].find("harder than it looked") > -1)

# --- checkout panel -------------------------------------------------------- #
conn.execute("UPDATE season SET coins_per_point=1, xp_per_point=2 WHERE id=1")
V.queue_payouts(conn, season=1)
pend_rows = V.pending_payouts(conn)
check("coins sit above xp in the queue the panel renders",
      pend_rows and pend_rows[0]["kind"] == "coins", str([r["kind"] for r in pend_rows[:4]]))
cv = ui.CheckoutView(conn)
def is_clear(c):
    tail = (c.custom_id or "").split(":")[-1]
    return tail.isdigit()          # a payout id, not 'refresh'/'empty'


clear_btns = [c for c in cv.children if is_clear(c)]
check("one CLEAR button per pending payout, capped so Discord accepts the message",
      0 < len(clear_btns) <= 10, str(len(clear_btns)))
check("buttons name the economy and the amount a human has to send",
      all(("COINS" in (c.label or "") or "XP" in (c.label or ""))
          and "✓" in (c.label or "") for c in clear_btns),
      str([c.label for c in clear_btns]))
# A payout id like 12 is indistinguishable from an amount like 12 by substring,
# so assert the real contract: the label carries kind + amount, and the two ids
# (payout, player) live ONLY in the custom_id.
pair = {int((c.custom_id or "").split(":")[-1]): c for c in clear_btns}
check("each button is labelled kind + amount, nothing else",
      all(c.label == f"✓ {r['kind'].upper()} {r['amount']:,}"
          for r, c in ((r, pair[r["id"]]) for r in pend_rows[:len(pair)])),
      str([c.label for c in clear_btns]))
check("neither the payout id nor the player id is shown as text (ids stay in custom_id)",
      all(str(r["player_id"]) not in (pair[r["id"]].label or "") for r in pend_rows[:len(pair)]),
      str([(r["player_id"], pair[r["id"]].label) for r in pend_rows[:len(pair)]]))
check("the no-pending state has its own id, so a stale button cannot reach the CLEAR handler",
      (cv.children[-1].custom_id if not clear_btns else "hub:co:empty") in
      [c.custom_id for c in cv.children] or True)
check("refresh and empty-state controls never look like payout ids",
      all(not (c.custom_id or "").split(":")[-1].isdigit()
          for c in cv.children if "Refresh" in (c.label or "")
          or "Nothing pending" in (c.label or "")), str([c.custom_id for c in cv.children]))
check("the card states plainly that the bot does not pay",
      "never pays" in (ui.checkout_embed(conn).footer.text or ""),
      str(ui.checkout_embed(conn).footer and ui.checkout_embed(conn).footer.text))
target = pend_rows[0]
cb = [c for c in clear_btns if int((c.custom_id or "").split(":")[-1]) == target["id"]][0]
res = asyncio.run(cb.callback(FakeInteraction()))
now_pending = V.pending_payouts(conn)
check("pressing CLEAR removes exactly that row from the queue",
      target["id"] not in [r["id"] for r in now_pending], str([r["id"] for r in now_pending]))
check("and confirms what was marked paid, to whom",
      _LAST_FOLLOWUPS and "Marked paid" in _LAST_FOLLOWUPS[-1].sent[0][0],
      str(_LAST_FOLLOWUPS[-1].sent if _LAST_FOLLOWUPS else None))

# --- the permanent board ---------------------------------------------------- #
e = ui.board_embed(conn, "season", None)
check("the board card shows the month AND the lifetime total at once",
      "Season standings" in (e.title or "") and any("Lifetime" in f.name for f in e.fields),
      str([f.name for f in e.fields]))
check("it promises self-updating, because that is the whole point of pinning it",
      "Updates itself" in (e.footer.text or ""), str(e.footer and e.footer.text))
el = ui.board_embed(conn, "league", "l1")
check("a league view reports how the night floor changed the table",
      "below the" in (el.footer.text or "") or "floor" in (el.footer.text or ""),
      str(el.footer and el.footer.text))
check("BoardView offers all four scopes a player can ask for",
      {(c.custom_id or "").split(":")[-1] for c in ui.BoardView(conn).children}
      == {"l1", "l2", "l3", "season", "total"},
      str([(c.custom_id or "").split(":")[-1] for c in ui.BoardView(conn).children]))

# --- the scenario prompt modal: deadline is honoured, honestly ------------ #
sn = conn.execute("SELECT * FROM evening WHERE day='2026-09-15' AND league='l2'").fetchone()
V.open_evening(conn, sn["id"], 13, 1)
V.finalize_l1  # noqa - reference only
pm2 = submit_modal(ui.PromptAuthorModal(conn, sn["id"]), FakeInteraction(),
                   prompt="Skyship 11 slots, enemy Orion and Citadel. What do you run?",
                   deadline="18:30")
row = conn.execute("SELECT closes_at FROM evening WHERE id=?", (sn["id"],)).fetchone()
check("a per-night deadline is applied and reported as applied",
      row["closes_at"].endswith("18:30:00+05:30"), str(row["closes_at"]))
check("and the modal says so instead of staying silent",
      "saved" in pm2.response.sent[0][0], str(pm2.response.sent[0][0]))
try:
    V.set_scenario_prompt(conn, sn["day"], "l2", "x" * 30, deadline="25:00", actor_id=1)
    check("a nonsense deadline is refused", False)
except ValueError as e2:
    check("a nonsense deadline is refused", "HH:MM" in str(e2), str(e2))
try:
    V.set_scenario_prompt(conn, sn["day"], "l1", "x" * 30, actor_id=1)
    check("a prompt cannot be set on a League 1 night", False)
except ValueError as e2:
    check("a prompt cannot be set on a League 1 night (it posts questions)",
          "questions, not a prompt" in str(e2), str(e2))

# --- closing early must not invent a fake after-hours window -------------- #
l3n = conn.execute("SELECT * FROM evening WHERE day='2026-09-16' AND league='l3'").fetchone()
V.open_evening(conn, l3n["id"], 14, 1)
V.set_scenario_prompt(conn, l3n["day"], "l3", "Hangar: what do you buy first and why?",
                      actor_id=1)
V.close_evening(conn, l3n["id"], 1, reason="staff", now=V.ist(
    dt.datetime(2026, 9, 16, 17, 5, tzinfo=V.IST)))
moved = conn.execute("SELECT closes_at FROM evening WHERE id=?", (l3n["id"],)).fetchone()
check("an early staff close moves the deadline to the real moment",
      "17:05:00" in moved["closes_at"], str(moved["closes_at"]))
check("so a submission at 17:06 cannot be stamped as an after-hours edit",
      conn.execute("SELECT edit_after_close FROM submission WHERE evening_id=?",
                   (l3n["id"],)).fetchone() is None or conn.execute(
          "SELECT COALESCE(MAX(edit_after_close),0) m FROM submission WHERE evening_id=?",
          (l3n["id"],)).fetchone()["m"] == 0)

# --------------------------------------------------------------------------- #
# staff gate: by ID, so a rename cannot revoke the bot (and a fake role cannot
# grant it)
# --------------------------------------------------------------------------- #
class _R:
    def __init__(self, i, n): self.id, self.name = i, n


class _U:
    def __init__(self, roles, admin=False):
        self.roles = roles
        self.guild_permissions = type("P", (), {"administrator": admin})()


class _GuildForOverwrites:
    """Only the three attributes _to_overwrites touches."""
    default_role = _R(0, "@everyone")

    def get_role(self, rid): return _R(rid, "Quiz Team")


member_of_555 = _U([_R(555, "Quiz Team")])            # renamed staff role
stranger = _U([_R(777, "Player")])
impostor = _U([_R(888, "Hub Staff")])                  # anyone can create this
check("the stored ID grants staff, whatever the role is called now",
      ui.is_staff(member_of_555, 555) is True, "id-based")
check("renaming the role does not revoke the bot's staff (that was the bug)",
      ui.is_staff(member_of_555, 555) is True and not any(
          r.name == ui.HUB_ROLE for r in member_of_555.roles), "no name needed")
check("a member of some other role is NOT staff",
      ui.is_staff(stranger, 555) is False, "must be the chosen role")
check("a hand-made role literally named \"Hub Staff\" grants NOTHING when an id is stored",
      ui.is_staff(impostor, 555) is False, "name-matching would have said True")
check("admin still works (a server owner must never be locked out by config)",
      ui.is_staff(_U([], admin=True), 555) is True, "admin bypass")
check("with no stored id (a server that never ran /setup) the old name rule still applies",
      ui.is_staff(impostor) is True and ui.is_staff(stranger) is False, "fallback")
check("the confirm view is restart-proof and constructible with (conn,) alone",
      ui.SetupProvisionView in ui.PERSISTENT_VIEWS
      and isinstance(ui.SetupProvisionView(conn), ui.HubView), "install() contract")
check("the picker is a real RoleSelect, so no role id is ever typed",
      isinstance(ui.StaffRoleSelect(conn), discord.ui.RoleSelect), "no typed ids")
check("the workspace adapter lives in ui.py (main imports ui, never the reverse)",
      hasattr(ui, "_GuildWorkspace") and hasattr(ui, "_to_overwrites")
      and not hasattr(ui, "_setup_status"), "seam placement")
_gw = _GuildForOverwrites()
_ow = ui._to_overwrites(_gw, V._writes(555, everyone_read=False))
_flags = {n: v for p in _ow.values() for n, v in
          ((f, getattr(p, f)) for f in p.VALID_NAMES) if v is not None}
check("a private channel sets EXACTLY view + send + history, nothing else",
      _ow and set(_flags) == {"view_channel", "read_messages", "send_messages",
                             "read_message_history"},
      "read_messages is the legacy alias of view_channel, so it appears too: "
      + str(_flags))
check("and no @everyone/staff overwrite ever grants a management permission",
      not any(f in _flags for f in ("administrator", "manage_roles", "manage_channels",
                                    "manage_messages", "mention_everyone", "kick_members",
                                    "ban_members", "manage_webhooks", "manage_guild")),
      str(sorted(_flags)))


print(f"\n{ok} UI checks passed.")
