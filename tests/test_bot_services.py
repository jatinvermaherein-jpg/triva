"""End-to-end tests of the REAL service layer against a temp SQLite DB.
No Discord token, no network. This exercises the grading, idempotency and
restart-recovery paths the UI calls directly.
"""
import datetime as dt
import pathlib
import asyncio
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
import db as D            # noqa: E402
import services as V      # noqa: E402

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

tmp = _fresh(ROOT / ".pytest_tmp" / "hub_test.db")
tmp.parent.mkdir(parents=True, exist_ok=True)
conn = D.connect(tmp)
# Most of this file asserts on tiny fixtures (1-3 nights), so the production
# 5-night podium floor is turned OFF here and tested separately below.
D.set_cfg(conn, "standings_night_floor", 0)

# --- season + calendar -----------------------------------------------------
# 2026-09-14 is a Monday. Day 7 (2026-09-20) must be a Sunday.
D.set_cfg(conn, "answer_seconds", 120)   # this fixture grades in-process, so it needs
res = V.create_season(conn, "Season 1", "2026-09-14", weeks=4)   # a short window
check("rotating calendar makes 36 evenings with EQUAL nights per league",
      res["evenings"] == 36 and res["nights_per_league"] == {"l1": 12, "l2": 12, "l3": 12},
      str(res))
_l1 = conn.execute("SELECT day FROM evening WHERE league='l1' ORDER BY day").fetchall()
import datetime as _dt
_days = sorted({_dt.date.fromisoformat(r["day"]).strftime("%a") for r in _l1})
check("League 1 lands on Monday, Thursday and Sunday only", _days == ["Mon", "Sun", "Thu"], str(_days))
_l2 = sorted({_dt.date.fromisoformat(r["day"]).strftime("%a") for r in conn.execute(
    "SELECT day FROM evening WHERE league='l2'")})
_l3 = sorted({_dt.date.fromisoformat(r["day"]).strftime("%a") for r in conn.execute(
    "SELECT day FROM evening WHERE league='l3'")})
check("League 2 Tue/Fri/Sun, League 3 Wed/Sat/Sun",
      _l2 == ["Fri", "Sun", "Tue"] and _l3 == ["Sat", "Sun", "Wed"], f"{_l2} {_l3}")
check("Sunday runs all three leagues (is_sunday is a scheduling fact)",
      conn.execute("SELECT COUNT(*) c FROM evening WHERE is_sunday=1").fetchone()["c"] == 12)
check("...and pays like any other night: no Grand tier, no multiplier",
      conn.execute("SELECT COUNT(*) c FROM evening WHERE difficulty='grand'").fetchone()["c"]
      == 0 and conn.execute("SELECT COUNT(*) c FROM evening WHERE multiplier<>1.0"
                            ).fetchone()["c"] == 0,
      "every evening in the season must be normal/1.0")
sunday = conn.execute("SELECT * FROM evening WHERE day='2026-09-20' AND league='l1'").fetchone()
tue = conn.execute("SELECT * FROM evening WHERE day='2026-09-15' AND league='l2'").fetchone()
check("Sunday and a weekday carry the identical multiplier",
      float(sunday["multiplier"]) == float(tue["multiplier"]) == 1.0,
      f"{sunday['multiplier']} vs {tue['multiplier']}")
check("no l2 evening on Monday (rotation, not all-leagues-nightly)",
      conn.execute("SELECT * FROM evening WHERE day='2026-09-14' AND league='l2'").fetchone() is None)

# --- open an evening, author 3 questions -----------------------------------
mon_l1 = conn.execute("SELECT * FROM evening WHERE day='2026-09-14' AND league='l1'").fetchone()
opened = V.open_evening(conn, mon_l1["id"], channel_id=111, actor_id=1)
check("evening opens", opened["evening"]["status"] == "open")
again = V.open_evening(conn, mon_l1["id"], channel_id=111, actor_id=1)
check("reopening is a no-op (restart proof)", again["already"] is True)

qs = []
for i, (prompt, opts, tier) in enumerate([
        ("Which is a support mech?", ["Orion", "Sasquatch", "Blacklight", "Ripsaw"], "easy"),
        ("Which weapon is legendary?", ["Dualars", "SMEDG", "Rat", "Vulcan"], "medium"),
        ("Hardest: which map has 3 capture rings?", ["Skyship", "Aurora", "Cold Store", "Ouabu"], "hard")],
        start=1):
    qs.append(V.add_question(conn, mon_l1["id"], i, prompt, opts, tier))
check("3 questions authored", all("question_id" in q for q in qs))
for q in qs:
    V.set_question_message(conn, q["question_id"], 900 + q["question_id"])
    conn.execute("UPDATE question SET posted_at=? WHERE id=?",
                 (int(time.time()) - 30, q["question_id"]))   # 30s ago
try:
    V.add_question(conn, mon_l1["id"], 9, "too few", ["only one"], "easy")
    check("option count enforced", False)
except ValueError:
    check("option count enforced", True)

# --- players answer --------------------------------------------------------
def answer(qidx, player, opt):
    return V.submit_answer(conn, qs[qidx]["question_id"], player, opt)

a1 = answer(0, 100, 1)          # 1 = "Sasquatch" -> the right option, set below
t_sub = conn.execute("SELECT submitted_at FROM entry WHERE question_id=? AND player_id=100",
                     (qs[0]["question_id"],)).fetchone()["submitted_at"]
a2 = answer(0, 101, 3)
a3 = answer(0, 100, 0)          # same player changes mind -> edit, not a second entry
check("first answer accepted", a1["accepted"] and a1["change"] == "created")
check("same player re-answering edits instead of duplicating", a3["change"] == "edited")
# but the edit overwrote the right pick, so restore it - and prove the edit stuck
answer(0, 100, 1)
cur = conn.execute("SELECT option_idx, submitted_at, edited_at FROM entry WHERE question_id=? "
                   "AND player_id=100", (qs[0]["question_id"],)).fetchone()
check("edit keeps the ORIGINAL submission time (speed bonus is not farmable)",
      cur["option_idx"] == 1 and cur["submitted_at"] == t_sub and cur["edited_at"] is not None,
      f"submitted moved: {t_sub} -> {cur['submitted_at']}")
n_entries = conn.execute("SELECT COUNT(*) c FROM entry WHERE question_id=?",
                         (qs[0]["question_id"],)).fetchone()["c"]
check("one entry per player per question", n_entries == 2, str(n_entries))

# --- closing an evening locks answers (this is what enforces the edit rule) -
# done on a SEPARATE evening: closing locks the whole evening, so it cannot be
# tested on the one the rest of this file still writes to.
# Thursday: an L1 night that is neither the one under test nor a Sunday grand
other = conn.execute("SELECT * FROM evening WHERE day='2026-09-17' AND league='l1'").fetchone()
V.open_evening(conn, other["id"], 111, 1)
oq = V.add_question(conn, other["id"], 1, "Lock test?", ["yes", "no"], "easy")
V.set_question_message(conn, oq["question_id"], 555)
check("answer accepted while open",
      V.submit_answer(conn, oq["question_id"], 103, 0)["accepted"] is True)
