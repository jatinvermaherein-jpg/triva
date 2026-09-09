# The Hub — Knowledge Season bot

Discord bot for a monthly Mech Arena knowledge competition. **One league a night, all three on
Sunday, 16:00 IST, 4-week season.** Staff author content and award points; the bot posts on time,
records everything, ranks it, hands out roles **and takes last season's back**, announces results — and **does not touch currency**.
Coins are queued as checkouts for a human to pay.

```
engine/scoring.py     THE RULEBOOK, as code. Points, bands, tie-breaks, rollover.
bot/db.py             SQLite schema + migrations. Idempotency lives here (UNIQUE + ON CONFLICT).
bot/services.py       Every rule the bot enforces. No Discord imports → fully testable.
bot/ui.py             Buttons, modals, panels. No state on view objects → restart-proof.
bot/main.py           Bot, slash commands, the 16:00 scheduler + downtime recovery.
bot/loader.py         Ensures exactly ONE copy of the ruleset is ever loaded.
sim/                  Fairness + load analysis behind the plan's numbers.
tests/                338 checks, ~3 seconds, no token required.
00-TOURNAMENT-PLAN.md  02-announcement-DISCORD.md  03-OPERATOR-KIT.md  04-OPS.md
```

## The format, in six lines

| | Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|---|
| League | 🧠 L1 | ⚔️ L2 | 🔧 L3 | 🧠 L1 | ⚔️ L2 | 🔧 L3 | 🏆 **all three** |
| Weight | ×1 | ×1 | ×1 | ×1 | ×1 | ×1 | ×1 |

12 nights per league per season, **every question open 24 hours** in every league. L1 = multiple
choice, value set **per question** (tier default 3/6/8 is a suggestion). L2/L3 = one written entry
inside the same window, awarded 0–25 on the bands 2/8/15/25. The bot adds the speed bonus and
first-correct; staff never do arithmetic, and no day multiplies anything.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python bot/main.py --setup          # Discord portal checklist
export HUB_TOKEN=your-token
python bot/main.py --check          # defaults the DB path; see Deploying
python bot/main.py
```

Python 3.11+ (`zoneinfo`), `discord.py` 2.x, **SERVER MEMBERS intent** on (role assignment).
Invite the bot with **Send Messages, Embed Links, Read Message History, View Channels,
Manage Channels, Manage Roles** — the last two are only for `/setup`, which builds the channels
and champion roles. No Administrator, no message-content access, nothing else.
Message Content intent is not needed and should stay off: the bot reads buttons and modals, never
chat text.

## First-run sequence, in Discord

```
/setup  mode:PLAN     read what would be created
/setup  mode:SETUP    pick the ONE staff role; the bot creates the category, 9 channels
                      and 5 champion roles, and stores every id
/setup  mode:STATUS   is anything renamed, missing or unwired?
/setup-channel   league: 📚 L1   channel: #league-1   (only to OVERRIDE one /setup made)
/setup-panel     channel: #staff-only
/pin-board       → the permanent leaderboard: this month · the three leagues · lifetime
/pin-checkout    → the pending-checkout queue staff clear with one button per row
/season-create   name: "Season 1"        (blank start day = next Monday, 4 weeks)
/question-add                            → opens a night picker, then a modal
/scenario-set                            → same, for an L2/L3 night
/tonight                                 → post now, or let 16:00 do it
/queue-payouts                           → at season end, turn standings into checkouts
```

Nothing here asks for an evening id, a player id or a channel id. Everything after setup is
buttons and modals; `/question-add` and `/scenario-set` are conveniences, not requirements.

Author the whole week in one sitting — that is the point of the scheduler: nothing at 16:00
depends on a human being awake.

## Tests

```bash
python3 run_tests.py         # all seven suites, ~5s, no token needed; non-zero exit on any failure

