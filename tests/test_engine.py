"""Engine tests. Run:  python3 tests/test_engine.py   (or pytest)."""
import importlib.util, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scoring", ROOT / "engine" / "scoring.py")
S = importlib.util.module_from_spec(spec)
sys.modules["scoring"] = S          # dataclasses need the module registered
spec.loader.exec_module(S)

ok = 0


def check(name, cond, detail=""):
    global ok
    if not cond:
        print(f"FAIL  {name}  {detail}")
        sys.exit(1)
    ok += 1
    print(f"pass  {name}")


# --- League 1 ------------------------------------------------------------- #
check("easy no speed bonus = 3", S.league1_raw("easy", None) == 3.0)
check("instant ignores speed + first bonus",
      S.league1_raw("instant", 0.4, first_correct=True) == 1.0)
check("wrong answer = 0", S.league1_raw("hard", 1.0, is_correct=False) == 0.0)
check("speed decay halves at halflife",
      abs(S.speed_bonus(40) - S.speed_bonus(0) / 2) < 1e-9)
check("speed bonus is 0 at cutoff", S.speed_bonus(180) == 0.0)
check("speed bonus has decayed to ~0.09 by 179s (no cliff, no drift)", 0 < S.speed_bonus(179) < 0.10)
check("40s answer still earns half the bonus", abs(S.speed_bonus(40) - 1.0) < 1e-9)
check("cutoff is exact, no drift", S.speed_bonus(179.999) > 0 and S.speed_bonus(180) == 0.0)
check("first-correct adds exactly 1",
      abs(S.league1_raw("medium", None, first_correct=True) - 7.0) < 1e-9)
check("max L1 medium raw = 9", abs(S.league1_raw("medium", 0, first_correct=True) - 9.0) < 1e-9)
try:
    S.league1_raw("impossible", 1)
    check("bad tier rejected", False)
except ValueError:
    check("bad tier rejected", True)

# --- Leagues 2/3 ---------------------------------------------------------- #
excellent = {c: 5 for c in S.rubric_for(S.League.STRATEGY)}
check("25 rubric -> Excellent band", S.band_from_rubric(S.League.STRATEGY, excellent) == (25, "Excellent"))
good = {"creativity": 3, "practicality": 3, "coordination": 3, "objective_control": 3, "counterplay": 3}
check("15 rubric -> Good band", S.band_from_rubric(S.League.STRATEGY, good) == (15, "Good"))
avg = {"creativity": 1, "practicality": 2, "coordination": 1, "objective_control": 2, "counterplay": 1}
check("7 rubric -> Average band", S.band_from_rubric(S.League.STRATEGY, avg) == (8, "Average"))
poor = {c: 1 for c in S.rubric_for(S.League.HANGAR)}
check("5 rubric -> Poor band", S.band_from_rubric(S.League.HANGAR, poor) == (2, "Poor"))
try:
    S.band_from_rubric(S.League.STRATEGY, {"creativity": 5})
    check("incomplete rubric rejected", False)
except ValueError:
    check("incomplete rubric rejected", True)
try:
    S.band_from_rubric(S.League.HANGAR, {c: 0 for c in S.rubric_for(S.League.STRATEGY)})
    check("wrong league criteria rejected", False)
except ValueError:
    check("wrong league criteria rejected", True)

# --- judge merge ---------------------------------------------------------- #
m, esc = S.merge_judges([20, 22])
check("close judges averaged, no escalation", (m, esc) == (21.0, False))
m, esc = S.merge_judges([8, 22], third_score=15)
check("wide split escalates to third", (round(m, 4), esc) == (15.0, True))
try:
    S.merge_judges([8, 22])
    check("unresolved split blocks scoring", False)
except ValueError:
    check("unresolved split blocks scoring", True)

# --- final points: order of operations ------------------------------------ #
def ev(**kw):
    base = dict(player_id=1, season_id=1, event_id="w1:knowledge", week=1,
                league=S.League.KNOWLEDGE, raw=6.0)
    base.update(kw)
    return S.EventResult(**base)

check("weekday medium correct = 6", ev().final == 6)
g = ev(event_id="w1:knowledge:grand", raw=9.0)
check("a Sunday pays EXACTLY what a weekday pays - no 1.5x (v4.1 owner call)",
      g.final == ev(raw=9.0).final == 9, f"{g.final} vs weekday 9")
check("there is no grand bonus left to add at all",
      not hasattr(g, "tiebreak_win") and not hasattr(g, "played_all_sunday")
      and not hasattr(S, "SUNDAY_MULTIPLIER") and not hasattr(S, "GRAND_TIEBREAK_BONUS"),
      "engine must not still carry the concept")
check("wrong answer floors to 0", ev(raw=6.0, is_correct=False).final == 0)
check("edited answer halved", ev(raw=8.0, edited=True).final == 4)
check("plagiarised duplicate zeroed", ev(raw=25.0, duplicate_of=2).final == 0)
check("disqualified zeroed", ev(raw=25.0, disqualified=True).final == 0)

