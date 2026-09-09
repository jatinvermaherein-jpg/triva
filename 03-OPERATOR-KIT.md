# OPERATOR KIT — run-of-show, authoring, grading, rollover

For **v4.x**: rotating week (🧠 Mon · ⚔️ Tue · 🔧 Wed · 🧠 Thu · ⚔️ Fri · 🔧 Sat · 🏆 Sun),
seasons of 4 weeks, points in per question, **band numbers out**, staff pay the coins.

Read `00-TOURNAMENT-PLAN.md` §0 first. The two things staff get wrong are the *who* (the bot does
arithmetic and timing; you do content and judgement) and the *how much* (you never invent a number,
you pick one from a band).

---

## 1. NIGHT RUN-OF-SHOW (print this)

| T (IST) | Who | Action |
|---|---|---|
| 16:00 | **bot** | Opens the night(s) by itself and posts the card in the league channel. No human at 16:00. |
| any time in the 24h | host | L1: `Answer` → the player taps A/B/C/D. One accepted answer per player per night — that is what makes points mean something. |
| any time in the 24h | host | L2/L3: `📝 Submit my answer` → one text box, solo entry. Confirmation repeats the deadline. |
| +24h | **bot** | Closes the evening — the same 24 hours for L1, L2 and L3, weekday or Sunday. Answers freeze; anything typed after is recorded and audited, never silently overwritten. |
| whenever you sit down | host | L1 grading: pick the correct option, confirm. Finalise → the bot computes speed bonus, per-question points and totals. |
| whenever you sit down | panel | L2/L3 grading: read the entry, tap a band button (or type a number). One reviewer, or two typing the same number into two accounts. |
| after | **bot** | Board cards, the league channels and the pending-checkout queue update themselves. |

**The only hard rule: a night is not finished until points are posted.** A night scored 8 hours late
costs more participation than a slightly wrong score costs integrity.

**Sunday differs in exactly one way:** *all three* leagues run the same night. Same open hour, same
24 hours, same points per band — no multiplier, no "Grand", no extra role. If you have never graded
three nights in one sitting, rehearse it before the first real one: the failure mode isn't wrong
numbers, it's the last league getting graded at 23:40 by someone who has stopped reading.

**Nobody gets pinged.** The bot does not send `@everyone`, does not send a reminder before a night
opens, and does not ping a role to start one. The opening card in the league channel is the whole
announcement. If you want a nudge, post it yourself — an unprompted ping from a bot is how a server
learns to mute it.

**The last Sunday is also season end.** Do not grade it the same night. Finish grading first, then
rollover (`§7`).

---

## 2. PRE-SEASON CHECKLIST

```
[ ] Discord dev portal: SERVER MEMBERS intent on (roles need it)
[ ] bot invited with View/Manage Roles/Send Messages/Embed Links + role above every role it grants
[ ] invite the bot with ONLY: Send Messages, Embed Links, Read Message History,
    View Channels, MANAGE CHANNELS, MANAGE ROLES
    (Manage Channels + Manage Roles are for /setup to build the server. Administrator is
     NOT needed and the bot will never ask for it. No other permission does anything here.)
[ ] /setup mode:SETUP — the bot creates the category, the 3 league channels,
    #hub-announcements, #league-table, #season-results, #appeals, #pending-checkout,
    #staff-only, and the 4 champion roles (Trivia / Strategy / Hangar / Season).
    There is no "Grand Finalist" role: Sunday is not a prize tier.
    Every id is stored; rename or recolour any of it later without touching the bot.
[ ] deploy on Railway: HUB_TOKEN + HUB_DB=/data/hub.db + volume at /data + HUB_GUILD
[ ] /setup-channel  (three league channels)
[ ] /setup-panel    (post the hub panel in #staff-only)
[ ] /pin-board      (permanent leaderboard channel)
[ ] /pin-checkout   (the pending-checkout channel)
[ ] /season-create  (starting_monday blank = next Monday) → 📅 /season-preview
[ ] /question-add or the panel's "Author tonight" for the first week's L1 questions
[ ] /scenario-set for week 1's strategy prompts
[ ] 24h later: zero failed posts in #staff-only, board card moved on its own
```

