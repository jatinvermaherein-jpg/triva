"""Rotating-week fairness model, run against the REAL engine.
  Mon/Thu -> L1   Tue/Fri -> L2   Wed/Sat -> L3   Sun -> all 3 leagues together
Each league has 8 weekday nights + 4 Sundays = 12 scoring nights, EQUAL.
Grinders attend more than casuals; that differential is what we are testing for.

v4.1: Sunday used to carry a 1.5x multiplier. It does not any more, and the number
here is READ FROM engine/scoring.py rather than hard-coded - a fairness model that
invents its own constants is how you end up citing a table that the bot does not
implement. Same trick means this file cannot drift again if the rule changes back.
"""
import importlib.util, pathlib, random, statistics, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sp = importlib.util.spec_from_file_location("scoring", ROOT/"engine"/"scoring.py")
S = importlib.util.module_from_spec(sp); sys.modules["scoring"]=S; sp.loader.exec_module(S)

SUN_MULT = getattr(S, "SUNDAY_MULTIPLIER", 1.0)   # absent in v4.1 => 1.0
LEAGUE_SEED = {"l1": 0, "l2": 5, "l3": 11}
N, REPS = 180, 48
NEED = {"easy":.30,"medium":.48,"hard":.66}
players = list(range(N))
r0 = random.Random(7)
skill = {p: min(.97,max(.03,r0.gauss(.55,.17))) for p in players}
typer = {p: r0.gauss(0,1) for p in players}
# three participation strata, realistic for a 200-person community
tier = [2 if p % 12 == 0 else (1 if p % 3 == 0 else 0) for p in players]
ATT_W = [.20, .55, .85]      # weekday league night
ATT_S = [.35, .75, .95]      # Sunday grand (the main event pulls people in)
core = {p for p, t in zip(players, tier) if t == 2}

# 4 weeks; each league gets Mon/Thu (or Tue/Fri, Wed/Sat) + every Sunday
LEAGUE_DAYS = {"l1": [(0, "Mon"), (3, "Thu")],
               "l2": [(1, "Tue"), (4, "Fri")],
               "l3": [(2, "Wed"), (5, "Sat")]}

def night_score(n, p, sun):
    """Deterministic per (night, player): what a human would call their result."""
    # Sunday's HARDER BANK is an authoring choice (staff write 4 meaner questions),
    # not a reward tier: a bigger number on one weekday would be a calendar effect
    # on the title race, which is exactly what v4.1 removed.
    tiers = (["medium","hard","hard","medium"] if sun else ["easy","medium","hard"])
    tot = 0
    for k, t in enumerate(tiers):
        if skill[p] < NEED[t]:
            continue
        rng = random.Random(n*7919 + p*131 + k*7 + (1000 if sun else 0))
        think = max(1., 42.*(1.15-skill[p])**2.1*rng.lognormvariate(0,.42))
        tt = max(.4, think + max(0., 6.*rng.gauss(1.,.30) - typer[p]*2.2))
        tot += int(S.league1_raw(t, tt)*(SUN_MULT if sun else 1.) + .5)
    return tot

FIXED = {}
for w in range(4):
    for league, days in LEAGUE_DAYS.items():
        for di, dname in days:
            n = w*7 + di
            for p in players: FIXED[(league, n, p)] = night_score(n, p, False)
        for p in players: FIXED[(league, w*7+6, p)] = night_score(w*7+6, p, True)

NIGHTS_PER_LEAGUE = 12   # 8 weekday + 4 sunday

def totals(league, floor, keep, seed):
    rnd = random.Random(seed)
    out = {}
    for p in players:
        played = []
        for w in range(4):
            for di, _d in LEAGUE_DAYS[league]:
                n = w*7 + di
                if rnd.random() < ATT_W[tier[p]]: played.append(FIXED[(league,n,p)])
            if rnd.random() < ATT_S[tier[p]]:
                played.append(FIXED[(league, w*7+6, p)])
        if len(played) < floor: continue
        v = sorted(played, reverse=True)
        out[p] = sum(v[:keep]) if keep else sum(played)
    return out

def spearman(tot):
    ks = sorted(tot, key=lambda p:-skill[p]); idx = {p:i+1 for i,p in enumerate(ks)}
    order = sorted(tot, key=lambda p:-tot[p]); n = len(order)
    if n < 3: return 0.
    d = sum((i+1-idx[p])**2 for i,p in enumerate(order))
    return 1 - 6*d/(n*(n*n-1))

print(f"nights per league: {NIGHTS_PER_LEAGUE} (8 weekday + 4 Sunday grands)\n")
print(f"{'league rule':>22} | {'field':>6} | {'champ flip':>11} | {'top5 ov':>8} | {'grinder seats in top10':>25} | {'rho':>6}")
rows = []
for name, floor, keep in (("count all 12",0,None),("count all + floor 5",5,None),
                          ("best 10 of 12",0,10),("best 8 of 12",0,8),("best 6 of 12",0,6),
                          ("best 5 of 12",0,5),("best 4 of 12",0,4),
                          ("best 8 + floor 5",5,8),("best 6 + floor 5",5,6)):
    ref = totals("l1", floor, keep, 1)
    rr = sorted(ref, key=lambda p:(-ref[p],p))
    flips, ovs, gs, fields, rhos = [], [], [], [], []
    for sd in range(REPS):
        for lg in ("l1","l2","l3"):
            # NOT hash(lg): PYTHONHASHSEED randomises string hashes per process, so
            # the old `hash(lg)%13` re-rolled every league's seed on each run and the
            # printed table could not be reproduced. A literal map is stable.
            t = totals(lg, floor, keep, 500+sd*7+LEAGUE_SEED[lg])
            r = sorted(t, key=lambda p:(-t[p],p))
            if len(r) < 10: continue
            flips.append(r[0]!=rr[0])
            ovs.append(len(set(r[:5])&set(rr[:5]))/5)
            gs.append(len(set(r[:10])&core)/10)
            fields.append(len(t)); rhos.append(spearman(t))
    print(f"{name:>22} | {statistics.fmean(fields):>6.0f} | {100*statistics.fmean(flips):>10.0f}% | "
          f"{100*statistics.fmean(ovs):>7.0f}% | {100*statistics.fmean(gs):>24.0f}% | "
          f"{statistics.fmean(rhos):>6.3f}")
    rows.append((name, statistics.fmean(flips), statistics.fmean(gs), statistics.fmean(rhos)))

best = min(rows, key=lambda r: (0.6 if r[2] > 0.30 else 0) + r[1])
print(f"\nGrinders are {len(core)}/{N} players ({100*len(core)/N:.0f}% of the club).")
print(f"Even attendance alone therefore predicts ~{100*len(core)/N:.0f}% of top-10 seats.")
for name, fl, gs, rh in rows:
    verdict = "GRIND-DOMINATED" if gs > 0.55 else ("balanced" if gs < 0.35 else "mixed")
    print(f"  {name:>22}: grinder seats {100*gs:>3.0f}%  flip {100*fl:>3.0f}%  rho {rh:.3f}  -> {verdict}")