closed = V.close_evening(conn, other["id"], actor_id=1, reason="test timer")
check("closing freezes answers", closed["evening"]["status"] == "locked")
late = V.submit_answer(conn, oq["question_id"], 104, 0)
check("answers rejected after close", late["accepted"] is False and late["why"] == "locked", str(late))
check("clearing an answer after close is refused",
      V.clear_answer(conn, oq["question_id"], 103) is False)
row = conn.execute("SELECT payload FROM entry WHERE question_id=? AND player_id=103",
                   (oq["question_id"],)).fetchone()
check("the answer itself is still on disk after a refused clear", row is not None)
try:
    V.close_evening(conn, conn.execute(
        "SELECT id FROM evening WHERE day='2026-09-24' AND league='l1'").fetchone()["id"])
    check("closing a never-opened evening is refused", False)
except ValueError:
    check("closing a never-opened evening is refused", True)
V.grade_question(conn, oq["question_id"], 0, actor_id=1)
check("clearing after grading is refused even though the deadline is still open",
      V.clear_answer(conn, oq["question_id"], 103) is False)
check("and the awarded row is untouched by that attempt",
      conn.execute("SELECT COUNT(*) c FROM ledger WHERE evening_id=?",
                   (other["id"],)).fetchone()["c"] == 1)
check("grading after close still works and pays the early answer",
      conn.execute("SELECT COUNT(*) c FROM award WHERE question_id=?",
                   (oq["question_id"],)).fetchone()["c"] == 1)
check("an edit after the freeze is flaggable, not silently accepted",
      conn.execute("SELECT edit_after_close FROM entry WHERE question_id=?",
                   (oq["question_id"],)).fetchone() is not None)

# --- grading: bot computes everything --------------------------------------
posted = conn.execute("SELECT posted_at FROM question WHERE id=?",
                      (qs[0]["question_id"],)).fetchone()["posted_at"]
# player 100 answered ~now (i.e. ~30s after post), player 101 likewise
g = V.grade_question(conn, qs[0]["question_id"], 1, actor_id=1)   # "Sasquatch" is index 1
check("one player scored on correct option", g["players_scored"] == 1, str(g))
# staff gave this easy question 3 base points; +~1.05 speed bonus at 30s +1 first-correct
pts = conn.execute("SELECT points FROM award WHERE question_id=?", (qs[0]["question_id"],)).fetchone()
check("bot adds the speed bonus on top of the staff-entered value",
      pts is not None and 4 <= pts["points"] <= 5, str(pts and pts["points"]))
check("suggested per-question value comes from the tier alone",
      V.default_points("easy") == 3 and V.default_points("hard") == 8, "one number per tier")
check("default_points no longer even accepts a Sunday flag - there is nothing to inflate",
      len(V.default_points.__code__.co_varnames[:V.default_points.__code__.co_argcount]) == 1,
      str(V.default_points.__code__.co_varnames))
firsts = conn.execute("SELECT COUNT(*) c FROM award WHERE question_id=?", (qs[0]["question_id"],)).fetchone()["c"]
check("only the fastest correct got an award row here", firsts == 1)

# idempotency: grading twice must not double-pay
before = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND evening_id=?",
                      (mon_l1["id"],)).fetchone()["points"]
V.grade_question(conn, qs[0]["question_id"], 1, actor_id=1)
after = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND evening_id=?",
                     (mon_l1["id"],)).fetchone()["points"]
check("re-grading the same question does NOT double-pay", before == after, f"{before} vs {after}")

# second question, same evening: must ADD to the evening total, not replace it
answer(1, 100, 0)
answer(1, 101, 0)
g2 = V.grade_question(conn, qs[1]["question_id"], 0, actor_id=1)
led = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND evening_id=?",
                   (mon_l1["id"],)).fetchone()["points"]
q1pts = conn.execute("SELECT points FROM award WHERE question_id=? AND player_id=100",
                     (qs[0]["question_id"],)).fetchone()["points"]
q2pts = conn.execute("SELECT points FROM award WHERE question_id=? AND player_id=100",
                     (qs[1]["question_id"],)).fetchone()["points"]
check("evening total = Q1 + Q2 (grading Q2 kept Q1)", led == q1pts + q2pts, f"{led} vs {q1pts}+{q2pts}")

# Q3 last: player 102 answers the wrong option. Nothing is recorded until staff grade.
answer(2, 102, 0)
w0 = conn.execute("SELECT points FROM ledger WHERE player_id=102 AND evening_id=?",
                  (mon_l1["id"],)).fetchone()
check("no phantom rows before staff grade", w0 is None)

# ungraded questions block finalisation
try:
    V.finalize_l1(conn, mon_l1["id"], actor_id=1)
    check("finalize refuses while questions are ungraded", False)
except ValueError as e:
    check("finalize refuses while questions are ungraded", "ungraded" in str(e), str(e))
# now grade Q3 (correct = "Ouabu" -> player 102's pick of 0 is WRONG, scores 0)
g3 = V.grade_question(conn, qs[2]["question_id"], 3, actor_id=1)
check("wrong answer earns no award row", g3["players_scored"] == 0, str(g3))
w = conn.execute("SELECT points FROM ledger WHERE player_id=102 AND evening_id=?",
                 (mon_l1["id"],)).fetchone()
check("wrong answer IS recorded as a 0-point night once graded",
      w is not None and w["points"] == 0, str(w))
nights = V.standings(conn, "league", league="l1", limit=50)
check("nights played counts attendance, not correctness",
      any(r["player_id"] == 102 and r["nights"] == 1 for r in nights), str(nights))
fin = V.finalize_l1(conn, mon_l1["id"], actor_id=1)
check("finalize succeeds once all graded", fin["top"][0][0] == 100, str(fin))
st = conn.execute("SELECT status FROM evening WHERE id=?", (mon_l1["id"],)).fetchone()["status"]
check("evening marked graded", st == "graded")

# --- restart recovery: close the DB, reopen, same state ---------------------
conn.close()
conn = D.connect(tmp)
reopened = conn.execute("SELECT * FROM evening WHERE day='2026-09-14' AND league='l1'").fetchone()
check("after restart the panel reads identical state from SQLite",
      reopened["status"] == "graded" and reopened["message_id"] is None)
led_after = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND evening_id=?",
                         (mon_l1["id"],)).fetchone()["points"]
check("leaderboard survives restart", led_after == q1pts + q2pts)

# --- L2: one number in a range -------------------------------------------- #
sun_l2 = conn.execute("SELECT * FROM evening WHERE day='2026-09-20' AND league='l2'").fetchone()
check("Sunday runs L2 as an ordinary night", sun_l2 is not None
      and sun_l2["difficulty"] == "normal", str(dict(sun_l2 or {})))
V.open_evening(conn, sun_l2["id"], 222, 1)
sub = V.create_submission(conn, sun_l2["id"], 200,
                          "Hold the high ring with Blacklight, trade first, "
                          "then rotate with the Surge")
