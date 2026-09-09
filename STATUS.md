# Build status — v4.0 refactor

## 2026-09-09 (newest) — heartbeat blocked: the event loop was waiting on Postgres

```
13:34:26  WARNING discord.gateway: Shard ID None heartbeat blocked for more than 10 seconds.
13:34:29  Loop thread traceback (most recent call last):
            File "/app/bot/ui.py", line 449, in run
              res = await V.provision(self.conn, _GuildWorkspace(i.guild, self.conn), ...
            File "/app/bot/services.py", line 273, in provision
              dbmod.set_cfg(conn, key, value)
            File "/app/bot/db.py", line 1174, in execute
              self._c.execute("RELEASE SAVEPOINT sp" + str(self._sp))
            File psycopg/connection.py ... waiting.wait(gen, self.pgconn.socket, ...)
```

Logged in fine at 13:20, first real work at 13:34 (`/setup` → "Create everything"), and the
loop thread dump shows exactly where the bot was stuck: **synchronously waiting on a Postgres
socket, on the asyncio event loop**. The facade kept its sqlite3-shaped synchronous API (that
is why 144 call sites survived the move to Supabase untouched), and the docstring's bet was
"queries run in the same region as the bot, so a statement costs milliseconds". The 13:34
statement cost more than ten *seconds* — a pooler hiccup, a dead TCP link, any WAN stall —
and while the loop sits in psycopg's socket wait the gateway cannot heartbeat, so Discord
drops a shard that goes quiet. Every button, every tick, every answer died with it.

The previous entry named this as the next failure and said what the real fix had to be: "move
that work off the event loop, not merely behind a defer". Done, in two parts:

**1. `db.acall()` — the async seam.** Async code may no longer call a blocking database
function directly; it awaits it through `acall(conn, fn, *args)`, which runs `fn` in a worker
thread when the backend is `PgConnection` and inline for a local SQLite file (microseconds,
no network, and the offline suites stay single-threaded). Every async surface was converted:
the scheduler (`tick`, `backfill`, `_extend_window`, `post_evening`, card re-edits, channel
resolution), every slash command, every button/modal callback in `ui.py` (player answers,
grading, awards, checkouts, panels, season end), the permission gate's config read, and the
three async services (`provision`, `finalize_season`, `_apply_roles`), including the
`with conn:` write-back blocks, which stay ONE transaction on ONE thread. Exceptions propagate
unchanged, so the duplicate-answer `except IntegrityError` paths still work; `PgConnection`'s
RLock now also guards `_ensure_conn` and `_heal`, which used to touch the shared connection
unlocked and would have raced the new worker threads.

**2. The stall itself is bounded.** `_pg_connect()` is the one place a connection is opened
(boot *and* reconnect — the reconnect path used to be a second, divergence-prone
`psycopg.connect` call). It adds `connect_timeout=10` (a reconnect could otherwise sit for the
OS TCP timeout, ~2 minutes) and TCP keepalives on a ~1-minute fuse (`keepalives_idle=30,
interval=10, count=3`): Supabase's pooler hangs up idle sessions, and without keepalives the
next statement on a dead link blocks for ~2 HOURS before the OS notices. kwargs override
anything a pasted conninfo carries, so the safety net applies to every HUB_DB shape.

Verified: new `tests/test_async_seam.py` (12 checks, registered in run_tests.py): the
keepalive/timeout kwargs reach `psycopg.connect`; `acall` runs Postgres-shaped work off the
loop thread and SQLite inline; worker exceptions reach the awaiter unchanged; and the property
the incident is about — with a simulated 1-second stalled statement, a heartbeat task through
`acall` keeps beating (≥10 ticks), while the same stall run inline freezes the loop (≤2
ticks, the old behaviour, kept as the negative control). All 8 suites green, no Postgres
server needed.

**Left open, noted:** a *server-side* slow query still holds its worker thread until the
keepalives fire or it finishes; nothing here can cancel it. If that ever matters, the seam is
the place a statement timeout or a pool would go — `db.acall`/`PgConnection` only, no call
site changes.