No step here asks anyone to type an id or a date into a chat and expect the bot to find it. Select
pickers do that.

---

## 3. LEAGUE 1 — WRITING QUESTIONS THAT SURVIVE 200 PLAYERS

One question = prompt + 2 to **4** options + the correct one + a tier + the points a correct answer
is worth. Authored from the panel or `/question-add` (answer the picker, not an id).

1. **Four options is a real limit, so design for it.** With only A–D, a wrong answer must still be
   *plausible*. Three obviously-fake fillers plus one real answer measures nothing — it's a free
   point. Put the near-miss in there: the weapon people *think* is the answer.
2. **Every question needs the patch it's true in, and an explanation.** Put the patch in the prompt
   text and the explanation in the notes — that's what the audit and any appeal will be argued from.
   A question whose answer moved in a balance patch is the fastest way to lose a season's credibility.
3. **Never ask anything a wiki page answers in one click.** "Which of these is a support weapon"
   tests knowledge. "What is X's reload in frames" tests typing speed and Googling.
4. **Set points per question, and let the tier fill it in.** The button shows the suggested band and
   takes the tier default on a blank, so the common case is one tap. Leave hard ones ungraded
   deliberately: an ungraded question zeroes the whole night rather than scoring an unfair one.
5. **Never reuse a scored question.** There is no `used_in` column and no auto-selection — the bot
   posts exactly what you authored for exactly that night. Reuse protection is a human habit: keep
   the bank in a sheet and mark it there, because a re-run question in a scored night will be in the
   group chat within 4 minutes.

**Calibration:** before a bank goes live, run it past 5 players of different ranks and publish the
answer rate. Keep easy in 60–85%, medium 35–60%, hard 10–30%. Outside that band it is either a bad
question or a mislabelled tier — and a hard question everyone gets is a tier error, not a great night.

---

## 4. LEAGUE 2 — SCENARIO CARD

```
SITUATION   ≤ 60 words. The decision, not the lore.
CONSTRAINTS credits remaining · slots left · weeks horizon · what you are defending
             ↑ without these four, the answer is a guess and the band is meaningless
WHAT TO SUBMIT  ≤ 300 words · one primary choice · what you give up to get it
FORBIDDEN       "it depends" with no commit · quoting a streamer's build
```

Bad prompt: *"What's the best way to spend coins early game?"* — no constraints, so every answer is
defensible and every low score is an argument.
Good prompt: *"3,500 credits, 4 slots, 6 weeks to a tournament, zero tanks. Spend it. What do you
give up?"* — the constraint *is* the question.

---

## 5. LEAGUE 3 — HANGAR REVIEW

Require all four in the submission text; players who omit them lose the band, and that's correct.

```
BUILD NAME · ROLE ASSUMED · COUNTER YOU EXPECT · WHAT YOU GIVE UP
```

**Mandatory one-liner for every review:** *"What is this build NOT doing?"* It separates people who
know a build from people who copied one. A review that cannot say what the build gives up is not
reviewing, it is describing — **Average 8**, not higher.

The trap this creates: if you can't state the credits-per-week assumed, the value-for-money criterion
is unscoreable and reviewers will split. That is a *prompt* bug — fix the card, not the score.
There is no automatic escalation here anymore by design: two reviewers who disagree talk, or a third
reads it. The bot only needs a number.

---

## 6. GRADING, BANDS AND THE ONE RULE ABOUT YOUR OWN NUMBER

```
POOR 2 · AVERAGE 8 · GOOD 15 · EXCELLENT 25      (L1 and Grand nights scale the same way they always did)
```

