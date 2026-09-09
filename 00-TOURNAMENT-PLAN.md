# THE HUB — KNOWLEDGE SEASON
### v4.1 — a rotating week, all three leagues on Sunday, 1-month seasons, every question open 24h.
Bot awards points and roles and builds the whole server on `/setup`; staff pay the coins.

> **v4.1 changes, all of them the owner's call.** (1) Sunday is not a “Grand”: the ×1.5 multiplier and the tier are deleted, `SUNDAY_MULTIPLIER` is gone from the engine. (2) Every question in every league stays open **24 hours** — there is no 60-second L1 window and no 20:00 submission deadline (a league's *next* question is ~48h after the previous one, which is the number that was asked for). (3) No Grand Finalist role, and no role a human has to make: `/setup` creates the channels and the champion roles itself. (4) Every one of those is stored by **Discord id**, so renaming or recolouring anything later is cosmetic.

> **What changed from v3.0.** Three leagues every evening became **one league a night**, with all
> three meeting on Sunday. That single change is the most important thing in this document, and
> §3.3 explains why in numbers. Also changed: the bot no longer touches currency (it queues
> checkouts, a human sends them), League 1's value is entered **per question** instead of fixed
> per tier, and seasons are explicitly **one month**.
>
> The v3 decisions that still stand: buttons and modals over commands, restart-proof panels,
> staff never defend a rubric, L1 is multiple choice.

---

## 1. THE SHAPE OF A WEEK

| | Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|---|
| **League** | 🧠 L1 | ⚔️ L2 | 🔧 L3 | 🧠 L1 | ⚔️ L2 | 🔧 L3 | 🏆 **all three** |
| **Weight** | 1.0× | 1.0× | 1.0× | 1.0× | 1.0× | 1.0× | 1.0× |

**Season = 4 weeks = 12 nights per league.** Every league gets 8 weekday nights and 4 Sundays, so
no league is advantaged by the calendar. That equality is the whole point of the rotation, and
`create_season` raises if a config would break it.

```
16:00   bot posts. Weekday: that league's card(s). Sunday: all three, in their own channels.
16:00   players answer, whenever they like. L1 by button, L2/L3 by modal (one entry, editable
        until close). Every league, every night: the window is the same 24 hours.
next day 16:00   the night closes. Nothing needs a human to be awake for this.
after that       staff: for each L1 card, tap the correct option and, if the question is not
                 worth the tier default, tap 🎯 Points and type the number. ~1 min a card.
                 L2/L3: one band button per entry (POOR/AVERAGE/GOOD/EXCELLENT/CUSTOM).
then            results card posts. Leaderboard card rewrites itself — nobody updates it.
```

**Staff time: about 5–10 minutes a night when you sit down to grade, and none of it is on a
deadline except the one you choose.** Grading is the human step; the 24-hour clock is the bot's.

**Sunday pays exactly what any other night pays.** There is no Grand tier, no 1.5× and no special
role: it is the night all three leagues happen to run together. (Earlier drafts of this plan gave
Sunday ×1.5 and a "Grand" label. The owner cancelled it — a day is not an event, and a multiplier
on one day of the week is a calendar effect on the title race.)

### 1.1 Content load, which is now the easy part
- **League 1:** 3 questions on each of its 12 nights = **~36 per season** (Sunday's count toward
  the same bank).
- **L2 and L3:** 12 prompts each = **24 written prompts per season**, against v3's 56.
- A staff member can now bank a whole season in an afternoon. That is the practical reason the
  rotation survives contact with a real volunteer roster.

---

## 2. WHAT THE BOT OWNS

| Automatic | Rule |
|---|---|
| **Scheduling** | Opens at 16:00 IST, closes 24h later for every league, recovers an evening missed while offline. |
| **L1 grading** | Staff marks the correct option. Bot compares every entry. |
| **L1 arithmetic** | `question_value + speed_bonus(t) + first_correct`, round half-up. No day multiplier anywhere. |
| **L2/L3 arithmetic** | Staff enter one number in the band range; the bot applies the multiplier and clamps. |
| **Leaderboards** | Rebuilt from the ledger after every award. There is no "update leaderboard" button to forget. |
| **Roles** | Season-end champion roles assigned, previous season's removed, always with a dry run first. |
| **Hall of Fame** | Append-only record plus the public post. |
| **Payout queue** | Computes what each player is owed and lists it. It does **not** pay. |

### 2.1 Points are per question, not per tier
`easy 3 · medium 6 · hard 8` is a **suggestion the bot offers**, because a question can turn out
easier or meaner than intended. Staff set the real number per question — 🎯 Points on the grading
row, or in the authoring modal at write-time.

Two rules make this safe:
1. **Staff always enter the base value, the number the question is actually worth.** No day
   multiplies it; the only thing the bot adds on top is the speed bonus it computes itself.
   Two people multiplying by hand in a chat is how a board stops being auditable.
