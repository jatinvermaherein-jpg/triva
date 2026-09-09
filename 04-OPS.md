# RUNBOOK — the panel, the flow, and what to do when it misbehaves

Format: **one league on a weekday, all three on Sunday**, 16:00 IST, 4-week season = 12 nights
per league. `00-TOURNAMENT-PLAN.md` is the rules; this file is the operating manual.

## 1. The evening, exactly as it happens

| When | Who | What appears |
|---|---|---|
| 16:00 | bot | **Weekday:** that league only — question cards in `#trivia-night`, **or** 1 card in `#strategy-night`, **or** 1 card in `#hangar-review`. **Sunday:** all three, simultaneously, in their own channels. Every one of them stays open **24 hours** |
| any time in the 24h | players | Tap an option. Reply says "recorded, you can change it until the timer". Ephemeral, so the channel stays readable. Editing your own answer before close is free |
| next day 16:00 | bot | The whole evening closes on its own. **No human presses anything to lock it.** |
| any time before close+24h | staff | On each card's grey staff row: tap the correct option → the bot pays everyone, adds the speed bonus and the +1 first-correct. If the question isn't worth the tier default, tap `🎯 Points` and type the number — a night already graded is re-graded immediately |
| after close | staff | `🏁 Finalise evening` (refuses until every question is graded, and tells you what's left) |
| after close | staff | `✅ Grade this evening` → `EXCELLENT 25 / GOOD 15 / AVERAGE 8 / POOR 2 / CUSTOM / Skip` per pending entry |
| after | bot | Standings rewrite themselves on the pinned board. Nothing to remember to press |

**The window is 24 hours (`answer_seconds`, default 86400) and it is the same for every league,
weekday or Sunday.** There is no per-season override any more — `create_season` has no window
parameter, because a season that silently ran 120-second nights looked identical in the database and
empty in practice. The speed bonus (halving every 40s, gone after 3 minutes) is what rewards knowing
the answer instantly; the 24 hours is what lets 200 people play when they are actually free.

**A weekday night is 2–4 minutes of staff time.** Sunday is the only busy one, and it is once a
week by design.

### 1.1 Authoring content (no ids, no dates)
`🛠️ Staff tools` on the hub panel → **Author a question** or **Set a night's prompt** → a select
listing the nights you can still write into → a modal. `/question-add` and `/scenario-set` with
no arguments open the same picker. Ordinals are automatic (next free slot). Nothing a staff member
does daily requires knowing that `evening 4821` exists.

### 1.2 The checkout queue (the bot does not pay anyone)
```
season ends → /queue-payouts (or the season-end panel) → rows appear in the pinned #checkouts card
            → a staff member sends the coins in the server economy
            → they press that row's ✓ button → row clears, records who cleared it
```
Coins sort above XP, biggest first, 10 rows per message. A second press on the same row is
**refused, not re-paid**. Clearing a coins row writes one shadow `coin_txn` row so the report
reconciles against whatever actually moves money; clearing XP never touches a coin balance.

## 2. Why the panels survive a restart (and how to keep it that true)

State lives in **custom_id + SQLite**, never in the view object.
`hub:ans:812:3` → parse → question 812, option 3 → read `evening.status` → act.

Break any of these and panels start dying after deploys:

1. `View(timeout=None)`, always.
2. Hand-written `custom_id` with a **fixed shape**. Never let discord.py generate one.
3. Register every view in `PERSISTENT_VIEWS`; `ui.install()` adds them **before** login.
4. **Never store an id, a page or a "current entry" on the view instance.** Views build from
   `(conn)` alone — if you need the evening id, put it in the custom_id. (Dynamic children, like
   the checkout buttons, are added in `__init__` with the id baked in, and `View.children` returns
   a **copy**, so do not try to reorder after `add_item`.)
5. Idempotency in the **schema**: `UNIQUE(award.entry_id)`, `UNIQUE(ledger.evening_id, player_id)`,
   `UNIQUE(payout.season_id, player_id, kind, reason)`. Re-running a grade rebuilds the night, so a
   double-click cannot pay twice. `UNIQUE(evening_id, player_id)` on `ledger` is load-bearing:
   widen it and a re-grade will **add** points instead of replacing them.
6. Staff permissions are checked **twice** — `interaction_check` *and* `_authorized()` at the top of
   every callback. A refactor that drops one must not open grading to players.
7. Re-render with `edit_message` on the same message. No new messages, no ping spam.
8. To strip controls use `view=_no_controls()` (a `View(timeout=None)` with no items). Passing
   `view=None` to `edit` is rejected by discord.py. Leaving stale buttons on a card is not
   acceptable for money rows, so the checkout card swaps itself to an empty view when the queue
   empties, and every callback re-checks status anyway.
9. Manual point corrections live on the sentinel `evening_id=0` with `league='adjust'`, and every
   rebuild deletes with `AND league != 'adjust'` — otherwise re-grading a night silently erases a
   correction made after the fact.

## 3. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Stale button." on a working panel | custom_id shape changed since that message was posted | Expected after a deploy that changed id formats. Finish the night, then `/tonight`. **Never change id shapes mid-season** |
| Button does nothing | view class not in `PERSISTENT_VIEWS` | Add it, restart. The #1 cause of "the panel died after I updated the bot" |
| Nothing posts at 16:00 | channels never provisioned, or an L1 night with 0 questions | `/setup mode:STATUS` says which id is dead. `/setup mode:SETUP` re-creates only what is missing. `/season-preview` shows authored counts |
| Only **one** league posts when you expected three | that is correct — weekday nights are single-league by design | Check the day: L1 Mon/Thu, L2 Tue/Fri, L3 Wed/Sat, all three Sun |
| Nothing posted in `#league-2` on a Monday | there is no Monday L2 night | Not an outage. See the row above |
| A whole night vanished after a re-grade | you widened `UNIQUE(ledger.evening_id, player_id)` | Put it back. Rebuilds depend on it |
| "FOREIGN KEY constraint failed" while authoring | the night is `locked`/`graded`, or you picked an L2/L3 night for a question | Now reported properly. Questions go on L1 nights that haven't finished |
| Player says "my answer didn't count" | answered after auto-lock, or edited after grading | `ledger` + `audit` have exact timestamps; `📊 My stats` shows their trail |
| Points look wrong | — | `V.adjust_points(conn, player, delta, reason, actor)` writes a **new** row, never an edit. Standings are derived, so the table fixes itself. Needs a non-blank reason |
| A player is missing from the league table | they are under the **5-night floor** | Intended for podiums only. Their points and coins still count, and the card says how many were excluded |
| Need to inspect | — | `sqlite3 /data/hub.db 'SELECT * FROM audit ORDER BY id DESC LIMIT 20'`. WAL: readers never block the bot |
| Payout looked paid twice | it wasn't | Second press returns "already cleared"; `UNIQUE` prevented the row. Check `coin_txn` for one shadow row per cleared coins payout |

## 4. Backups — the only thing that can end a season permanently

```bash
sqlite3 /data/hub.db ".backup /data/backup-$(date +%F).db"   # nightly, and copy it OFF the volume
```
`journal_mode=WAL` + `synchronous=FULL` means a crash cannot corrupt or lose an award. It does
**not** protect you from deleting the file. Standings are rebuildable from `ledger`; `ledger` is
rebuildable from nothing.

**On Railway:** the app filesystem is wiped on every deploy. The DB must live on a **volume mounted
at `/data`** with `HUB_DB=/data/hub.db`. `--check` prints a warning if it isn't. If you ever delete
`hub.db` by hand, delete `hub.db-wal` and `hub.db-shm` too — a surviving WAL re-applies the **old
schema**, which is how "the code is right but the bot is broken" happens.

## 5. Deliberate limits in this build

- **4 options max per question.** Discord allows 5 buttons per row and the 5th slot is "clear", so a
  5-option question cannot be rendered. Rejected at authoring, loudly, not at post time in front of
  200 people.
- **Single active season.** `season_id()` prefers the newest. Two concurrent seasons need an explicit
  id threaded through every command — not built.
- **Solo entries, enforced structurally.** `UNIQUE(evening_id, player_id)` for L2/L3. There is no
  second-entry item, so nothing has to be relaxed if you ever want one: change the key to include
  an `attempt`.
- **No AI detection, on purpose.** The only automatic similarity check is near-duplicate *between
  players* on the same night. `flag_submission(..., 'ai', note)` records a human judgement; it never
  changes arithmetic. Do not add a classifier — a false positive costs someone their night.
- **Standings cap at 25 rows, no pagination.** Add `?page=` before advertising a top-100 table.
- **Tests read the real wall clock.** `submit_answer`/`close_evening` use `datetime.now()`, so a test
  can only simulate dates up to today. Injecting a `clock` callable into `services` is the
  highest-value refactor left, and it unlocks a full simulated season under test.
- **Not built:** coin shop/wallet (removed by design — the bot does not run the economy), appeal
  tickets, weekly rollup post, optional `/final` bracket.

## 6. What a deploy looks like

```bash
git pull
python3 run_tests.py            # 5 suites, ~330 checks, ~3s; exits non-zero on any failure
python3 bot/main.py --check     # views registered? DB on a volume? token set?
```
- **Locally / a box you own:** `systemctl restart hub-bot`, any time, even mid-evening.
- **Railway:** push, or "Redeploy". The service restarts itself; nothing else to do.

Restarting **during** an evening is safe by design: answers in the DB stay, cards stay tappable,
the scheduler recovers anything it missed with a fresh full window, and the board re-renders from
SQLite. If a deploy can't happen at 16:05, it isn't restart-proof.