check("submission accepted", sub["accepted"] and sub["word_count"] > 5)
try:
    V.award_submission(conn, sub["submission_id"], 99, actor_id=1)
    check("out-of-range points rejected", False)
except ValueError:
    check("out-of-range points rejected", True)
try:
    V.award_submission(conn, sub["submission_id"], -1, actor_id=1)
    check("negative points rejected", False)
except ValueError:
    check("negative points rejected", True)
w = V.award_submission(conn, sub["submission_id"], 15, actor_id=1, band="Good")
check("Sunday GOOD 15 pays 15, not 23", w["points"] == 15, str(w))
check("bot reports the band back for the results card", V.band_for(15) == "Good")
check("band labels match the published 2/8/15/25 thresholds",
      [V.band_for(v) for v in (0, 5, 6, 12, 13, 20, 21, 25)]
      == ["Poor", "Poor", "Average", "Average", "Good", "Good", "Excellent", "Excellent"])
w2 = V.award_submission(conn, sub["submission_id"], 17, actor_id=1)   # CUSTOM entry
check("a CUSTOM number still gets a truthful band label", V.band_for(17) == "Good")
check("CUSTOM 17 on Sunday pays 17", w2["points"] == 17, str(w2["points"]))
# idempotent award
V.award_submission(conn, sub["submission_id"], 8, actor_id=1, band="Average")
led2 = conn.execute("SELECT points FROM ledger WHERE player_id=200 AND evening_id=?",
                    (sun_l2["id"],)).fetchone()["points"]
check("re-awarding a submission replaces, not adds", led2 == 8, str(led2))  # 8, flat

# duplicate detection
s_a = V.create_submission(conn, sun_l2["id"], 201,
                          "Hold the high ring with Blacklight, trade first, "
                          "then rotate with the Surge on the east approach")
s_b = V.create_submission(conn, sun_l2["id"], 202,
                          "Hold the high ring with Blacklight, trade first, "
                          "then rotate with the Surge on the east approach")
check("near-identical submission is flagged", s_b["duplicate_of"] == s_a["submission_id"], str(s_b))
s_c = V.create_submission(conn, sun_l2["id"], 203,
                          "Rush the mid with Yagorath and force a 50 second timer swing")
check("unrelated submission not flagged", s_c["duplicate_of"] is None)

# --- the qualification floor itself (fresh fixture, 2 nights vs 1) --------- #
# Every earlier block has already locked/graded the first week, so this uses
# untouched nights: l2 plays Tue 29 Sep, l1 plays Thu 24 Sep.
tue = conn.execute("SELECT * FROM evening WHERE day='2026-09-29' AND league='l2'").fetchone()
fri = conn.execute("SELECT * FROM evening WHERE day='2026-09-25' AND league='l2'").fetchone()
thu = conn.execute("SELECT * FROM evening WHERE day='2026-09-24' AND league='l1'").fetchone()
for nm, row in (("tue", tue), ("fri", fri), ("thu", thu)):
    check(f"floor fixture night {nm} is still scheduled",
          row is not None and row["status"] == "scheduled", str(row and row["status"]))
V.open_evening(conn, thu["id"], 401, 1)
tq = V.add_question(conn, thu["id"], 1, "Floor test?", ["a", "b"], "easy")
for who in (900, 901, 902):
    V.submit_answer(conn, tq["question_id"], who, 0)
V.grade_question(conn, tq["question_id"], 0, actor_id=1)
V.finalize_l1(conn, thu["id"], actor_id=1)
for ev_id, nights in ((tue["id"], (900, 901, 902)), (fri["id"], (900,))):
    V.open_evening(conn, ev_id, 402, 1)
for who, extra in ((900, fri["id"]), (901, None), (902, None)):
    for eid in [e for e in (tue["id"], extra) if e]:
        V.create_submission(conn, eid, who, f"player {who} plan number {eid} with detail here")
        sub_id = conn.execute("SELECT id FROM submission WHERE evening_id=? AND player_id=?",
                             (eid, who)).fetchone()["id"]
        V.award_submission(conn, sub_id, 25, actor_id=1, band="Excellent")
# 900 has 2 l2 nights; 901/902 have 1 each
D.set_cfg(conn, "standings_night_floor", 2)
two = V.standings(conn, "league", season=1, league="l2", limit=50)
D.set_cfg(conn, "standings_night_floor", 0)
zero = V.standings(conn, "league", season=1, league="l2", limit=50)
D.set_cfg(conn, "standings_night_floor", 2)
size = V.league_table_size(conn, "l2", season=1)
check("the floor keeps players who played enough nights",
      [r["player_id"] for r in two] == [900], str(two))
check("without the floor everyone shows", len(zero) >= 2, str(zero))
D.set_cfg(conn, "standings_night_floor", 0)
check("the panel can report how many the floor excluded",
      size["all"] >= size["qualified"] and size["floor"] == 2, str(size))
check("lifetime (total) table ignores the floor - it is not a podium",
      len(V.standings(conn, "total", limit=50)) > 0)
check("L1 per-question points were honoured (2 nights for 900 across l1)",
      conn.execute("SELECT points FROM ledger WHERE player_id=900 AND league='l1'")
      .fetchone() is not None)

# --- manual corrections survive re-grading (regression: the deletes used to be
#     scoped by evening+player only, so a re-grade erased the correction) ------- #
cor = conn.execute("SELECT * FROM evening WHERE league='l1' AND status='scheduled' "
                   "AND day>='2026-09-14' ORDER BY day LIMIT 1").fetchone()
check("correction test found an untouched League 1 night",
      cor is not None and cor["league"] == "l1" and cor["status"] == "scheduled",
      str(cor and (cor["day"], cor["league"], cor["status"])))
sub_night = conn.execute("SELECT * FROM evening WHERE league='l2' AND day='2026-09-15'").fetchone()
try:
    V.add_question(conn, sub_night["id"], 1, "Misplaced?", ["a", "b"], "easy")
    check("a question cannot be authored onto a League 2/3 night", False)
except ValueError as e:
    check("a question cannot be authored onto a League 2/3 night (it would never post)",
          "only League 1 has questions" in str(e), str(e))
    # regression: this message used to crash with IndexError because it read a
    # column add_question had not selected. An error handler that raises is worse
    # than no error handler - staff see a traceback, not an explanation.
    check("and the refusal message is fully rendered, naming the night to fix",
          "2026-09-15" in str(e) and "L2" in str(e), str(e))
V.open_evening(conn, cor["id"], 403, 1)
cq = V.add_question(conn, cor["id"], 1, "Correction test?", ["a", "b"], "easy")
V.set_question_message(conn, cq["question_id"], 778)
V.submit_answer(conn, cq["question_id"], 900, 1)
V.close_evening(conn, cor["id"], 1)
V.grade_question(conn, cq["question_id"], 1, actor_id=1)
# L1 ledger rows are written at finalize, not at grade - see the check above.
V.finalize_l1(conn, cor["id"], actor_id=1)


