# Build status — v4.0 refactor

Last run: `python3 run_tests.py` → **all 5 suites green, 287 checks**
(engine 48 · services 123 · UI 52 · scheduler 64 · symbol cross-check).

## Engine + schema + services: done for v4

| Area | State |
|---|---|
| Rotating calendar (Mon🧠 Tue⚔️ Wed🔧 Thu🧠 Fri⚔️ Sat🔧 Sun🏆 all 3) | `create_season` builds 36 evenings / 12 per league in 4 weeks, raises if the leagues are unequal |
| Sunday grands | `evening.difficulty='grand'`, `multiplier=1.5`, `is_sunday=1` |
| Per-question points (staff-entered) | `question.points_per_correct`; `default_points(tier, is_grand)` deliberately does NOT pre-apply the Sunday 1.5x; `grade_question` resolves base as *passed > authored > tier default* |
| Points-and-roles only, no wallet | `payout` queue: `queue_payouts` (idempotent), `pending_payouts` (coins first, then biggest), `payout_batches` (≤10 rows/message), `clear_payout` (race-guarded, writes one shadow `coin_txn` row for coins only), `payout_summary` |
| Qualification floor | `standings(scope='league', min_nights)` from config `standings_night_floor` (5); podium floor filters the table, never the points; `scope='total'` = lifetime and ignores the floor |
| Upgrade safety | `MIGRATIONS` + `_migrate()` in `connect()` add new columns to an existing `hub.db` |

### Guardrails added this round (each backed by a test)
- `add_question` refuses a night that has already run (`locked`/`graded`) with a message
  naming the status — previously it died as `FOREIGN KEY constraint failed`.
- `add_question` refuses a **League 2/3 night** outright: such a question can never be
  posted or graded, so it would silently sit there while staff believed it was live.
- Manual corrections (`league='adjust'`) live on the sentinel `evening_id=0`, and every
  ledger rebuild deletes with `AND league != 'adjust'`, so re-grading a night cannot
  erase a correction. `UNIQUE(evening_id, player_id)` is load-bearing and commented —
  widening it would make a re-grade ADD points instead of replacing them.
- `adjust_points` refuses a zero delta and a blank reason.
- Test suites delete `-wal`/`-shm`/`-journal` with `_fresh()`: a stale WAL resurrects the
  OLD schema, which is how "the code is fine but the test fails" happens on a real server too.

## v4.1 — UI, policy and deploy (this round)
- **No ids typed anywhere.** `PickNightView` + `NightSelect` + `QuestionAuthorModal` /
  `PromptAuthorModal`; `/question-add` and `/scenario-set` open the picker when left
  blank; `/season-create` defaults to next Monday. Ordinals auto-assign (`None`).
- **Checkout panel** `CheckoutView`/`checkout_embed` + `/pin-checkout`, `/queue-payouts`.
- **Permanent board** `board_embed` (month + lifetime + league), `BoardView` gains
  `total`, `ui._refresh_boards` fires on award/points/season-end; `/pin-board`.
- **Per-question points** `🎯 Points` on every grading row → `PointsModal`; audited with
  the reason; re-grades a night that has already been scored.
- **No AI, solo** — `HONESTY_NOTE` on the card and modal, `flag_submission` records a
  human judgement on `submission.flag` (migration added). No classifier anywhere.
- **Railway** — `railway.json`, `nixpacks.toml`, `.dockerignore` (NOT .railwayignore,
  which is undocumented), `HUB_GUILD` instant sync, `--check` warns if the DB is off `/data`.
- Docs rewritten for the rotation: plan, announcement, ops, README.

### Bugs this round caught by running things
- `grade_question` filtered `entry.status`, a column that never existed — masked by
  a stale `hub.db-wal` carrying an old schema. Fixed by deleting the filter.
- `MIGRATIONS` was a dict keyed by table: adding `submission.flag` silently deleted the
  existing `submission.edit_after_close` entry. Now a list of triples + a static guard.
- `evening.difficulty` and `payout.content_hash` were only in MIGRATIONS, not SCHEMA.
- `close_evening` left `closes_at` at 20:00 after an early staff close, which would have
  stamped every later submission as an after-hours edit.
- `Embed.add_field(overwrite=True)` does not exist in discord.py (3 sites).
- `View.children` returns a copy, and setting `.row` before `add_item` makes discord.py
  silently REMOVE the item — both broke the new picker until built via `add_item` only.

## Still open
- Sunday Grand **bracket**: the table no longer needs it for fairness (champ flip 44%
  vs 99% under v3), so it is an entertainment call. Not built.
- `question.used_in` (no-reuse enforcement) does not exist; README now says so instead
  of pointing at a phantom column.
- Inject a clock into `services` so a full season can be simulated under test.

## v4.1b — seasonal roles stop piling up (2026-09-09)