python3 tests/test_engine.py          #  48  rules: points, bands, tie-breaks, rollover, floor
python3 tests/test_bot_services.py    # 133  grading, idempotency, restart state, roles, payouts
python3 tests/test_bot_ui.py          # 108  persistence, layout limits, click paths, permissions
python3 tests/test_bot_scheduler.py   #  66  auto-post, auto-lock, outage recovery, routing
python3 tests/test_symbols.py         #  static guard + structural schema checks
python3 tests/test_deploy.py          #   40  every HUB_DB paste shape, the pooler trap, setup_hook
```

`test_deploy.py` is the newest one and exists because of the first live deploy: a paste the parser
rejected and a persistent view defined in the wrong scope. Neither is reachable from a fake — the
first needs psycopg's real conninfo grammar, the second needs `setup_hook` to run at all — so both
are now asserted directly, with the Postgres-only checks skipping loudly (never silently) when no
server is configured.

Run `run_tests.py` before every deploy. `test_symbols.py` catches two classes of bug that only
otherwise appear when a player presses a button: cross-module references that resolve to the wrong
module (`V.cfg` when it lives in `db`), and migration/schema drift — e.g. a column added to
`MIGRATIONS` but missing from `SCHEMA`, or two migrations for one table written as a dict literal
where the second key silently deletes the first.

## Deploying (Railway)

There is deliberately **no `nixpacks.toml`**. An earlier one broke the very first deploy: it carried
`providers = { python = "3.13" }` and a `schemaVersion` key, neither of which exists in Nixpacks'
config schema, so Nixpacks 1.41 died during `prepare` with
`invalid type: map, expected a sequence for key 'providers'` — before a single package was
installed. Every option in that file was already covered by `railway.json` or by Railway's defaults,
so it bought nothing and cost a deploy. Now Railway/Nixpacks runs on autodetection, which cannot
fail to parse.

The two things that must be pinned are pinned by plain text files instead:

- `runtime.txt` → `python-3.13`, and `.python-version` → `3.13`. Both are read by Nixpacks' Python
  provider; either alone is enough, they agree so there is no "which wins" question. **Without one of
  these you get Python 3.8 and the bot dies on `from zoneinfo import ZoneInfo`.**
- `requirements.txt` is exact (`discord.py==2.7.1`), and the full suite was run against a venv
  created from that file, so the deploy host resolves exactly what was tested. No `pip freeze` needed.

One **worker** service, no web server, `restartPolicyType: ALWAYS`, `numReplicas: 1`.

```
variables:  HUB_TOKEN   (Secret)  bot token
            HUB_DB      postgres://...  ← Supabase (recommended),  OR  /data/hub.db + a Volume
            HUB_GUILD   your server id  → instant command sync (global sync can take an hour)
            PYTHONUNBUFFERED  1     (optional for Python, it flushes anyway)
```

These three are the complete list — `grep` over `bot/` finds exactly three `os.environ.get` calls
(`HUB_TOKEN`, `HUB_DB`, `HUB_GUILD`); `HUB_ROLE` in `bot/ui.py` is a constant, not config. If Railway
ever needs the version overridden at build time rather than by the file, the documented variable is
`NIXPACKS_PYTHON_VERSION=3.13`.

**If a build fails again and you want to stop iterating on Railway's autodetection**, flip the
service's *Builder* to **Dockerfile** and commit this — it removes Nixpacks from the loop entirely:

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot/main.py"]
```

### Database: Supabase (recommended) or a Railway volume

Two backends, one code path. `HUB_DB` decides: a `postgres://` URL means Supabase/Postgres,
anything else is a SQLite file. `db.connect()` is the only place that knows the difference — the
144 queries in `bot/` never mention a driver.

**Supabase** is the better default because it removes the failure mode entirely. A SQLite file on
Railway survives only if you mount a Volume at `/data` *and* never recreate that service; get it
wrong and nothing errors — the season just quietly resets to empty on the next deploy.

Set it up like this:

1. Supabase → new project. **Settings → Database → Connection pooling → enable** (PgBouncer).
2. Copy **Connection string → URI → `Session` or `Transaction` pooler**, not the direct
   connection: Railway has no fixed egress IP, so the direct 5432 port is unreachable for it.