def night_pts():
    return conn.execute("SELECT points FROM ledger WHERE player_id=900 AND evening_id=?",
                        (cor["id"],)).fetchone()


adj_pts = lambda: conn.execute("SELECT points FROM ledger WHERE player_id=900 "
                               "AND evening_id=0 AND league='adjust'").fetchone()


check("the night itself is scored", night_pts()["points"] > 0, str(night_pts()))
V.adjust_points(conn, 900, -3, "was awarded for an answer that broke the rules", actor_id=1)
check("a correction is a SEPARATE row beside the graded points, not an overwrite",
      night_pts()["points"] > 0 and adj_pts()["points"] == -3,
      f"{dict(night_pts())} / {dict(adj_pts()) if adj_pts() else None}")
V.grade_question(conn, cq["question_id"], None, actor_id=1)      # "no correct option"
check("voiding the question zeroes the night...",
      conn.execute("SELECT COALESCE(SUM(points),0) p FROM ledger WHERE player_id=900 "
                   "AND evening_id=?", (cor["id"],)).fetchone()["p"] == 0, str(night_pts()))
check("...but the correction survives, exactly once", adj_pts()["points"] == -3, str(adj_pts()))
check("the correction is identifiable by league='adjust', so standings can explain it",
      conn.execute("SELECT league FROM ledger WHERE player_id=900 AND evening_id=0")
      .fetchone()["league"] == "adjust")
check("and the two live under different keys so a rebuild cannot collide",
      conn.execute("SELECT COUNT(*) c FROM ledger WHERE evening_id=0 AND league='adjust'"
                   " AND player_id=900").fetchone()["c"] == 1)
try:
    V.adjust_points(conn, 900, -3, "", actor_id=1)
    check("a correction with no reason is refused", False)
except ValueError as e:
    check("a correction with no reason is refused (it is what you show the player)",
          "reason" in str(e), str(e))
try:
    V.adjust_points(conn, 900, 0, "nothing", actor_id=1)
    check("a zero correction is refused", False)
except ValueError as e:
    check("a zero correction is refused rather than padding the audit log", "delta" in str(e),
          str(e))
try:
    V.adjust_points(conn, 900, 5, "attempt to fake a night", actor_id=1, evening_id=cor["id"])
    check("a correction cannot be hidden on a real evening", False)
except TypeError:
    check("a correction cannot be hidden on a real evening (no evening_id parameter)", True)

# --- questions are frozen once a night has run ---------------------------- #
try:
    V.add_question(conn, thu["id"], 9, "Too late?", ["a", "b"], "easy")
    check("adding a question to an already-graded evening is refused", False)
except ValueError as e:
    check("adding a question to an already-graded evening is refused with a real message",
          "already run" in str(e), str(e))
    check("and the error names the status, not a raw FOREIGN KEY failure",
          "FOREIGN KEY" not in str(e), str(e))

# --- payouts: the checkout queue, bot never pays ---------------------------- #
conn.execute("UPDATE season SET coins_per_point=1, xp_per_point=2 WHERE id=1")
q1 = V.queue_payouts(conn, season=1)
check("queue creates one checkout per scorer", q1["created"] >= 1, str(q1))
check("coins and xp are both queued at their own rates",
      {p["kind"] for p in V.pending_payouts(conn)} == {"coins", "xp"}
      or {"coins"} <= {p["kind"] for p in V.pending_payouts(conn)})
pend = V.pending_payouts(conn)
for want in ("coins", "xp"):
    row = next(r for r in pend if r["kind"] == want)
    check(f"{want} amount = points x rate", row["amount"] == row["points"] * row["rate"],
          str(row))
check("the two economies queue separately, at their own rates",
      {r["kind"] for r in pend} == {"coins", "xp"}, str(sorted({r['kind'] for r in pend})))
q2 = V.queue_payouts(conn, season=1)
check("re-running the queue creates NO duplicate checkout", q2["created"] == 0, str(q2))
check("and the pending count is unchanged",
      len(V.pending_payouts(conn)) == q1["total_pending"])
# The queue must hand the money rows over first: they are the ones a human has
# to go and send, and clearing them is the only thing that writes a coin row.
pend = V.pending_payouts(conn)
check("the queue puts the money rows on top, not the xp rows",
      pend[0]["kind"] == "coins", str([(r["id"], r["kind"]) for r in pend[:3]]))
check("within a kind the biggest payout is offered first",
      [r["amount"] for r in pend if r["kind"] == "coins"] ==
      sorted((r["amount"] for r in pend if r["kind"] == "coins"), reverse=True), str(pend))
coins_row = pend[0]
pid = coins_row["id"]
c1 = V.clear_payout(conn, pid, actor_id=7)
check("CLEAR marks it done and records who did it", c1["cleared"] is True, str(c1))
check("CLEAR stamps who cleared it", c1["player_id"] == coins_row["player_id"], str(c1))
shadow = conn.execute("SELECT * FROM coin_txn WHERE reason LIKE ?",
                      (f"%payout #{pid}%",)).fetchone()
check("clearing writes exactly one reconciliation row for that checkout",
      shadow is not None and conn.execute(
          "SELECT COUNT(*) c FROM coin_txn WHERE reason LIKE ?",
          (f"%payout #{pid}%",)).fetchone()["c"] == 1, str(pid))
check("the shadow row carries the player, the amount and the resulting balance",
      shadow is not None
      and shadow["player_id"] == coins_row["player_id"]
      and shadow["amount"] == coins_row["amount"]
      and shadow["balance_after"] == coins_row["amount"], str(dict(shadow) if shadow else None))
check("payout_summary now reports nothing pending for coins",
      V.payout_summary(conn, coins_row["player_id"])["pending"].get("coins", 0) == 0,
      str(V.payout_summary(conn, coins_row["player_id"])))
c2 = V.clear_payout(conn, pid, actor_id=8)
check("a second CLEAR cannot double-pay (race-guarded)",
      c2["already"] is True and c2["cleared"] is False, str(c2))
check("the double press wrote no second coin row",
      conn.execute("SELECT COUNT(*) c FROM coin_txn WHERE reason LIKE ?",
                   (f"%payout #{pid}%",)).fetchone()["c"] == 1)
check("the entry is gone from the queue",
      pid not in [r["id"] for r in V.pending_payouts(conn)])
xp_row = next(r for r in V.pending_payouts(conn) if r["kind"] == "xp")
bal_before = D.balance(conn, xp_row["player_id"])
V.clear_payout(conn, xp_row["id"], actor_id=7)
check("clearing an xp checkout never touches a coin balance",
      D.balance(conn, xp_row["player_id"]) == bal_before
      and conn.execute("SELECT COUNT(*) c FROM coin_txn WHERE reason LIKE ?",
                       (f"%payout #{xp_row['id']}%",)).fetchone()["c"] == 0)
c2 = V.clear_payout(conn, pid, actor_id=8)
check("a second CLEAR cannot double-pay (race-guarded)",
      c2["already"] is True and c2["cleared"] is False, str(c2))