## 2026-09-09 (previous) — the picker answered too late, and never disabled itself

```
09:30:36  registered 11 persistent views; 0 evenings pending
09:30:38  logged in as 🏆 Mech Arena | Tournament Hub#4457 (1541282042128367626)
09:31:08  ERROR hub: setup role picker failed
          File "/app/bot/ui.py", line 206, in callback
            await interaction.response.edit_message(
          discord.errors.NotFound: 404 Not Found (error code: 10062): Unknown interaction
```

Two separate bugs, both in `StaffRoleSelect.callback`, and the first one is a rule this README
already had — written down, then exempted away.

**1. It answered after 16 database round trips, so Discord had already dropped the token.**
`/setup` defers before it touches the database; the picker did not, on the reasoning that
"buttons and selects are already answers". The click is; the callback is not. Measured by wrapping
the connection and driving the real callback: **16 statements run before the first response**
(the `staff_role_id` write plus the 15 config reads `setup_plan_embed` makes through
`provision_plan` — 5 roles, 9 channels, 1 category). 1.4 ms on local SQLite, which is why no
offline suite could see it; 16 × RTT against Supabase, so any link slower than ~190 ms a round
trip is past Discord's 3-second window, and after that the token does not arrive late — it is
gone, and every way of answering 404s with 10062.

Fixed by acknowledging first (`interaction.response.defer()`, which on a component is
`DEFERRED_UPDATE_MESSAGE`: no visible change, full 15 minutes) and answering through `ui.reply`
with the new `clear_content=True` — an omitted `content` leaves the message text alone, an
explicit null replaces the picker's prompt with the plan. A refusal still answers *without*
deferring, because a deferred update can only be completed by editing the clicked message, and
"that control is not yours" must not overwrite the picker.

**2. The picker was never disabled, because `interaction.view` does not exist.**
`_view_state_after_pick` did `getattr(interaction, "view", None)` — and discord.py 2.7.1 has no
such attribute (`hasattr(discord.Interaction, "view")` is `False`; the view lives on the *item*:
`View.add_item` sets `item._view`, and the dispatcher calls
`item.view._dispatch_item(item, interaction)`). So it was `None` on every real click and `stop()`
never ran. The check that was supposed to catch it — "the picker that was answered is stopped" —
passed because the test's fake interaction *invented* the attribute. `_stop_the_picker` now reads
`self.view`, and the test drives a real `discord.ui.View` (built inside the loop, because
`View.stop()` can only mark `is_finished()` when the view was handed a loop for its stopped
future).

Verified: 125 UI checks (was 118), all 7 suites green. Negative control on the deployed callback
shape against a 20 ms window with 10 ms per statement: `NotFound`, view still `is_finished() ==
False`; the fixed callback against the same link: no exception, one ack, one answer, view
finished.

**Left open, measured:** the same instrumentation says the grading button is the next to blow this
window and by much more — `QuestionGradeView.mark` runs `V.grade_question` before it answers, and
that is 13 statements for one answer, 34 for ten, 127 for fifty, **477 for 200** (~95 s at 200 ms
RTT). Not fixed in this pass on purpose: the grade result is an ephemeral message to the staff
member, so it needs `defer(thinking=True, ephemeral=True)` rather than a deferred update, the card
re-edit has to move off the interaction onto `i.message`, and 477 blocking statements will also
miss gateway heartbeats — so the real fix moves that work off the event loop, not merely behind a
defer. A player's answer tap is fine: `submit_answer` is 2 statements.


## 2026-09-09 (previous) — first real click on the staff picker, and it was inert

`AttributeError: 'StaffRoleSelect' object has no attribute '_authorized'`, twice, prefixed
`Ignoring exception in view <_SetupAskView timeout=900.0 children=1>`. A `RoleSelect` is a
`discord.ui.Item`; `_authorized` is a `HubView` (View) method. The other eleven call sites all
sit inside `HubView` subclasses, which is why copying the line read as safe — and
`View.on_error`'s default handler *logs and discards*, so the symptom was a control that silently
did nothing rather than a visible failure.