3. **Settings → Database → IPv4 — turn OFF the "enforce IP restrictions"** toggle, or every
   deploy connection is refused from an IP you cannot allowlist.
4. Railway → Variables → **RAW Editor** → paste the block below, then fill in `HUB_TOKEN` and
   `HUB_GUILD`. Keep `HUB_DB` out of git: that URL alone
   lets anyone rewrite a season.
5. Deploy, then run `/setup` again if you are moving an existing season (see below).

Use the pooler **port 6543** (`aws-0-<region>.pooler.supabase.com`) and keep `sslmode=require`.
**`prepare_threshold` must be `None`, not `0`.** psycopg's own guard reads
`if self.prepare_threshold is None`, so `0` skips the disable path and falls through to
`count >= 0` — which is true for a query's *first* execution. In other words `0` means "prepare
every query", and under PgBouncer (Supabase's `:6543`) a prepared statement lives on the server,
outlives our container, and collides: the next boot fails its very first query with
`prepared statement "_pg3_0" already exists`. `None` is the only value that turns the cache off;
client-side binds then work normally through transaction pooling. This is not a knob to
"improve" later by adding a pool — the line is load-bearing.

**Moving an existing season off SQLite:** run `python3 tools/sqlite2pg.py hub.db "postgres://…"`
**from your own machine** (it needs to reach both the old file and the new server; `tools/` is not
in the deploy image). It is idempotent — re-running after a few more nights copies the new rows and
supersedes the old ones rather than duplicating them — and it refuses to report success unless every
table's row count matches the source.
It copies rows, then re-aligns every `id` sequence, which is the step that gets forgotten and
bites later: Postgres sequences do not follow explicitly-inserted ids, so the next `INSERT`
without an id collides with a row that already exists — during a grading run, days from now.