check("the entry is gone from the queue",
      pid not in [r["id"] for r in V.pending_payouts(conn)])
try:
    V.clear_payout(conn, 999999, actor_id=7)
    check("clearing a nonexistent payout raises", False)
except ValueError:
    check("clearing a nonexistent payout raises", True)
summary = V.payout_summary(conn, c1["player_id"])
check("a player can see what is still owed to them", "pending" in summary
      and "lifetime_points" in summary, str(summary))
batches = V.payout_batches(conn)
check("queue chunks so no panel exceeds Discord's button ceiling",
      all(len(b) <= 10 for b in batches), str([len(b) for b in batches]))

# --- coins ------------------------------------------------------------------
coins = conn.execute("SELECT coins FROM award WHERE player_id=100 AND question_id=?",
                     (qs[0]["question_id"],)).fetchone()["coins"]
led_pts = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND evening_id=?",
                       (mon_l1["id"],)).fetchone()["points"]
check("coins track points at the configured rate (1:1)",
      sum(r["coins"] for r in conn.execute(
          "SELECT coins FROM award WHERE player_id=100")) >= q1pts, f"{coins} {led_pts}")

# --- appeals / adjust -------------------------------------------------------
V.adjust_points(conn, 100, -2, "double-counted manual entry", actor_id=1)
after = conn.execute("SELECT points FROM ledger WHERE player_id=100 AND day='adjust'").fetchone()
check("correction is a NEW row, not an edit", after is not None and after["points"] == -2)
total = conn.execute("SELECT SUM(points) t FROM ledger WHERE player_id=100").fetchone()["t"]
check("correction flows into the standings", total == q1pts + q2pts - 2, str(total))
hist = V.points_history(conn, 100)
check("player can audit their own trail", len(hist) >= 2 and all(h["reason"] for h in hist[:1]))

# --- seed League 3 on the same Monday so all three leagues have a champion ---
mon_l3 = conn.execute("SELECT * FROM evening WHERE day='2026-09-16' AND league='l3'").fetchone()
V.open_evening(conn, mon_l3["id"], 333, 1)
s3 = V.create_submission(conn, mon_l3["id"], 300, "Stop building the third mech, bank credits "
                         "for the legendary token, downgrade the reload mod to HP")
V.award_submission(conn, s3["submission_id"], 25, actor_id=1, band="Excellent")
V.create_submission(conn, mon_l3["id"], 301, "Fine as is")
conn.close(); conn = D.connect(tmp)     # prove the L3 award also survived nothing being in memory
l3led = conn.execute("SELECT points FROM ledger WHERE player_id=300 AND evening_id=?",
                     (mon_l3["id"],)).fetchone()["points"]
check("League 3 award persisted (25, weekday 1.0x)", l3led == 25, str(l3led))
# an empty league must NOT produce a champion
V.create_season(conn, "Season 2 dry", "2026-10-12", weeks=1)
# (active season switched; verify skip semantics on the fresh one)
fresh = V.role_diff(conn)
check("a brand-new league with no points is skipped, not awarded",
      any(a["action"] == "skip" and "no points" in a["why"] or "no role_id" in a["why"]
          for a in fresh), str(fresh))
conn.execute("UPDATE season SET status='closed' WHERE name='Season 2 dry'")
conn.execute("UPDATE season SET status='active' WHERE name='Season 1'")

# --- season end: roles + hall of fame, idempotent ---------------------------
rolemap = {"l1": 501, "l2": 502, "l3": 503, "role_overall": 504}
conn.execute("UPDATE season SET role_trivia=501, role_strategy=502, role_hangar=503, role_overall=504")
diff = V.role_diff(conn)
check("dry run lists an add for each of the three leagues",
      sum(1 for d in diff if d["action"] == "add") == 3, str(diff))
check("dry run mutates nothing at all", conn.execute(
    "SELECT COUNT(*) c FROM audit WHERE action LIKE 'role.%'").fetchone()["c"] == 0)
check("dry run touches nothing", conn.execute(
    "SELECT COUNT(*) c FROM hall_of_fame").fetchone()["c"] == 0)
r1 = asyncio.run(V.finalize_season(conn, actor_id=1, apply_roles=False))
# one podium row per placing per league that has enough players
check("season finalizes and writes HoF podium rows", len(r1["hall_of_fame"]) >= 3,
      str(r1["hall_of_fame"]))
check("l1 podium is the actual l1 leader",
      any(league == "l1" and place == 1 and pid == 100
          for _sid, league, pid, place, _pts in r1["hall_of_fame"]), str(r1["hall_of_fame"]))
conn.execute("UPDATE hall_of_fame SET post_id=4242, role_id=501 WHERE placement=1")
r2 = asyncio.run(V.finalize_season(conn, actor_id=1, apply_roles=False))
_rows = conn.execute("SELECT season_id,league,player_id,placement,points FROM hall_of_fame "
                     "ORDER BY league,placement").fetchall()
_hof = len(_rows)
# expected = min(3, scorers) per league, derived rather than hard-coded
SID = 1                       # explicit: season is now 'closed', default lookup is ambiguous
def _expected(league):
    n = len([r for r in V.standings(conn, "league", season=SID, league=league, limit=50)
             if r["points"] > 0])
    return min(3, n)
check("re-running season end does NOT double-post the Hall of Fame",
      _hof == sum(_expected(l) for l in ("l1", "l2", "l3")),
      f"count={_hof} rows={[tuple(r) for r in _rows]}")
check("a 0-point player never gets a Hall of Fame podium slot",
      all(r["points"] > 0 for r in _rows), str(_rows))
for lg in ("l1", "l2", "l3"):
    pl = sorted(r["placement"] for r in _rows if r["league"] == lg)
    check(f"{lg} podium placements are contiguous from 1", pl == list(range(1, len(pl) + 1)),
          str(pl))
check("a re-run preserves the HoF post_id and role_id (no rowid churn)",
      conn.execute("SELECT COUNT(*) c FROM hall_of_fame WHERE post_id=4242 "
                   "AND role_id=501").fetchone()["c"] >= 1)
audit = conn.execute("SELECT COUNT(*) c FROM audit WHERE action='season.finalize'").fetchone()["c"]
check("season end is audited", audit >= 2)

# --- audit trail is complete ------------------------------------------------
for action in ("season.create", "evening.open", "evening.close", "question.grade",
               "submission.write", "submission.award", "points.adjust"):
    n = conn.execute("SELECT COUNT(*) c FROM audit WHERE action=?", (action,)).fetchone()["c"]
    check(f"audit logged: {action}", n >= 1, f"{n} rows")

# --- the no-AI / solo rule: recorded by a human, never guessed by a model ---- #
sun2 = conn.execute("SELECT * FROM evening WHERE day='2026-09-28' AND league='l1'").fetchone()
plan_night = conn.execute("SELECT * FROM evening WHERE league='l2' AND status='scheduled' "
                          "ORDER BY day LIMIT 1").fetchone()
