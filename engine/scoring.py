"""The Hub Knowledge Season — scoring engine.

Pure, deterministic, no Discord imports, no I/O. This module IS the ruleset.
If a number is not computable here, it is not part of the competition.

Design rule (agreed for this server): humans author questions and award every
point. The bot never judges. This engine only guarantees that the points humans
award are combined, rounded, tie-broken and rolled over identically every time.

FINAL SCORING ORDER (memorise this, it answers 90% of appeals):
    raw -> penalties -> Sunday x1.5 -> bonuses -> round half-up -> clamp >= 0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------- #
# Constants — the published knobs. Changing one = a patch note.
# --------------------------------------------------------------------------- #

L1_TIER_POINTS = {          # League 1 raw difficulty value, before speed bonus
    "easy": 3,
    "medium": 6,
    "hard": 8,              # Grand Challenge / knockout questions only
    "instant": 1,           # 5-second "first to type" tie-break, no speed bonus
}

SPEED_BONUS_MAX = 2.0
SPEED_BONUS_HALFLIFE_S = 40.0   # bonus halves every 40 seconds
SPEED_BONUS_CUTOFF_S = 180.0    # after 3 minutes the speed bonus is exactly 0
FIRST_CORRECT_BONUS = 1.0

# v4.1: Sunday used to carry a 1.5x "Grand" multiplier plus tie-break and
# participation bonuses. The owner's call: Sunday is simply the day all three
# leagues run together, so it pays exactly what any other night pays. The columns
# evening.difficulty / evening.multiplier stay (existing DBs have them) but are
# always normal/1.0, and NO code path reads a Sunday bonus any more.
DROP_LOWEST_COUNT = 0     # measured: with 12 nights/league, dropping nights DOUBLES
                          # champion churn (44% -> 100%). Count every night. See sim/.
STANDINGS_NIGHT_FLOOR = 5 # nights a player must play to hold a league podium spot;
                          # lifts rank-vs-knowledge rho 0.730 -> 0.857


DISQUALIFY_MULTIPLIER = 0.0     # edited answer -> 0 (host may override to 0.5)
EDIT_OVERRIDE_MULTIPLIER = 0.5
DUPLICATE_PENALTY_MULTIPLIER = 0.0


class League(str, Enum):
    KNOWLEDGE = "knowledge"    # League 1 - Knowledge
    STRATEGY = "strategy"      # League 2 - Strategy
    HANGAR = "hangar"          # League 3 - Hangar


LEAGUE_LABEL = {
    League.KNOWLEDGE: "League 1 - Knowledge",
    League.STRATEGY: "League 2 - Strategy",
    League.HANGAR: "League 3 - Hangar",
}

LEAGUE_ROLE = {
    League.KNOWLEDGE: "Mech Arena Trivia Champion",
    League.STRATEGY: "Strategy Master",
    League.HANGAR: "Best Advisor",
}

RUBRIC_CRITERIA = {
    League.STRATEGY: ("creativity", "practicality", "coordination",
                      "objective_control", "counterplay"),
    League.HANGAR: ("accuracy", "logic", "resource_efficiency",
                    "build_quality", "long_term_value"),
}

BAND_THRESHOLDS = ((21, "Excellent"), (13, "Good"), (6, "Average"), (0, "Poor"))
BAND_POINTS = {"Excellent": 25, "Good": 15, "Average": 8, "Poor": 2}


def rubric_for(league: League) -> tuple[str, ...]:
    return RUBRIC_CRITERIA[League(league)]


# --------------------------------------------------------------------------- #
# Core data
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EventResult:
    """One player's outcome for one event. `raw` is what a human awarded."""

    player_id: int
    season_id: int
    event_id: str
    week: int
    league: League
    raw: float
    is_correct: bool = True
    band: str | None = None
    disqualified: bool = False
    is_no_show: bool = False
    edited: bool = False
    duplicate_of: int | None = None

    @property
    def droppable(self) -> bool:
        """Only absences may be dropped. An honest wrong answer is a result,
        not an absence - dropping it would reward players for not trying."""
        return self.is_no_show or self.disqualified

    @property
    def final(self) -> int:
        return event_points(self)


@dataclass
class Standing:
    player_id: int
    points: int = 0
    events: int = 0
    best_event: int = 0
    firsts: int = 0
    band_counts: dict[str, int] = field(default_factory=dict)
    dropped_event: str | None = None
    dropped_points: int = 0
    droppable_absences: int = 0