Paste one of these into the RAW Editor (`.env` style — the form Railway's editor expects):

```
HUB_TOKEN=
HUB_DB=postgres://postgres.YOUR-REF:YOUR-PASSWORD@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require
HUB_GUILD=
```

or the JSON form, which does the same thing:

```json
{
  "HUB_TOKEN": "",
  "HUB_DB": "postgres://postgres.YOUR-REF:YOUR-PASSWORD@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require",
  "HUB_GUILD": ""
}
```

Nothing else is needed — the start command comes from `railway.json`, the Python version from
`runtime.txt`, and every other setting (open hour, answer window, point bands, channel/role ids)
is written into the database by `/setup`, not read from the environment.

**What `HUB_DB` tolerates.** All of these are copy/paste results, so all of them work: Supabase's
**JSON** "Database connection string"; a `postgresql://` URI whose query contains `?database=` or
`?db=`; keyword text (`database=… ssl=true`, as printed by some consoles); and a scheme mangled by
`pathlib` (`postgres:/…`). `bot/db.py` acts as a translator rather than a passthrough — it renames
`database`/`db` to libpq's `dbname`, maps `ssl=true` to a real `sslmode` value, drops keys psycopg
has never heard of (`driver`, `max_connections`, `description`), and rebuilds the URI — because
handing any of them to psycopg verbatim is what produced this repo's first crash-loop
(`invalid connection option "database"`). A value that is still not a connection is **refused at
boot** rather than treated as a filename: falling back to `/app/hub.db` is the one outcome where
the bot looks healthy and still loses the season. The failure prints one line naming the key that
is missing plus what to paste instead, and `tests/test_deploy.py` asserts every shape above.

**If you would rather stay on SQLite:** then yes, **add a Volume and mount it at `/data`**, and
set `HUB_DB=/data/hub.db`. `python bot/main.py --check` prints a warning when the DB is not on a
volume and a confirmation line when it is Postgres, so `--check` output tells you which backend
you are actually on:

```
db : postgres://xxx.pooler.supabase.com:6543/postgres (postgres)
     ✓ hosted Postgres: survives redeploys without a volume. …
```

### Why the Postgres path is not just `sqlite3` renamed

The two engines disagree in ways that a mock cannot show, so all four suites plus a new parity
suite were run against a real PostgreSQL 17 server (`HUB_TEST_DB=postgres://… python3
run_tests.py`). What had to be handled, each found by running it:

| difference | consequence if ignored |
|---|---|
| `INTEGER PRIMARY KEY` is the rowid in SQLite, plain int in Postgres | every id-less insert fails `null value in column "id"` |
| any error aborts the whole transaction in Postgres, not in SQLite | a player's duplicate submission poisons the `with conn:` block around it → `InFailedSqlTransaction` |
| `psycopg.IntegrityError` ≠ `errors.IntegrityConstraintViolation` | `except IntegrityError` misses UNIQUE violations → dead button, lost answer |
| `HAVING nights` (a SELECT alias) is legal SQLite, illegal Postgres | the leaderboard query 500s |
| SQLite's `INSERT OR REPLACE` deletes then inserts, so unlisted columns revert to DEFAULT | a re-grade would keep last run's `coins`/`applied_by` and print wrong totals |
| psycopg scans `%` for placeholders whenever a params object is passed | `LIKE 'role.%'` and any prompt containing `%` become syntax errors |

`tests/test_pg_parity.py` runs one full season on both backends and asserts the ledger rows,
award totals, leaderboard order, audit size and payout total are **identical**, then closes the
socket to prove the reconnect works.

### The build entry point (why there is a `main.py` in the repo root)

Railway's builder is now **Railpack** (`using build driver railpack-v0.39.0` in the build log).
Unlike Nixpacks it does not take its start command from `railway.json` — it detects a framework,
and for a plain script it looks for `main.py` or `app.py` **in the project root**, failing the
build outright if there is none. So the repo carries a ~17-line root `main.py` that `runpy`-executes
`bot/main.py`. It is deliberately a shim: `bot/` is not a package, and running the file keeps
`__name__ == "__main__"`, so argument parsing, `--check`, `HUB_DB` resolution and exit codes all
still live in exactly one place. `tests/test_deploy.py` asserts the root file stays a shim (no
`argparse`, no `discord`) and that `.dockerignore` never excludes it.

Measured against the real `railpack-v0.39.0` binary, on this repo:

```
before (no root main.py)  → exit 1, "✖ No start command detected"
after                     → exit 0, deploy startCommand: python main.py
python version            → 3.13.15, resolved from .python-version
                            ("idiomatic-version-file"), so runtime.txt still governs
.dockerignore             → honoured; tests/, sim/, tools/, *.db stay out of the image
```

If you would rather pin the command explicitly than rely on autodetection, either set
**Settings → Deploy → Start Command** to `python main.py` in the Railway dashboard, or add a
`railpack.json` at the repo root (`{"deploy": {"startCommand": "python main.py"}}`) — Railpack
reads that file and ignores `railway.json` for this purpose. `railway.json` still carries
`numReplicas: 1` and the restart policy, which do still apply.

### Interaction expiry, and why the slow commands say "Thinking…"

A slash command is only answerable for **3 seconds**. Past that — or if the gateway reconnects
in between, which the log shows as a second `logging in using static token` — the token is gone
and *every* way of answering raises `404 Unknown interaction`, including from inside the error
handler that was supposed to explain it. That is exactly how `/setup` failed on the first live
run: the traceback in the log was the error handler itself dying.

Two rules follow, asserted in `tests/test_deploy.py` and `tests/test_bot_ui.py`:

1. **Acknowledge first, work second.** `/setup`, `/setup-panel` and `/clock-tick` call
   `i.response.defer(ephemeral=True)` before touching the database, so a slow round trip to
   Supabase cannot cost the answer.
   *This used to exempt buttons and selects — "they are already answers" — and that sentence
   cost a live deploy.* The click is an answer; the callback is not. `StaffRoleSelect.callback`
   ran **16 statements** (one `staff_role_id` write plus the 15 config reads
   `setup_plan_embed` makes through `provision_plan`) before it replied, and on the deploy
   every one of those is a Supabase round trip. Free on local SQLite — 1.4 ms for all 16 —
   and 16 × RTT on a WAN link, so any link slower than ~190 ms per round trip (Railway and
   Supabase in different regions) walks out of the window. The log said
   `setup role picker failed … NotFound: 404 (error code: 10062): Unknown interaction`.
   It now calls `interaction.response.defer()` — on a component that is
   `DEFERRED_UPDATE_MESSAGE`, which acknowledges with no visible change and buys the full
   15 minutes — and then answers through `ui.reply`. Note what that forces: a deferred
   *update* can only be completed by **editing the message that was clicked**, so a refusal
   must never defer (it would overwrite the picker with the refusal text).
   `test_bot_ui.py` asserts the ack precedes the first plan read, and re-runs the whole click
   against a connection that costs 10 ms a statement with a 20 ms window — the same
   arithmetic, 1000× smaller.
2. **Never let a reply raise.** All of them go through `ui.reply(i, ...)`, which picks the one
   method that is legal for the interaction's current state (`is_done()` → `edit_original_response`,
   otherwise `send_message`) and swallows only `NotFound`/4xx — anything else still propagates, so
   a genuine bug is not mistaken for a lost race. `SETUP_NOTES` says "enable Members intent":
   that one *is* required, unlike Message Content.