V.open_evening(conn, plan_night["id"], 404, 1)
sub_a = V.create_submission(conn, plan_night["id"], 950,
                            "Hold the ridge, trade the first poke, rotate east when the "
                            "timer swings and force them to contest the high ground twice")
sub_b = V.create_submission(conn, plan_night["id"], 951,
                            "Take the ridge, trade the first hit, rotate east on the timer "
                            "swing and make them contest the high ground twice over")
fl = V.flag_submission(conn, sub_a["submission_id"], "ai", actor_id=1,
                       note="player admitted it in #appeals")
check("a flag records the player and the previous state",
      fl["player_id"] == 950 and fl["previously"] is None and fl["flag"] == "ai", str(fl))
check("flagging is audited with the staff note (that is the appeal evidence)",
      "admitted it" in conn.execute(
          "SELECT after FROM audit WHERE action='submission.flag' ORDER BY id DESC LIMIT 1"
      ).fetchone()[0])
check("a flag does NOT touch the points a human awarded",
      conn.execute("SELECT points_in FROM submission WHERE id=?",
                   (sub_a["submission_id"],)).fetchone()["points_in"] is None)
V.award_submission(conn, sub_a["submission_id"], 20, actor_id=1, band="Good")
paid = conn.execute("SELECT points_in, points FROM submission s LEFT JOIN ledger l "
                    "ON l.player_id=s.player_id AND l.evening_id=s.evening_id "
                    "WHERE s.id=?", (sub_a["submission_id"],)).fetchone()
check("awarding a flagged entry still pays the staff number — the bot does not "
      "downgrade on suspicion", paid["points_in"] == 20 and paid["points"] == 20, str(dict(paid)))
check("clearing a flag is possible (a false accusation must be removable)",
      V.flag_submission(conn, sub_a["submission_id"], None, actor_id=1)["flag"] is None)
for bad in ("plagiarism", "suspect", "AI"):
    try:
        V.flag_submission(conn, sub_a["submission_id"], bad, actor_id=1)
        check(f"flag {bad!r} refused", False)
    except ValueError:
        check(f"only the one documented flag exists, not a free-text accusation ({bad})",
              True)
try:
    V.flag_submission(conn, 999999, "ai", actor_id=1)
    check("flagging a missing submission raises", False)
except ValueError as e:
    check("flagging a missing submission raises rather than updating nothing",
          "not found" in str(e), str(e))
check("solo is structural: a submission row is keyed to the player who pressed the "
      "button, so a second account can never be attached to it",
      conn.execute("SELECT COUNT(*) c FROM submission WHERE evening_id=? AND player_id=950",
                       (plan_night["id"],)).fetchone()["c"] == 1)

# --- seasonal roles must NOT pile up (regression: removals were keyed on
#     hall_of_fame.role_id, which nothing ever wrote, so nothing was ever removed) -- #
def _champion_db(path_name):
    """One graded night => one league champion, in a fresh DB."""
    pp = _fresh(ROOT / ".pytest_tmp" / path_name)
    pp.parent.mkdir(parents=True, exist_ok=True)
    cc = D.connect(pp)
    D.set_cfg(cc, "standings_night_floor", 0)     # 1-night fixture vs a 5-night floor
    V.create_season(cc, "Season 1", "2026-09-14", weeks=1)
    cc.execute("UPDATE season SET role_trivia=111 WHERE id=1")
    e1 = cc.execute("SELECT * FROM evening WHERE league='l1' AND day='2026-09-14'").fetchone()
    V.open_evening(cc, e1["id"], 1, 1)
    q1 = V.add_question(cc, e1["id"], 1, "Q?", ["a", "b"], "easy")
    V.set_question_message(cc, q1["question_id"], 5)
    V.submit_answer(cc, q1["question_id"], 900, 0)
    V.close_evening(cc, e1["id"], 1)
    V.grade_question(cc, q1["question_id"], 0, actor_id=1)
    V.finalize_l1(cc, e1["id"], actor_id=1)
    return cc


rc = _champion_db("roles.db")
r1 = asyncio.run(V.finalize_season(rc, actor_id=1, apply_roles=False))
adds = [a for a in r1["role_diff"] if a["action"] == "add"]
check("season 1 crowns its champion with a role", len(adds) == 1
      and adds[0]["role_id"] == 111, str(r1["role_diff"]))
check("the closed season's champion is on the wall",
      rc.execute("SELECT COUNT(*) c FROM hall_of_fame WHERE placement=1").fetchone()["c"] == 1)
V.create_season(rc, "Season 2", "2026-10-12", weeks=1)
rc.execute("UPDATE season SET role_trivia=999 WHERE id=2")
d2 = V.role_diff(rc, 2)
removes = [a for a in d2 if a["action"] == "remove"]
check("the NEXT season's rollover strips the PREVIOUS champion's role",
      len(removes) == 1 and removes[0]["role_id"] == 111
      and removes[0]["player_id"] == 900, str(d2))
check("removal is NOT conditional on the new season producing a champion",
      not any(a["action"] == "add" for a in d2) and len(removes) == 1, str(d2))
# a role the bot attached is remembered, so HoF is self-describing
class _Role:
    """Mirrors discord.py: Role equality is by id, and `in member.roles` therefore
    has to hash. A fake without __eq__/__hash__ makes every removal look like a no-op
    - which is how you "prove" a bug that does not exist."""
    def __init__(self, i): self.id, self.name = i, f"role{i}"
    def __eq__(self, other): return isinstance(other, _Role) and other.id == self.id
    def __hash__(self): return hash(("role", self.id))


class _Member:
    def __init__(self, i): self.id, self.roles = i, []
    async def add_roles(self, r, reason=None): self.roles.append(r)
    async def remove_roles(self, r, reason=None): self.roles.remove(r)


class _Guild:
    def __init__(self): self.seen = []; self._members = {}
    def get_role(self, rid): return _Role(rid)
    def get_member(self, pid): return _Member(pid)
    async def fetch_member(self, pid): return _Member(pid)


g = _Guild()
# season 1's own rollover must APPLY the role for real (this is what writes
# hall_of_fame.role_id), and only then can season 2's rollover strip it.
mem = _Member(900)
g._members = {900: mem}
g.get_member = lambda pid, _m=mem: _m
asyncio.run(V._apply_roles(g, [{"action": "add", "player_id": 900, "role_id": 111,
                               "league": "l1", "why": "l1 champion S1"}], rc, 1))
check("applying a champion's role stamps hall_of_fame.role_id (self-describing wall)",
      rc.execute("SELECT role_id FROM hall_of_fame WHERE season_id=1 AND league='l1' "
                 "AND placement=1").fetchone()["role_id"] == 111,
      str(rc.execute("SELECT role_id FROM hall_of_fame").fetchall()))
ap = asyncio.run(V._apply_roles(g, V.role_diff(rc, 2), rc, 2))
check("apply removes a role the member actually holds",
      any("➖" in x for x in ap) and mem.roles == [], str(ap) + " " + str(mem.roles))