2. **The number is audited with the reason** (`question.points` in `audit`). "Why did that pay 11"
   is the most common appeal, and the answer is a row in the database, not a memory.

### 2.2 The leagues' ranges (unchanged from v3)
- **L2/L3: 0–25.** 0 = nothing usable, 2 = Poor, 8 = Average, 15 = Good, 25 = Excellent.
  Five **band buttons**, not a number box: consistent between staff and 20 seconds a night. Keep
  CUSTOM for the odd case; if a league uses CUSTOM more than 20% of the time the bands are wrong,
  not the staff.
- **L1:** no range — the correct option plus the per-question value.

### 2.3 What "restart proof" means here
1. `View(timeout=None)` — never expires.
2. Hand-written `custom_id`s (`hub:ans:4821:3`), never generated ones.
3. `bot.add_view(cls(conn))` **before** `login`, so a click on a three-day-old panel still routes.
4. **Zero state on the view.** Every callback re-reads the row from SQLite.
5. **Idempotent callbacks.** `UNIQUE` on `award(entry_id)` and on `ledger(evening_id, player_id)`
   mean a double-click inserts nothing.

Point 4 is the one people skip: the panel works, the host restarts, and the pinned panel stops
responding. With state in the DB and ids in the button string, a restart changes nothing a user
can see.

### 2.4 UI rules that are load-bearing
1. **Every panel names the phase in its first line** — `OPEN until 16:01` / `ANSWERS LOCKED` /
   `GRADED · +8 awarded` / `CLOSED`.
2. **One place to click.** The pinned hub card: tonight, standings, Hall of Fame, my stats,
   staff tools.
3. **Staff controls sit on the same messages, gated twice** — `interaction_check` *and* an
   explicit check at the top of each callback, so a bypassed dispatcher cannot let a player grade
   their own night.
4. **Every action answers back**, ephemeral, in one line.
5. **Undo before close, never after.** After the freeze the only answer control is an appeal.
6. **Never `@everyone`.**

### 2.5 No ids, no dates, no slash commands for daily work
Staff never type an evening id, a player id or a channel id. Content is authored by picking a
night from a select and filling a modal; `/season-create` defaults its start day to the next
Monday; `/question-add` and `/scenario-set` open the same picker if you leave them blank. Ids
live in button payloads, where a human never has to read them. This is not polish — a staff
member who has to look up `evening 4821` will eventually grade the wrong night.

---

## 3. SCORING

### 3.1 League 1
```
per question:  round( (question_value + 2.0 × 0.5^(seconds/40) + [1 if first correct]) × [1.5 if Sunday] )
question_value: staff-entered; bot offers easy 3 · medium 6 · hard 8
```
- Speed bonus halves every 40s and is **exactly 0 after 180s**. With a 60-second window it is
  mostly "did you show up", not "how fast do you type".
- `first_correct` +1 goes to the single earliest correct entry; ties by timestamp then lowest
  snowflake. Nothing to argue about.
- Wrong answer = **0 points, not negative**, and still counts as a night played.
- Verified: speed cannot flip a tier (best easy 5.00 < slowest medium 6.00), and across a
  180-player simulation the 15 fastest typists took 0–1% of top-15 seats.

### 3.2 League 2 / 3
`round( staff_points × [1.5 if Sunday] )`. No rubric, no second judge. The trade you accepted is
judge-to-judge consistency; the mitigations that cost nothing are the band buttons and **posting
the awarded number with every result** — public numbers self-police.

### 3.3 Every night counts. This is now measurably the right choice.
Simulated at your shape (180 players, 48 seasons, 12 nights per league, grinders = 8% of the
club with 20/55/85% weekday attendance and 35/75/95% Sunday):

| rule | podium field | champ flips | grinders in top 10 | skill correlation ρ |
|---|---|---|---|---|
| **count all 12** | 180 | 44% | **10%** | 0.730 |
| **count all 12 + 5-night floor** | **77** | 44% | **10%** | **0.857** |
| best 10 of 12 | 180 | 69% | 10% | 0.731 |
| best 8 of 12 | 180 | 91% | 9% | 0.738 |
| best 5 of 12 | 180 | 99% | 9% | 0.769 |

Three findings, all of them reversing a v3 conclusion:
1. **The rotation fixes the grind problem by itself.** Grinders took 10% of top-10 seats against
   an 8% pure-attendance baseline — almost no advantage left to exploit. In v3's 28-night format
   it was 59%.
2. **Drop-lowest / best-N is actively harmful.** Every best-K variant nearly doubles how often
   the champion changes on attendance luck, and best-5 destroys the top-5 overlap. Do not add it.