**Still exposed, deliberately not changed here.** The same measurement run over the other
callbacks says the grading button is the next one to blow this window, and by a wide margin:
`QuestionGradeView.mark` runs `V.grade_question` *before* it answers, and that is 13 statements
for a question with one answer and **477 for one with 200** — about 95 s at 200 ms RTT. Fixing it
is not the same one-liner: the grade result is an ephemeral message to the staff member, so it
needs `defer(thinking=True, ephemeral=True)` (not a deferred *update*), the card re-edit has to
move from the interaction to `i.message`, and 477 blocking statements on the event loop will also
miss gateway heartbeats — so the honest fix moves that work off the loop, not just behind a defer.
A player tapping an answer button is safe: `submit_answer` is 2 statements.

### The staff permission check, and the bug the first real click exposed

Permission is enforced twice on purpose (`interaction_check` **and** an in-callback gate), but
the in-callback half was a **method on `HubView`** — and the staff-role picker in `/setup` is a
`discord.ui.RoleSelect`, an *Item*, which has never had that method. Clicking it raised
`AttributeError`, and the base `View.on_error` *logs and discards*, so what the user saw was a
select box that did nothing. Fixed by moving the check to a module-level
`ui.gate(interaction, conn, owner_bypass=…)` that both a View and a bare Item can call;
`HubView._authorized` now delegates to it, so the "check it twice" promise has one definition.

Two quieter bugs came out of the same hole:

- `_authorized` called `is_staff(user)` **without the stored id**, falling back to matching the
  literal name `"Hub Staff"`. That contradicts this project's own rule — the id decides, a rename
  must not matter — and would have locked every grading button out of any server whose staff role
  is called something else. It passes `staff_role_id` now.
- The picker needs `owner_bypass`: the confirm button is staff-only, and at the moment you choose
  the *first-ever* staff role, nobody is staff yet. Selecting the role now stores the id at once
  (idempotent — `provision()` writes the same key from the same value), so the person doing the
  setup can click confirm without being an administrator.

`tests/test_bot_ui.py` drives the real callback and asserts owner-accepted, stranger-refused,
picker-stopped, id-stored — plus a class-hierarchy scan that fails if any `Item` in `ui.py`
reaches for a `View`-only private method. That scan is what finds this bug without a live
Discord, and it caught a typo of mine while I was writing it.

### The /setup dead-ends: "the application did not respond", nothing created, empty log