Fixed: the check moved to module-level `ui.gate()`, callable by both. Same pass closed two more —
`_authorized` was calling `is_staff(user)` **without** the stored `staff_role_id`, falling back to
matching the literal name "Hub Staff" (exactly the rename-fragility this project promises not to
have, and it would have locked out the very role being created), and the picker needed
`owner_bypass` plus an immediate `set_cfg("staff_role_id", …)` so the person choosing the
first-ever staff role can click the staff-only confirm button without being an administrator.
`/setup` is now `@guild_only()` too: `setup_plan_embed` dereferenced `interaction.guild`
unguarded, so invoking it from a DM was an `AttributeError` waiting to happen.

Also removed a false promise: `setup_hook` no longer `add_view()`s the picker. A view resumes only
when it is persistent, and this one carries no message_id — `SetupProvisionView` (persistent,
id-bearing) is the part that survives a restart, and it stays registered.

Verified by driving the actual callback in `test_bot_ui.py` (118 checks) and a class-hierarchy
scan that fails if any `Item` in `ui.py` references a `View`-only private method — that scan
caught a typo of mine (`ui_gate`) while I was writing it. Both new deploy checks were
negative-controlled; deleting `/setup`'s `defer` initially still passed because my source check
skipped every `if ... return`, including the real body. It now skips only a guard that contains
no work, and it fails when the defer is removed.

**Open question for the operator:** the container logs `timeout=900.0` for `_SetupAskView`, but
that class has read `timeout=None` in every commit ever pushed, and the `on_error` added in
`630e98c` (pushed 08:39) was absent from a 09:03 deploy. The running image may not be `main` —
worth confirming in Railway → Deployments before chasing further ghosts.


## 2026-09-09 (latest) — bot is LIVE on Discord; `/setup` was answerable too late

`registered 11 persistent views` … `Shard ID None has connected to Gateway` …
`logged in as 🏆 Mech Arena | Tournament Hub#4457 (1541282042128367626)`. The build and the
database work; the remaining failure was on Discord's side of the fence:

`discord.errors.NotFound: 404 (error code: 10062): Unknown interaction` from `/setup`, and the
traceback was raised **by the error handler** (`tree.error` → `followup.send` on a dead token),
which is why nothing was shown to the user. Three `logging in using static token` lines in three
minutes says the gateway session was bouncing, and a bounce between command and answer kills the
interaction token outright.

Fixed in two layers: the three handlers that do real work before answering (`/setup`,
`/setup-panel`, `/clock-tick`) now `defer()` first, and every reply is routed through one
`ui.reply()` that cannot raise. `setup_error` converts an expired interaction into a sentence
instead of a stack. `test_deploy.py` grew a state-machine fake (fresh / deferred / answered /
expired / forbidden) because the previous `FakeInteraction` had no way to represent a dead token —
that is why 72 unguarded reply sites all looked healthy — plus a source check that those three
handlers defer, and `_SetupAskView` got an `on_error` so an abandoned picker fails visibly.
Negative control verified: stripping the guard makes the expired case raise `NotFound` again.

## 2026-09-09 (later) — the *build* broke too: Railway changed builders to Railpack

The next deploy never started a container at all: `using build driver railpack-v0.39.0` …
`✖ No start command detected` … `railpack prepare exited with an error`. Railpack does not read
`railway.json`'s `deploy.startCommand` the way Nixpacks did — it autodetects, and for a plain
script wants `main.py`/`app.py` in the project root. Fixed with a 17-line root `main.py` that
`runpy`-executes `bot/main.py` (single entry point, no `bot/__init__.py` needed, `__main__`
preserved). Verified with the actual `railpack-v0.39.0-x86_64-linux-musl` binary: exit 1 before,
exit 0 and `startCommand: python main.py` after, Python resolved to 3.13.15 from
`.python-version`, `.dockerignore` still honoured. `test_deploy.py` now runs `python main.py
--check` from a bare tree and guards the shim against becoming a second copy.