# --------------------------------------------------------------------------- #
# League 1
# --------------------------------------------------------------------------- #

def speed_bonus(seconds_to_answer: float | None) -> float:
    """Continuous decay instead of a cliff after 'first correct'. A 3-second
    answer and a 9-second answer should not be 1 point apart."""
    if seconds_to_answer is None:
        return 0.0
    t = max(0.0, float(seconds_to_answer))
    if t >= SPEED_BONUS_CUTOFF_S:
        return 0.0
    return SPEED_BONUS_MAX * (0.5 ** (t / SPEED_BONUS_HALFLIFE_S))


def league1_raw(tier: str, seconds_to_answer: float | None = None,
                is_correct: bool = True, first_correct: bool = False) -> float:
    if not is_correct:
        return 0.0
    key = tier.lower().strip()
    if key not in L1_TIER_POINTS:
        raise ValueError(f"unknown difficulty tier {tier!r}")
    raw = float(L1_TIER_POINTS[key])
    if key != "instant":
        raw += speed_bonus(seconds_to_answer)
        if first_correct:
            raw += FIRST_CORRECT_BONUS
    return raw


# --------------------------------------------------------------------------- #
# Leagues 2 / 3
# --------------------------------------------------------------------------- #

def band_from_rubric(league: League, scores: dict[str, int]) -> tuple[int, str]:
    """Total the five 0-5 criteria and map to a published band.

    Judges score criteria, never points. The band is derived, so nobody can
    award 17 and start a war over it.
    """
    criteria = rubric_for(league)
    unknown = set(scores) - set(criteria)
    if unknown:
        raise ValueError(f"unknown criteria for {league.value}: {sorted(unknown)}")
    if len(scores) != len(criteria):
        missing = sorted(set(criteria) - set(scores))
        raise ValueError(f"missing rubric criteria: {missing}")
    for name, value in scores.items():
        if not 0 <= int(value) <= 5:
            raise ValueError(f"criterion {name} out of range 0-5: {value}")
    total = sum(int(v) for v in scores.values())
    for threshold, band in BAND_THRESHOLDS:
        if total >= threshold:
            return BAND_POINTS[band], band
    raise AssertionError("unreachable")


def rubric_total(league: League, scores: dict[str, int]) -> int:
    return sum(int(v) for v in scores.values())


def merge_judges(judge_totals: list[float], third_trigger: float = 6.0,
                 third_score: float | None = None) -> tuple[float, bool]:
    """Average independent judge totals.

    Returns (raw_before_rounding, escalated_to_third_judge).
    Two judges further apart than `third_trigger` -> a third is mandatory.
    """
    if not judge_totals:
        raise ValueError("no judge scores supplied")
    if len(judge_totals) >= 3:
        return sum(judge_totals) / len(judge_totals), False
    spread = max(judge_totals) - min(judge_totals)
    if spread > third_trigger:
        if third_score is None:
            raise ValueError(
                f"judges split by {spread:.0f} pts (> {third_trigger:.0f}) - "
                "a third judge is required before this entry can be scored"
            )
        vals = [judge_totals[0], judge_totals[1], float(third_score)]
        return sum(vals) / len(vals), True
    return sum(judge_totals) / len(judge_totals), False


# --------------------------------------------------------------------------- #
# Event -> season
# --------------------------------------------------------------------------- #

def event_points(result: EventResult) -> int:
    """raw -> penalties -> round half-up. Nothing else.

    Every night is scored the same way whatever day it falls on: a night's value is
    the points staff entered for it, and the only multipliers left are the ones that
    punish a real problem (disqualification, a late edit, a duplicate).""",
    raw = float(result.raw)
    if result.disqualified:
        raw *= DISQUALIFY_MULTIPLIER
    elif result.edited:
        raw *= EDIT_OVERRIDE_MULTIPLIER
    elif result.duplicate_of is not None:
        raw *= DUPLICATE_PENALTY_MULTIPLIER
    elif not result.is_correct:
        raw = 0.0

    return max(0, int(raw + 0.5))