The fix above corrected `_authorized`, the in-callback half of the check. The **dispatcher** half
was left matching the role *name*, and three more faults sat behind it. Together they produced one
symptom with no evidence: `/setup mode:SETUP` timing out, no channels, and a blank Railway log.

- **`interaction_check` matched `"Hub Staff"` by name.** It ran `is_staff(user)` with no stored id,
  so it fell back to the literal string — the exact bug already fixed one section above, in the
  *other* gate. Because discord.py runs `interaction_check` **first**, it refused every staff
  control before the corrected callback gate could allow it, on any server whose staff role is
  named anything else. Which is every server that used the picker, since the picker exists to
  choose that role. It now calls the same `ui.gate()` as everything else.
- **The owner bypass could never fire.** It compared `interaction.user.id` against
  `message.author.id` — but a bot's ephemeral message is authored by the **bot**, so that was
  always bot-id vs human-id. The bypass existed and was dead code. Discord answers this properly
  with `message.interaction_metadata.user`, so that is what is read now, with the author check
  kept as a fallback. `SetupProvisionView` also has to *pass* `owner_bypass=True`; it never did,
  so the "Create everything" button refused the non-admin who had just been told to press it.
- **Re-running `/setup` was silently dead.** `main.py` called `view.stop()` on the picker whenever
  a staff role was already stored, reasoning that a re-run "only re-confirms" it. But `stop()`
  unregisters the view, and `View._dispatch_item` returns `None` for a finished view — the click is
  discarded **before** the callback, so no reply is ever sent and Discord shows "did not respond".
  The dropdown still rendered and still looked tappable. Re-running `/setup` is the documented
  repair for a deleted channel, so it has to work; the `stop()` is gone.
- **`HubView.on_error` hid the evidence.** It replaced discord.py's default handler with
  `response.send_message` under `except discord.HTTPException: pass`, and logged nothing. A
  callback that had already deferred raised `InteractionResponded` — a `ClientException`, which
  that clause does not catch — so the handler meant to explain the failure failed itself. It now
  logs with a traceback first and answers through `ui.reply()`, which tolerates a spent token.

The fourth reason the log was empty is not Discord's fault: Railway reads stdout through a pipe,
and Python block-buffers a pipe in 8 KB chunks, so a bot logging a few hundred bytes an hour shows
**nothing** for hours while running perfectly. `main()` now line-buffers stdout, points
`basicConfig` at it, and passes `log_handler=None` to `bot.run()` so discord.py does not install a
second handler that double-prints every gateway line.

Eight regression checks cover these, and all eight fail against the previous commit: the two gates
under a renamed role, the owner bypass against bot-authored metadata (accepted for the invoker,
refused for a passer-by), the confirm view carrying the bypass, discord.py's drop-on-stopped-view
behaviour, and `on_error` both logging and replying. The buffering and handler fixes are asserted
in `tests/test_deploy.py`, which also fails if `/setup` ever regains a `.stop()` call.

### If the build fails

