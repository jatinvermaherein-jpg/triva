# Hub Knowledge — Discord Knowledge Season bot

TypeScript + discord.js + Supabase bot running a four-week Mech Arena knowledge competition.

Three independent leagues run Monday–Saturday, opening 4 PM IST, each question open 24 hours:

| League | Format | Scoring |
| --- | --- | --- |
| 🧠 Knowledge | Private multiple choice, one final answer | 1–10 points from the answer key, +1 for first correct |
| ⚔️ Strategy | Long answer, saved drafts, edits until closing | Staff score 0–10 |
| 🔧 Hangar | Long answer, saved drafts, edits until closing | Staff score 0–10 |

Rewards (per league, per season): 1st 3,000 · 2nd 2,000 · 3rd 1,000 · 4th 800 · 5th 700 · 6th 600 · 7th 500 · 8th 400 · 9th 300 · 10th 200. Coins are **recorded, never transferred** — staff pay them manually. Champion roles are created and assigned automatically.

## Project structure

```text
├── package.json
├── tsconfig.json
├── .env.example
├── .dockerignore
├── railway.json
├── sql/
│   └── 001_initial.sql     schema, derived-scoring views, transactional rules
├── src/
│   ├── domain.ts           scheduling, ranking, draft limits, image sniffing
│   └── index.ts            bot, panels, worker loop
└── tests/
    └── domain.test.ts
```

## Where the rules live

The database, not the bot process, owns anything that can race:

- `ks_answer_points` derives earned points, including the first-correct bonus. The bonus is computed from the earliest eligible correct answer, so disqualifying that answer moves the bonus automatically instead of leaving it attached to a removed user. Knowledge answers are excluded from the view until their challenge closes, so open trivia results never leak into standings.
- `ks_standings` aggregates points, correct counts, the 10→0 score histogram, and staff tie-break values.
- `ks_answer_action()` (save / submit / trivia) locks the season row, enforces the deadline, blocked participants, the 10-section / 2,000-word / 20,000-character limits, and version-stamps every submission into `ks_answer_versions`.
- `ks_score_answer()` requires the reviewer's expected version, refuses self-scoring, refuses scoring before closing, and refuses to override a derived Knowledge score.
- `ks_finalize()` refuses while any valid answer is unscored, refuses unresolved top-10 ties, excludes departed members, and freezes awards as snapshots.
- `ks_events` is the audit trail: score changes, wording corrections, cancellations, reward status changes.

All `ks_*` tables have RLS enabled and are revoked from `anon`/`authenticated`; only `service_role` (the bot backend) can reach them. Members never talk to Supabase.

## Setup

```bash
npm install
cp .env.example .env     # then fill it in — never commit .env
```

Run `sql/001_initial.sql` in the Supabase SQL Editor, then:

```bash
npm test
npm run build
npm start
```

### Discord application

Enable the privileged **Server Members** and **Message Content** intents (Message Content is used for images uploaded into private answer workspaces).

Install with the `bot` and `applications.commands` scopes and these permissions: View Channels, Send Messages, Send Messages in Threads, Read Message History, Embed Links, Attach Files, Manage Channels, Manage Roles, Manage Threads, Create Public Threads, Create Private Threads. Place the bot's role above the roles it manages. Administrator is **not** required.

### First run

Run `/setup` in the server, pick the trusted staff role, and the bot creates the channel layout, the Nonparticipating role, the panels, and Season 1 (starting the next Monday 00:00 IST). `/panels` reposts the three root panels if they are deleted. Everything else is buttons and dropdowns.

## Deploy (Railway)

1. Push this repository to a private GitHub repo.
2. Create a Railway project from it and add the variables from `.env.example`.
3. Deploy with **exactly one replica** — the worker loop has no cross-process lease yet.
4. Confirm the logs show `Logged in as YOUR_BOT`, then run `/setup` in Discord.

The committed `package-lock.json` is required: the build step is `npm ci`.

## Tests

`npm test` runs the scheduling and ranking helpers under `node --test` via tsx: IST Monday selection, the 24-slot four-week calendar, 4 PM IST openings, no Sunday openings, word counting, the draft limit, and Knowledge-vs-Strategy tie-break ordering.

## v1 limitations — read before a prize-bearing season

- **Setup recovery:** setup is not a resumable wizard; a failure midway through channel creation can leave partial channels to clean up by hand.
- **Draft concurrency:** saved drafts survive restarts, but simultaneous edits from two open forms are not guarded by optimistic draft-version checks.
- **Advanced moderation:** clarification threads, season-scoped bans, and corrections to already-finalized awards have no dedicated panel.
- **Recovery controls:** deleted-channel/role rebinding and ambiguous Discord state are logged, not auto-repaired.
- **Large histories:** several selectors and review queries use bounded lists; add pagination before heavy use.
- **Retention:** automatic deletion of old submissions/attachments is **not implemented**.
- **Supabase Free capacity:** five 10 MB images per answer fills storage quickly; quotas are not bypassed.
- **Award-role capacity:** permanent hoisted roles are created without a role-limit warning.
- **Integration coverage:** tests cover scheduling and ranking helpers, not a live Discord/Supabase deployment. The SQL migration has been parsed with PostgreSQL's grammar and its derived-scoring views exercised against an in-memory engine, but it has not been run against a live Supabase project.
- **High availability:** one replica only.

**Recommended first run:** a private test server, sample questions in all three leagues, two member accounts submitting and being scored, and a permissions pass before inviting the community.