def standings(results: list[EventResult], drop_lowest: int = DROP_LOWEST_COUNT,
              firsts_by_player: dict[int, int] | None = None,
              keep_best: int | None = None,
              min_nights: int = 0) -> list[Standing]:
    """Standings with absence forgiveness, then the published tie-break.

    Two ways to forgive an absence - never both:
      drop_lowest - drops absences only (a wrong answer is still a result)
      keep_best   - counts only a player's N best nights (league format)
    """
    by_player: dict[int, list[EventResult]] = {}
    for r in results:
        by_player.setdefault(r.player_id, []).append(r)

    out: list[Standing] = []
    for pid, rs in by_player.items():
        st = Standing(player_id=pid, events=len(rs))
        droppable = sorted((r for r in rs if r.droppable), key=lambda r: (-r.final, r.event_id))
        drop = droppable[-1] if drop_lowest > 0 and droppable else None
        del droppable
        ordered = sorted(rs, key=lambda r: (-r.final, r.event_id))
        if drop is not None:
            st.dropped_event, st.dropped_points = drop.event_id, drop.final
            st.droppable_absences += 1
        counted = ordered
        if keep_best is not None and len(ordered) > keep_best:
            counted, dropped_extra = ordered[:keep_best], ordered[keep_best:]
            if drop is None and dropped_extra:
                drop = dropped_extra[-1]
                st.dropped_event, st.dropped_points = drop.event_id, drop.final
        for r in counted:
            st.best_event = max(st.best_event, r.final)
            if r is drop:
                continue  # drop exactly one absence
            st.points += r.final
            if r.band:
                st.band_counts[r.band] = st.band_counts.get(r.band, 0) + 1
        st.points = max(0, st.points)
        st.firsts = (firsts_by_player or {}).get(pid, 0)
        out.append(st)

    out.sort(key=lambda s: (-s.points, -s.best_event, -s.firsts, -s.events, s.player_id))
    if min_nights:
        # A qualification floor, not a punishment: it is the single cheapest
        # fairness lever in the whole format (rho 0.730 -> 0.857) and it does not
        # remove grinders' coins - only their podium claim.
        out = [s for s in out if s.events >= min_nights]
    return out


def podium_firsts(results: list[EventResult]) -> dict[int, int]:
    """Count of event wins per player (used by tie-break #3)."""
    by_event: dict[str, list[EventResult]] = {}
    for r in results:
        by_event.setdefault(r.event_id, []).append(r)
    firsts: dict[int, int] = {}
    for rs in by_event.values():
        if not rs:
            continue
        best = max(r.final for r in rs)
        if best <= 0:
            continue
        for r in sorted(rs, key=lambda x: (x.final, x.event_id), reverse=True):
            if r.final == best:
                firsts[r.player_id] = firsts.get(r.player_id, 0) + 1
    return firsts


def season_report(results: list[EventResult]) -> dict:
    """Everything the end-of-season post needs, computed once and auditable."""
    firsts = podium_firsts(results)
    table = standings(results, firsts_by_player=firsts)
    by_league: dict[League, list[Standing]] = {}
    for league in League:
        rs = [r for r in results if r.league is league]
        by_league[league] = standings(rs, firsts_by_player=podium_firsts(rs))
    return {
        "season": table,
        "leagues": by_league,
        "events": len({r.event_id for r in results}),
        "players": len({r.player_id for r in results}),
    }


def rollover(results: list[EventResult], coins_per_point: int) -> dict:
    """Season end. Points reset; coins and Hall of Fame never do."""
    ledger = []
    for r in results:
        pts = r.final
        if pts <= 0:
            continue
        ledger.append({
            "player_id": r.player_id, "event_id": r.event_id,
            "points": pts, "coins": pts * coins_per_point,
        })
    return {
        "entries": ledger,
        "total_points": sum(e["points"] for e in ledger),
        "total_coins": sum(e["coins"] for e in ledger),
        "reset": ["league_points", "standings", "win_streaks"],
        "kept": ["lifetime_coins", "hall_of_fame", "badges", "audit_log"],
    }


def point_capacity(events: list[dict]) -> dict:
    """Max bankable points in a season. If this runs away, the prize budget is
    wrong - fix the budget, not the players."""
    per_event = []
    for ev in events:
        lg = ev.get("league") or next(
            (x for x in ev["id"].split(":") if x in {m.value for m in League}), "knowledge")
        league = League(lg)
        if league is League.KNOWLEDGE:
            v = float(max(v for k, v in L1_TIER_POINTS.items() if k != "instant"))
            v += SPEED_BONUS_MAX + FIRST_CORRECT_BONUS
        else:
            v = 25.0
        per_event.append(int(v + 0.5))
    ranked = sorted(per_event, reverse=True)
    keep = ranked[: max(0, len(ranked) - DROP_LOWEST_COUNT)]
    return {"events": len(events), "theoretical_max": sum(keep), "per_event_max": per_event}