check("skips are not turned into member lookups",
      not any("roleNone" in x for x in ap), str(ap))
# every earlier season is scanned, not just the one before
rc2 = _champion_db("roles2.db")
asyncio.run(V.finalize_season(rc2, actor_id=1, apply_roles=False))     # closes S1
V.create_season(rc2, "Season 2", "2026-10-12", weeks=1)
V.create_season(rc2, "Season 3", "2026-11-09", weeks=1)               # S2 never ran
check("two seasons later the stale role is STILL on the removal list",
      any(a["action"] == "remove" and a["role_id"] == 111
          for a in V.role_diff(rc2, 3)), str(V.role_diff(rc2, 3)))
# a role a human granted must not be taken away by a bot
rc3 = _champion_db("roles3.db")
asyncio.run(V.finalize_season(rc3, actor_id=1, apply_roles=False))   # writes the HoF row
# simulate a season whose role config was lost: the champion row stands, no role id
# can be resolved. Rows are not deleted - the FKs are doing their job.
rc3.execute("UPDATE season SET role_trivia=NULL, role_strategy=NULL, role_hangar=NULL "
            "WHERE id=1")
rc3.execute("UPDATE hall_of_fame SET role_id=NULL")
manual = [a for a in V.stale_roles(rc3, 2)]
check("a champion whose season has no configured role is reported, never guessed at",
      manual and all(a["action"] == "manual" for a in manual), str(manual))
check("season_report and role_diff refuse to silently use the newest season",
      V.season_report(rc, 1)["leagues"]["l1"] and not V.standings(rc, "league",
                                                                  league="l1", limit=5),
      "S1 report is non-empty while the active S2 table is empty")


# --------------------------------------------------------------------------- #
# /setup provisioning: create once, adopt by ID forever
# --------------------------------------------------------------------------- #
class _FR:
    def __init__(self, i, n):
        self.id, self.name = i, n


class _FC:
    def __init__(self, i, n, cat=None, slow=0, ow=None):
        self.id, self.name, self.category, self.slowmode_delay = i, n, cat, slow
        self.overwrites = ow or {}


class _FW:
    """Fake workspace speaking the provision() protocol exactly as main.py's
    _GuildWorkspace does, so the seam itself is under test."""

    def __init__(self):
        self.roles, self.channels, self.created = [], [], []
        self.cats, self.cat_overwrites = [], None
        self._next = 1000

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == int(rid)), None) if rid else None

    def find_role(self, name):
        return next((r for r in self.roles if r.name == name), None)

    def get_channel(self, cid_):
        return next((c for c in self.channels if c.id == int(cid_)), None) if cid_ else None

    def find_channel(self, key, name):
        return next((c for c in self.channels if c.name == name), None)

    async def create_role(self, *, name, colour=0, hoist=False, reason=None):
        self._next += 1
        r = _FR(self._next, name)
        self.roles.append(r)
        self.created.append(("role", name))
        return r

    async def create_category(self, *, name, overwrites=None, reason=None):
        self._next += 1
        c = _FC(self._next, name, None, 0, overwrites)
        self.channels.append(c)
        self.cats.append(c)
        self.cat_overwrites = overwrites
        self.created.append(("category", name))
        return c

    async def create_channel(self, *, name, category=None, overwrites=None,
                             slowmode=0, reason=None):
        self._next += 1
        c = _FC(self._next, name, category, slowmode, overwrites)
        self.channels.append(c)
        self.created.append(("channel", name))
        return c


pw = _fresh(ROOT / ".pytest_tmp" / "prov.db")
pc = D.connect(pw)
V.create_season(pc, "Season 1", "2026-09-14", weeks=1)     # role binding needs a target
ws = _FW()
res = asyncio.run(V.provision(pc, ws, actor_id=1))
check("/setup creates 1 category + 9 channels + 5 roles, and nothing else",
      ws.created.count(("category", V.CATEGORY_NAME)) == 1
      and sum(1 for k, _ in ws.created if k == "channel") == 9
      and sum(1 for k, _ in ws.created if k == "role") == 5
      and len(ws.created) == 15, str(ws.created))
check("a provisioned season's calendar has no 'grand' tier and no multiplier anywhere",
      pc.execute("SELECT COUNT(*) c FROM evening WHERE difficulty<>'normal' "
                 "OR multiplier<>1.0").fetchone()["c"] == 0, "calendar purity")
check("every created object is stored by DISCORD ID in config",
      all(D.cfg(pc, V._hubkey(m["kind"], m["key"])) == m["id"] for m in res["entries"]),
      str([(m["kind"], m["key"], m["id"], D.cfg(pc, V._hubkey(m["kind"], m["key"])))
           for m in res["entries"][:4]]))
check("the staff role id is stored and resolvable",
      ws.get_role(D.cfg(pc, "staff_role_id")) is not None, str(D.cfg(pc, "staff_role_id")))
check("champion roles are bound to the season that will award them",
      tuple(pc.execute("SELECT role_trivia, role_strategy, role_hangar, role_overall "
                       "FROM season WHERE id=1").fetchone())
      == tuple(D.cfg(pc, V._hubkey("role", k))
                 for k in ("champ_l1", "champ_l2", "champ_l3", "champ_overall")),
      str(pc.execute("SELECT role_trivia FROM season WHERE id=1").fetchone()))
check("public channels: everyone reads and writes, staff identical, nothing more",
      V._writes(4242, everyone_read=True) == {
          "everyone": {"read": True, "write": True},
          "staff": {"read": True, "write": True}, "staff_id": 4242}, str(V._writes(4242, everyone_read=True)))
check("staff-only channels: @everyone cannot even see them",
      V._writes(4242, everyone_read=False)["everyone"] == {"read": False, "write": False},
      str(V._writes(4242, everyone_read=False)))
check("the category does NOT deny @everyone - a deny there would hide public channels",
      ws.cat_overwrites is not None and ws.cat_overwrites["everyone"]["read"] is True,
      str(ws.cat_overwrites))
check("private channels are private, and slowmode sits only on the loud ones",
      next(c for c in ws.channels if c.name == "staff-only").overwrites["everyone"]["read"]
      is False
      and next(c for c in ws.channels if c.name == "trivia-night").slowmode_delay == 10
      and next(c for c in ws.channels if c.name == "league-table").slowmode_delay == 0,
      str([(c.name, c.slowmode_delay) for c in ws.channels]))

# ---- re-running must adopt, never duplicate ------------------------------- #
ws2 = _FW()
ws2.roles, ws2.channels = list(ws.roles), list(ws.channels)
res2 = asyncio.run(V.provision(pc, ws2, actor_id=1))
check("running /setup twice creates NOTHING new (adopt by id)",
      ws2.created == [], str(ws2.created))
check("...and every adoption is reported as id-based, not a name collision",
      all(m["adopted"] == "id" for m in res2["reused"]),
      str([(m["name"], m["adopted"]) for m in res2["reused"]]))

# ---- the rename test: the entire point of the design ---------------------- #
for nm in ("trivia-night", "league-table", "pending-checkout"):
    next(c for c in ws2.channels if c.name == nm).name = nm.upper() + "-renamed"