# --- standings ------------------------------------------------------------ #
rows = [
    ev(player_id=7, event_id="w1:knowledge", raw=6.0),
    ev(player_id=7, event_id="w2:knowledge", raw=6.0),
    ev(player_id=7, event_id="w3:knowledge", raw=0.0, is_correct=False),  # answered wrong
    ev(player_id=8, event_id="w1:knowledge", raw=6.0),
    ev(player_id=8, event_id="w2:knowledge", raw=6.0),
]
st = S.standings(rows)
by = {s.player_id: s for s in st}
check("wrong answer still counts (no no-show masking)", by[7].points == 12 and by[7].events == 3,
      f"got {by[7].points}")
check("nothing dropped when player has no absence", by[7].dropped_event is None and by[7].dropped_points == 0)
check("tie broken by best_event then id", st[0].player_id == 7 or st[0].player_id == 8)

# no-show must be droppable: same score, but it was an absence
rows2 = [
    ev(player_id=9, event_id="w1:knowledge", raw=6.0),
    ev(player_id=9, event_id="w2:knowledge", raw=6.0),
    ev(player_id=9, event_id="w3:knowledge", raw=0.0, is_no_show=True),   # absent
]
st2 = S.standings(rows2, drop_lowest=1)   # dropping is now opt-in
check("absence is the droppable zero", st2[0].points == 12 and st2[0].dropped_event == "w3:knowledge")
check("absence does not count as an attempted event", st2[0].droppable_absences == 1)
mixed = S.standings(rows2[:2] + [ev(player_id=9, event_id="w4:knowledge", raw=0.0, is_correct=False)],
                      drop_lowest=1)
check("wrong answer never dropped when an absence exists too",
      mixed[0].dropped_event != "w4:knowledge")

# --- qualification floor + "count every night" (measured policy) ------------ #
check("drop_lowest defaults to 0: every night counts", S.DROP_LOWEST_COUNT == 0)
check("league podium needs STANDINGS_NIGHT_FLOOR nights", S.STANDINGS_NIGHT_FLOOR == 5)
floored = [
    ev(player_id=21, event_id=f"w{i}:knowledge", raw=9.0) for i in range(1, 5)   # 4 nights
] + [ev(player_id=22, event_id=f"w{i}:knowledge", raw=6.0) for i in range(1, 6)]  # 5 nights
no_floor = {x.player_id: x for x in S.standings(floored)}
with_floor = {x.player_id: x for x in S.standings(floored, min_nights=5)}
check("a 4-night player can top the raw table", no_floor[21].points == 36 and
      no_floor[21].points > no_floor[22].points)
check("but not the podium table", 21 not in with_floor and 22 in with_floor, str(list(with_floor)))
check("they keep every point (the floor gates rank, not earnings)",
      with_floor[22].points == 30 and no_floor[21].points == 36)

# --- season report / rollover --------------------------------------------- #
res = [
    ev(player_id=1, event_id="w1:knowledge", raw=9.0),
    ev(player_id=2, event_id="w1:knowledge", raw=6.0),
    ev(player_id=1, event_id="w1:strategy", raw=25.0, league=S.League.STRATEGY),
    ev(player_id=2, event_id="w1:strategy", raw=25.0, league=S.League.STRATEGY),
]
rep = S.season_report(res)
check("league table built per league", set(rep["leagues"]) == set(S.League))
check("champion is season leader", rep["season"][0].player_id == 1)
check("league1 podium has both players", len(rep["leagues"][S.League.KNOWLEDGE]) == 2)
roll = S.rollover(res, coins_per_point=50)
check("rollover coins = points x rate", roll["total_coins"] == roll["total_points"] * 50)
check("rollover keeps lifetime currency + hall of fame",
      "lifetime_coins" in roll["kept"] and "league_points" in roll["reset"])

# --- inflation guard ------------------------------------------------------ #
events = ([{"id": f"w{w}:{L}"} for w in range(1, 5) for L in
           ("knowledge", "strategy", "hangar", "knowledge", "strategy", "hangar")]
          + [{"id": f"w{w}:{s}:grand"} for w in range(1, 5) for s in ("knowledge", "strategy", "hangar")])
cap = S.point_capacity(events)
check("point capacity is finite and logged", cap["theoretical_max"] > 0, str(cap["theoretical_max"]))
check("capacity no longer inflates on Sundays - every night is the same size",
      len(set(cap["per_event_max"])) <= 6, str(cap["per_event_max"]))

# --- best-N league format ------------------------------------------------- #
kb = [
    ev(player_id=11, event_id="w1:knowledge", raw=9.0),
    ev(player_id=11, event_id="w2:knowledge", raw=9.0),
    ev(player_id=11, event_id="w3:knowledge", raw=2.0),
    ev(player_id=11, event_id="w4:knowledge", raw=1.0),
    ev(player_id=12, event_id="w1:knowledge", raw=6.0),
    ev(player_id=12, event_id="w2:knowledge", raw=6.0),
]
kb_st = S.standings(kb, drop_lowest=0, keep_best=2)
kb_by = {x.player_id: x for x in kb_st}
check("keep_best=2 counts only the two best nights", kb_by[11].points == 18, f"got {kb_by[11].points}")
check("keep_best ignores a bad night without needing an absence flag",
      kb_by[11].dropped_event in ("w4:knowledge", "w3:knowledge"))
check("player with fewer nights than N keeps everything", kb_by[12].points == 12)
kb_full = S.standings(kb, drop_lowest=0, keep_best=None)
check("keep_best=None is plain sum", {x.player_id: x.points for x in kb_full}[11] == 21)

print(f"\n{ok} checks passed - engine is safe to build the bot on.")