| log line | cause | fix |
|---|---|---|
| `AttributeError: 'StaffRoleSelect' object has no attribute '_authorized'`, beside `Ignoring exception in view <_SetupAskView …>` | a bare `ui.Item` calling a `HubView` method; `View.on_error` swallowed it, so the control looked inert | fixed by `ui.gate()` above. If your log *also* shows `timeout=900.0` on that view, you are not running this repo — that class has read `timeout=None` in every commit pushed |
| `✖ No start command detected` + `railpack prepare exited with an error` | Railway now builds with **Railpack 0.39**, not Nixpacks. Railpack does **not** read `railway.json`'s `deploy.startCommand`; it autodetects, and there was no `main.py`/`app.py` in the repo root | fixed by the root `main.py` shim (below). Verify with `railpack prepare . --error-missing-start`, or pin Settings → Deploy → Start Command in the dashboard |
| `Failed to ensure mise is installed` | Railpack provisions Python through `mise`, which wants a writable cache dir | nothing to do in the repo; it runs as root in the builder. Locally set `RAILPACK_CACHE_DIR` |
| `invalid type: map, expected a sequence for key 'providers'` | an old `nixpacks.toml` from before this fix, still in your working copy | delete `nixpacks.toml`; it is not needed |
| `Python version 3.13 is not supported` / it installs an old Python anyway | this deploy's Nixpacks (1.41) resolves versions from its Nixpkgs snapshot, which I cannot query from here | change `runtime.txt` and `.python-version` to `3.12` — one line, then redeploy. Or set `NIXPACKS_PYTHON_VERSION` and delete both files |
| `ModuleNotFoundError: No module named 'discord'` | requirements not picked up | check the service root is the **repo root**, not `bot/` |
| `invalid connection option "database"` (or `invalid URI query parameter`) repeating every second | `HUB_DB` written as keyword text, or a URI whose query Supabase/Prisma/TypeORM added | fixed as of this release — `db.py` now renames and filters those keys. If you still see it, paste the **URI** (`postgresql://…?sslmode=require`) and nothing else |
| `prepared statement "_pg3_0" already exists` | `prepare_threshold` was set to `0`, which means *prepare everything* | keep it `None` in **both** connect sites in `bot/db.py`; there is nothing to clear on Supabase — the statements die with the pooler's server connections |
| `NameError: name '_SetupAskView' is not defined` inside `setup_hook` | a persistent view defined inside `build_tree()`, referenced at module scope before that function ever runs | fixed by moving the class to module scope; `test_deploy.py` now calls `setup_hook` against a stand-in, because this only ever fired on a real gateway login |
| `FATAL: (ENOTFOUND) tenant/user postgres.<ref> not found` | project ref or pooler user wrong | the user must stay `postgres.<ref>` while Connection Pooling is on; drop the `.ref` only if you turn pooling off and use port 5432 |
| `404 Not Found (error code: 10062): Unknown interaction` on `/setup`, with a traceback from the error handler | the reply was sent after the interaction's 3-second window, or the gateway reconnected in between (look for a second `logging in using static token` nearby in the log) | fixed: the slow commands now `defer()` first and every answer goes through `ui.reply`, which cannot raise. Just run `/setup` again — nothing was created |
| `setup role picker failed` + `404 (error code: 10062): Unknown interaction` from `StaffRoleSelect.callback` | the picker answered after **16 database round trips** (1 `staff_role_id` write + the 15 config reads the plan makes). Free on SQLite, 16 × RTT on Supabase — past Discord's 3-second window on a cross-region link | fixed: the callback defers before it touches the database, then answers through `ui.reply`. The role id *was* stored, so `/setup mode:PLAN` still reads correctly — run `/setup mode:SETUP` to pick again |
| the staff-role picker stays clickable after you have already picked | `_view_state_after_pick` read `interaction.view`, which does not exist on `discord.Interaction` (discord.py keeps the live view on the *item*: `View.add_item` sets `item._view`), so it was `None` on every real click and `stop()` never ran | fixed: `_stop_the_picker` stops `self.view`. The test that was supposed to catch this passed because its fake interaction invented the attribute — it now drives a real `discord.ui.View` and asserts `is_finished()` |
| `invalid sslmode value: "true"` | Supabase's JSON field `ssl` pasted into a conninfo string | `db.py` maps it now; or paste the URI, which says `sslmode=require` |

The middle row is the only build risk I could not eliminate from here: I verified the Python the suite
runs on (3.13.14) but not which versions Railway's image offers. The code needs **3.9+** — only
`zoneinfo` (`bot/services.py:22`) sets the floor — so 3.10, 3.11 or 3.12 are all fine if 3.13 is
unavailable. Nothing in `bot/` or `engine/` uses a 3.12/3.13-only feature.

**`.dockerignore` is the ignore file Railway honours** — not `.railwayignore`, which is
undocumented and only ever applied by the CLI uploader. It keeps `*.db`, `tests/` and `sim/` out of
the build context, because a `hub.db` baked into an image can resurface over the volume and reads
exactly like "my season vanished after a deploy". Commit it if you deploy from GitHub; `railway up`
also accepts `--ignore-files`.