3. **A 5-night floor is free skill signal.** ρ 0.730 → 0.857 and a 77-person podium, with grind
   share unchanged. It only removes a *podium claim*, never coins — which is why it is on the
   standings table and not on the points.

### 3.4 The Final is now optional
v3 required a top-8 bracket because the table was luck-dominated (99% champ flip). Under the
rotation the same measurement is 44%, so **the table is a defensible champion on its own**. A
Sunday bracket is still a good *event* — a live climax sells week four — but it is no longer
needed to make the result honest. Decision left open; §6.

### 3.5 Tie-breaks
points → best single evening → number of 1st-place evenings → nights played → lowest snowflake.
Deterministic and auditable, no coin flips.

### 3.6 What carries over
- **Reset each season:** season points, standings, streaks, champion roles. The bot removes old
  champion roles itself — otherwise they pile up on profiles and stop meaning anything (§4.2).
- **Never reset:** lifetime points, Hall of Fame, the ledger, cleared payouts.
- The pinned board shows **both**: this month, the three league tables, and lifetime. A monthly
  format must never imply your Season 1 effort disappeared.

---

## 4. ECONOMY — THE BOT DOES NOT RUN IT

Points and roles only. Coins (and XP, if you use it) are sent by a human.

```
season ends → queue_payouts (idempotent) → a pinned channel lists every pending checkout
            → a staff member sends the coins in the server economy
            → they press that row's ✓ button → the row clears and records who did it
```

- One button per payout, coins listed above XP, biggest first, 10 rows per message.
- **A second press cannot double-pay.** The row is status-guarded; the second click is told
  "already cleared".
- Clearing a **coins** row writes one shadow `coin_txn` row so a monthly report can be reconciled
  against whatever bot actually moves money. An **XP** row never touches a coin balance.
- `UNIQUE(season_id, player_id, kind, reason)` means re-running the queue can never create a
  second checkout for the same award.

### 4.1 The prize table (staff fund these; the bot only owes them)
1 point = 1 coin.

| Place | Each league | Season overall |
|---|---|---|
| 🥇 | 3,000 | 12,000 |
| 🥈 | 1,500 | 6,000 |
| 🥉 | 900 | 4,000 |
| 4–5 | 400 | 2,000 |
| 6–8 | 200 | 1,000 |
| 9–20 | 100 | — |
| played 10+ nights | 50 | 50 |

**Never for sale:** extra time, a re-grade, points. Say so wherever prizes are listed.

### 4.2 Seasonal roles, and why the bot has to remove them
A "Season 1 Champion" still on a profile in Season 6 is not an honour, it is noise — it makes the
role unreadable and teaches new players that roles mean nothing. So removal is part of the same
step as assignment: every rollover strips the champion roles the bot granted in **all** earlier
seasons, whether or not the new season produced a champion. Roles it cannot attribute to a season
it crowned are reported and left alone, because a human's deliberate grant is not the bot's to
revoke.

---

## 5. STAFF LOAD — RECOMPUTED FOR THE ROTATION

```
L1  12 nights × 45 entrants × 0.25 min (tap the right option)   =  2.3 h  per season
L2  12 nights × 16 entries  × 0.55 min (one band button)        =  1.8 h
L3  12 nights × 12 entries  × 0.55 min                          =  1.3 h
Authoring  ~36 questions + 24 prompts (2–3 min each)            =  2.6 h
Checkout queue, season end, role apply                           =  0.5 h
                                              TOTAL            = ~8.5 h  ≈ 18 min/day
```
v3 measured 12.4 h for the same club on 28 all-league nights, and 37.3 h with rubric judging.
Two roles, rotating weekly: **Host** (posts, grades L1) and **Panel** (L2/L3 numbers). Two people
minimum, four to not burn out. `awarded_by` is on every award, so a bias complaint is answered
with data.

---

## 6. POLICIES, DECIDED

**No AI.** Entries are solo and in the player's own words. The bot ships **no detector**, on
purpose: model-written prose is not reliably separable from a rushed human paragraph, and a false
positive takes someone's night away on a statistic. The rule is stated on the card and in the
submit modal, the near-duplicate check catches copy-paste *between players*, and a staff member
can record the judgement with `flag_submission(..., 'ai', note=...)`. A flag never changes the
arithmetic — it is evidence, attached to a human's decision, clearable if it was wrong.

**Solo entries only.** Structural, not a rule to police: a submission row is keyed to the account
that pressed the button, so a second player cannot be attached to it.

**Where it runs:** Railway, one worker service, SQLite on a mounted volume at `/data`. See
README → Deploying. Without the volume a redeploy wipes the season.

**Still open:** whether to add a live Sunday bracket on top of the rotation (§3.4). The table no
longer needs it for fairness; it may still be worth it for excitement, which is a community call,
not a mathematical one.