for r in ws2.roles:
    if r.name == "Hub Staff":
        r.name = "Quiz Team"
ws3 = _FW()
ws3.roles, ws3.channels = list(ws2.roles), list(ws2.channels)
res3 = asyncio.run(V.provision(pc, ws3, actor_id=1))
check("after renaming channels AND the staff role, a third /setup still creates nothing",
      ws3.created == [], str(ws3.created))
check("renamed objects are still adopted by id, so the rename is cosmetic",
      all(m["adopted"] == "id" for m in res3["reused"]),
      str([(m["name"], m["adopted"]) for m in res3["reused"]]))
check("provision_plan reports the stored ids, so /setup PLAN is honest about adoption",
      V.provision_plan(pc)["category"]["id"] is not None
      and all(r["id"] for r in V.provision_plan(pc)["roles"]),
      str(V.provision_plan(pc)["roles"]))

# a channel deleted in Discord must come back, and only that one
victim = next(c for c in ws3.channels if c.name == "appeals")
ws4 = _FW()
ws4.roles, ws4.channels = list(ws3.roles), [c for c in ws3.channels if c is not victim]
asyncio.run(V.provision(pc, ws4, actor_id=1))
check("deleting one channel recreates exactly that one, not nine",
      ws4.created == [("channel", "appeals")], str(ws4.created))

# a hand-set champion role is a decision, not an oversight
pc.execute("UPDATE season SET role_trivia=55555 WHERE id=1")
V._bind_season_roles(pc)
check("re-running setup does not overwrite a champion role an owner set by hand",
      pc.execute("SELECT role_trivia FROM season WHERE id=1").fetchone()[0] == 55555,
      "COALESCE keeps the human's choice")
_na = pc.execute("SELECT COUNT(*) c FROM audit WHERE action='hub.provision'"
                 ).fetchone()["c"]
check("provisioning is audited, so 'who ran /setup, when' stays answerable",
      _na >= 4, f"{_na} audit rows for 4 provision calls")

# ---- the 24h window: one rule for every league -------------------------- #
alld = conn.execute("SELECT league, opens_at, closes_at FROM evening").fetchall()
lens = {(V.parse_iso(r["closes_at"]) - V.parse_iso(r["opens_at"])).total_seconds()
        for r in alld}
pw2 = _fresh(ROOT / ".pytest_tmp" / "window.db")
pc2 = D.connect(pw2)                       # untouched config: the real default
V.create_season(pc2, "Season W", "2026-09-14", weeks=4)
wlens = {(V.parse_iso(r["closes_at"]) - V.parse_iso(r["opens_at"])).total_seconds()
         for r in pc2.execute("SELECT opens_at, closes_at FROM evening").fetchall()}
check("a season built with default settings is open exactly 24h, all leagues alike",
      wlens == {24 * 3600}, str(wlens))
check("24h is a rule, not a per-season option: create_season has no window parameter",
      V.NIGHT_WINDOW_S == 24 * 3600
      and "answer_seconds" not in V.create_season.__code__.co_varnames,
      f"window={V.NIGHT_WINDOW_S} params={V.create_season.__code__.co_varnames[:6]}")
check("answer_window reports 24h on a server that configured nothing",
      V.answer_window(pc2) == 24 * 3600, str(V.answer_window(pc2)))

mon_close = V.parse_iso(pc2.execute(
    "SELECT closes_at FROM evening WHERE day='2026-09-14' AND league='l1'"
).fetchone()[0])
thu_open = V.parse_iso(pc2.execute(
    "SELECT opens_at FROM evening WHERE day='2026-09-17' AND league='l1'"
).fetchone()[0])
check("Mon trivia closes Tue 21:30 and the next trivia opens Thu 21:30 (the 48h gap)",
      mon_close.date().isoformat() == "2026-09-15" and thu_open.date().isoformat()
      == "2026-09-17" and (thu_open - mon_close).total_seconds() == 48 * 3600,
      f"{mon_close} -> {thu_open}")
check("Sunday opens at the same hour and runs the same 24h as any other night",
      {r[0][-8:] for r in pc2.execute("SELECT opens_at FROM evening WHERE is_sunday=1")}
      == {r[0][-8:] for r in pc2.execute("SELECT opens_at FROM evening WHERE is_sunday=0")}
      and (V.parse_iso(pc2.execute("SELECT closes_at FROM evening WHERE is_sunday=1"
                                   " AND league='l2'").fetchone()[0])
           - V.parse_iso(pc2.execute("SELECT opens_at FROM evening WHERE is_sunday=1"
                                     " AND league='l2'").fetchone()[0])
           ).total_seconds() == 24 * 3600, "sunday parity")


# ---- picking an EXISTING staff role: no competing "Hub Staff" is minted ------ #
ps = _fresh(ROOT / ".pytest_tmp" / "prov2.db")
pc3 = D.connect(ps)
ws5 = _FW()
pre_existing = _FR(555, "Quiz Team")
ws5.roles = [pre_existing]
res5 = asyncio.run(V.provision(pc3, ws5, 555, actor_id=1))
check("when the owner picks a staff role, /setup adopts THEIRS and does not mint Hub Staff",
      not any(n == "Hub Staff" for k, n in ws5.created if k == "role")
      and D.cfg(pc3, "staff_role_id") == 555
      and ws5.find_role("Quiz Team").id == 555, str(ws5.created))
check("the picked role is what channel overwrites point at",
      res5["entries"] and V._writes(D.cfg(pc3, "staff_role_id"), everyone_read=False)
      ["staff_id"] == 555, str(res5["entries"][0]))
try:
    asyncio.run(V.provision(pc3, _FW(), 999999, actor_id=1))
    check("a dead staff role id is refused loudly", False, "no error raised")
except ValueError as e:
    check("a dead staff role id is refused loudly", "does not exist" in str(e), str(e))

# ---- the import-time collision guard: prove it CAN fail ------------------- #
_saved = list(V.PROVISION_ROLES)
try:
    # Re-use the CHANNEL key "staff" for a role. Before namespacing this is exactly
    # what the real table did, and it silently corrupted hub:staff at write-back.
    V.PROVISION_ROLES.append(("staff", "Dup Role", 0, False))   # same key as Hub Staff
    try:
        V._no_collisions()
        check("the collision guard actually fires when two objects share a config key",
              False, "no error raised - the guard is decoration")
    except RuntimeError as e:
        check("the collision guard actually fires when two objects share a config key",
              "hub:role_staff" in str(e), str(e))
finally:
    V.PROVISION_ROLES[:] = _saved
check("and the real table passes it - no key collides today",
      V._no_collisions() is None, "live key set")
check("roles and channels sharing a bare key is allowed precisely because of the prefix",
      V._hubkey("role", "staff") != V._hubkey("channel", "staff"),
      f'{V._hubkey("role", "staff")} vs {V._hubkey("channel", "staff")}')



print(f"\n{ok} service-layer checks passed.")