## 2026-09-09 — first live Railway deploy: two crash-loops, both fixed

The Supabase deploy at `c0f246d` never reached a player. Two independent causes, and **no data
was ever at risk** — both fired before the first write.

1. `psycopg.ProgrammingError: invalid connection option "database"`, every 0.7 s. `_normalise()`
   handed psycopg keyword text and URI query strings through unvalidated, and Supabase's own
   console prints the key as `database` where libpq requires `dbname`. Fixed: `db.py` is now a
   translator, not a passthrough — aliases (`database`/`db`→`dbname`, `ssl`→`sslmode`), a key
   whitelist, and the URI rebuilt from parsed parts. Values that are *still* unreadable exit with
   one line naming the missing field instead of a traceback, and are never treated as a filename.
2. `NameError: name '_SetupAskView' is not defined` in `setup_hook`. The class lived inside
   `build_tree()`, which runs after login, but the persistent-view registration ran before it —
   so it only ever fired against the real Discord gateway. Fixed by moving it to module scope.
3. Latent, and the nastier of the three: **`prepare_threshold=0` does not disable prepared
   statements.** psycopg's guard is `is None`, so `0` falls through to `count >= 0` and prepares
   *every* query. Through PgBouncer those statements outlive the container, so the restart caused
   by (1) or (2) then failed the first query with `prepared statement "_pg3_0" already exists`.
   Both connect sites now pass `None`, and the README paragraph that confidently recommended `0`
   has been rewritten to say why only `None` works.

`tests/test_deploy.py` (40 checks) is new and covers exactly these: every pasteable `HUB_DB`
shape, the `prepare_threshold` value seen by psycopg, and `setup_hook` run against a stand-in.
The Postgres-only checks skip *loudly* when no server is configured. Verified against a real
Supabase pooler endpoint: the old paste now fails on credentials (`tenant/user not found`), which
is the correct complaint for fake credentials — it no longer fails on parsing.


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
- **Railway** — `railway.json`, `.dockerignore` (NOT .railwayignore, which is undocumented),
  `HUB_GUILD` instant sync, `--check` warns if the DB is off `/data`. `nixpacks.toml` was
  **deleted** after it failed the first deploy (see README); Python is pinned by `runtime.txt`
  and `.python-version`, and `requirements.txt` is now exact and was test-verified from a clean venv.
- Docs rewritten for the rotation: plan, announcement, ops, README.
- **Postgres / Supabase backend** (`main`, this round) — `db.connect()` now speaks to either
  engine and nothing else knows the difference. `HUB_DB=postgres://…` selects Postgres; a file
  path still selects SQLite+WAL. The facade in `bot/db.py` translates `?`→`%s` (quote-aware),
  `INSERT OR REPLACE/IGNORE`, `with conn:` (23 sites) → BEGIN/COMMIT/ROLLBACK with savepoints so
  a caught UNIQUE violation stays survivable, identity `id` columns, bind-time bool/float
  coercion, and `sqlite3.IntegrityError`-compatible error names. `requirements.txt` gains
  `psycopg[binary]==3.2.10` with `prepare_threshold=0` for PgBouncer transaction pooling.
  `tools/sqlite2pg.py` migrates a live season and re-aligns sequences; verified value-for-value.
- **Verification, not assumption** — all four suites plus a new `tests/test_pg_parity.py` were
  run against a real PostgreSQL 17 server (`HUB_TEST_DB=postgres://…`): 49 · 171 · 108 · 75 · 14 ·
  refs green on **both** backends. Six dialect bugs were found only by running it (identity ids,
  aborted-transaction poisoning, psycopg's IntegrityError taxonomy, HAVING aliases, REPLACE
  revert-to-DEFAULT, and `%` in `LIKE`) — each is in the README table. `run.sh` installs both drivers.


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