**Judge the answer that was submitted. Do not judge the player you expected.** The single most common
scoring error is the "good point, but" score — a correct, on-topic answer pulled down to Average
because it wasn't the answer *you* would have written. The band is about whether the player made a
case under the constraints, not whether they guessed your opinion.

- One reviewer, or two typing the same number into two accounts. **No rubric.** The band *is* the
  instrument.
- A second number averaged against the first **does not remove bias, it hides it**: both reviewers
  drift the same way on a player they like, and the average looks clean. Two independent reads are
  worth it only when they can disagree — then a third reader picks a band and the reason is written
  in the audit note.
- `⏭️ Skip / 0` is a real outcome. Zeroing a night is honest; scoring it from a bad read is not.
- **Flag ≠ score.** There is no AI detector in this bot and there never will be one — model prose is
  not reliably separable from a rushed human paragraph, and a false positive would cost someone their
  night on a statistic. Two things exist instead: the bot's own *deterministic* near-duplicate check
  (two entries tonight that read the same → both submitters see "⚠️ very similar to another entry"),
  and a flag a staff member records against a submission, which shows on the grading card as
  "⚠️ Flagged before awarding" and lives in the audit trail. Neither changes a number. Flagging is a
  note, not a verdict; if you flag it, you still read it and still award it.
- Appeals: one reply in `#appeals`, within 24h. A reviewer who didn't score it answers *with the band
  and the quote it earned*. A changed number is logged with the reason and visible in the export.
  Appeals exist to catch errors, not to reopen debate — no re-grade on a Friday.

---

## 7. SEASON END + ROLLOVER

Panel → 🛠️ Staff tools → `📋 Season end — DRY RUN`, read the output in #staff, then `APPLY`.
Dry run is the default for a reason: an award announced twice is an award withdrawn later.

Rollover does four things, in order: closes the old season, awards standings-night points, writes the
Hall of Fame, **strips every earlier season's champion role**. Then `/queue-payouts` turns the
standings into pending checkout cards. Staff pay those by hand and press `CLEAR` on each one.

The removal part matters more than it looks. A "Season 1 Champion" still on someone's profile in
Season 6 isn't an honour, it's noise — it makes the role unreadable and teaches new players that roles
mean nothing. Removal runs **even if the new season crowns nobody**, and a role the bot can't attribute
to a season it crowned is reported as `manual` and left alone: an admin's deliberate grant is not the
bot's to revoke. If you rename or replace a champion role between seasons, update that season's config
before the rollover.

Before finalising: every league night graded, zero open evenings, appeals closed 24h prior (state the
timestamp in the post). Then `/export` for the results post.

---

## 8. THINGS THAT WILL BITE YOU

1. **The board is the only truth.** Any screenshot from Discord is a snapshot of one player's claim.
   `/export` is what you argue from.
2. **Never edit a Discord message and call it a re-grade.** A changed number that exists only in chat
   is not a changed number — it must come through the grading flow so it lands in the audit trail.
3. **Do not grade Sunday nights on Sunday.** You'll be tired, you'll be inconsistent, and inconsistent
   judging is the one thing #appeals exists to catch.
4. **Do not re-invite, restart or redeploy to "refresh" commands.** A restart re-syncs and re-arms
   everything; if a command is missing, `HUB_GUILD` is unset and you're waiting on global propagation.
5. **A night with an ungraded question finalises to zero for everyone.** Fix the question, then
   finalise again. The bot refuses to post a half-scored leaderboard, and that refusal is the feature.
6. **Do not "fix" someone's balance by editing the ledger.** A hand edit leaves the payout queue
   pointing at the old number, so it pays what was correct before your edit. The service function
   (`adjust_points`) writes an audited row and re-queues the payout — but **it has no button yet**.
   Until it does, corrections belong in `#appeals` as a re-grade of the night that was wrong, not as a
   balance edit: re-grading is the path that is idempotent, logged and safe to run twice.