Reported by the owner: "old champion roles are never removed, they just pile up." True, and worse
than reported — three separate defects:

| Defect | Effect | Fix |
|---|---|---|
| `hall_of_fame.role_id` was **never written**, and removals were selected `WHERE role_id IS NOT NULL` | the removal query matched zero rows **forever** — no role was ever taken back | `_role_ids_for_season()` (season config ∪ stamped HoF ids) + `_stamp_hof_role()` on a successful add |
| Removals looked only at the **immediately previous** season | one skipped/failed rollover strands roles permanently | `stale_roles(conn, up_to_season)` scans **all** earlier seasons |
| Removal was tied to a successful award | a season where every league is skipped strips nothing | removals emitted **unconditionally** |
| `season_report`/`role_diff` resolved the season themselves via `season_id()` (prefers newest) | closing S1 after S2 exists reported an empty table and crowned nobody | explicit `season=` threaded through `finalize_season` → `_apply_roles` |

Safety kept: only seasons with a `hall_of_fame` placement=1 row are swept, and a champion whose role
can't be resolved reports `action:"manual"` and is **never revoked** — a human's deliberate grant is
not the bot's to take.

Test counts: engine 48 · services **143** · UI **96** · scheduler 66 · static guards. `run_tests.py` green.
`03-OPERATOR-KIT.md` rewritten for v4 (it still documented `/season end`, `/coins payout`, a
`used_in` column, rubric judging and an automatic third judge — none of which exist).


Also fixed the same defect on the **dry-run side**: `SeasonAdminView` previewed `role_diff(conn)`
(self-resolving → newest season) while APPLY closed the *active* one, so the preview could describe
a different season from the change it showed. Both now call one `closing_season_id(conn)`, the embed
titles itself "Season N end — DRY RUN", and `manual` actions render 🖐 instead of an unlabelled `·`.

**Known gap, not hidden:** `services.adjust_points()` has no button or command. Corrections go
through re-grading until it does.

## v4.2 — Sunday demoted, 24h windows, `/setup` builds the server (2026-09-09)

Four owner decisions, all now enforced in code rather than prose:

1. **No "Grand" anything.** `SUNDAY_MULTIPLIER`, `GRAND_TIEBREAK_BONUS`,
   `GRAND_PARTICIPATION_BONUS` and `EventResult.is_grand` are **deleted** from the engine.
   Every evening is written `difficulty='normal', multiplier=1.0`. A static guard in
   `test_symbols.py` now fails if the engine ever re-exposes a Sunday multiplier.
   No Grand Finalist role exists, and no season-end bracket.
2. **One window rule: 24 hours, every league, every night.** `create_season` LOST its
   `answer_seconds` parameter (a per-season override made "48h for every question" a
   suggestion) and `sub_close_hour` is gone, so L1 and L2/L3 no longer have different
   deadlines. A Mon 16:00 trivia night closes Tue 16:00; its league's next question is Thu
   16:00, i.e. the ~48h gap that was asked for. `open_at` is now genuinely IST (it was
   16:00 **UTC**, and every document said "16:00 IST" — six hours wrong).
3. **`/setup`** (plan | status | setup) asks exactly one question — which role runs the
   quizzes — then creates 1 category, 9 channels and 5 roles with real overwrites.
   Needs **Manage Channels + Manage Roles only**; never Administrator.
4. **Everything is stored by Discord id.** `hub:role_*` / `hub:chan_*` / `staff_role_id`
   in `config`; lookups go id-first, then name, then create. Renaming `#trivia-night` or
   the staff role is cosmetic and the regression tests prove it. `is_staff` now matches the
   stored **role id** (name-matching meant a rename revoked every button and anyone could
   mint a role called "Hub Staff" to gain them).

Bugs found while doing it, all fixed and pinned by tests:
- `hub:staff` was **one key for two objects** (the Hub Staff role and #staff-only), so the
  channel write deleted the role's stored id and the bot silently fell back to name
  matching. Keys are namespaced by kind now, with an import-time `_no_collisions()` guard.
- The category was written as `hub_category_id` but read as `hub:category` — never adopted.
- `post_evening` called `open_evening` **before** checking for content, so an L1 night with
  no questions became `open`, accepted submissions against nothing, and the scheduler
  (querying only `status='scheduled'`) never retried it. Status now flips after the cards;
  an empty night stays `scheduled`, warns `#staff-only` at most once an hour, and opens on
  the next tick after staff author it.
- The `get_channel(ev["channel_id"] or 0)` fallback looked up **channel 0** for a night that
  had never posted. Now guarded by the actual value.
- `FakeChannel` in the scheduler suite dropped `content`, so every text-only message was
  invisible to assertions — a blind spot, not a bug, but it hid the two above.

Counts: engine 49 · services **171** · UI **108** · scheduler **75** · static guards =
**503 checks**, `run_tests.py` green, `--check` exit 0, 11 persistent views.