Two things that go wrong here and are worth knowing: `healthcheckPath` is unset because this is a
worker with no HTTP port (a health check on a worker kills it), and `Restart=always`/ALWAYS is not
decoration — recovery is designed to resume an interrupted evening with a fresh full window, so a
crash costs a delayed card, not a cancelled night.

For a box you own instead, the same environment with a systemd unit:

```ini
[Service]
WorkingDirectory=/opt/hub-bot
Environment=HUB_DB=/opt/hub-bot/hub.db
EnvironmentFile=/etc/hub-bot.env
ExecStart=/opt/hub-bot/.venv/bin/python bot/main.py
Restart=always
RestartSec=5
```

~200 MB RAM. A Raspberry Pi or a ₹300/mo VPS is plenty for 200 players.

## Upgrading an existing database

`connect()` runs `MIGRATIONS` on every boot, so new columns arrive on their own. One thing it does
**not** rewrite: a pre-`flag` database carries a FOREIGN KEY on `ledger.evening_id` that makes
manual point corrections fail. Fixing it needs a table rebuild:

```bash
python3 - <<'PY'
import sqlite3, shutil
shutil.copy("/data/hub.db", "/data/hub.db.bak")
c = sqlite3.connect("/data/hub.db")
c.executescript("""
PRAGMA foreign_keys=OFF;
CREATE TABLE ledger_new(id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL,
  evening_id INTEGER NOT NULL DEFAULT 0, season_id INTEGER NOT NULL REFERENCES season(id),
  league TEXT NOT NULL, day TEXT NOT NULL, points INTEGER NOT NULL, UNIQUE(evening_id, player_id));
INSERT INTO ledger_new SELECT * FROM ledger;
DROP TABLE ledger; ALTER TABLE ledger_new RENAME TO ledger;
PRAGMA foreign_keys=ON;""")
c.execute("VACUUM"); c.commit(); print("rebuilt", c.execute("PRAGMA foreign_key_check").fetchall())
PY
```

## Rules of engagement for anyone editing this

1. **The bot never decides a point.** No code that infers correctness from text, scores an essay,
   or picks a winner. Staff award; the bot records and multiplies.
2. **The bot never moves currency.** No wallet, no auto-mint, no shop. Owed money is a row in
   `payout` that a human clears. If you are about to add `add_coins` to a flow that isn't the
   reconciliation shadow row, stop.
3. **Change scoring in `engine/scoring.py` only.** `bot/` imports it and must not duplicate it, or
   the published rulebook and the live table drift apart.
4. **Corrections are new rows, never edits** (`adjust_points`, sentinel `evening_id=0`), and every
   ledger rebuild must keep `AND league != 'adjust'` or a re-grade erases them.
5. **A view holds no state.** If you write `self.evening_id = ...` for a *pinned* panel, put it in
   the `custom_id` instead.
6. **Never reuse a scored question.** There is no `question.used_in` column today — if you want the
   bot to enforce it, add the column, the migration entry and a check in `add_question`; until then
   it is a house rule you police by hand.

## Config (the `config` table)

| key | default | effect |
|---|---|---|
| `answer_seconds` | 60 | per-question window for League 1 |
| `sub_close_hour` | 20 | L2/L3 close time (IST) |
| `standings_night_floor` | 5 | nights required to appear in a **league** podium (never affects points or coins) |
| `channel:l1` `:l2` `:l3` | — | where each league posts |
| `hub_channel_id` | — | the hub panel |
| `board:<message_id>` | — | pinned leaderboard cards, refreshed on every award |
| `checkout:<message_id>` | — | pinned checkout queues |
| `prompt:<day>:<league>` | — | L2/L3 card text |

Season columns worth knowing: `coins_per_point` (1), `xp_per_point` (**0 = that economy is off**),
and the four champion role ids.

`set_cfg` JSON-encodes values — read config through `cfg()`, never `int()` a raw column.
