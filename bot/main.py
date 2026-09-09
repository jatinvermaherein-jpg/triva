"""The Hub Knowledge Season bot.

  python3 bot/main.py --db hub.db            # run
  python3 bot/main.py --setup                # print the Discord portal checklist

Design contract (do not break): the bot posts, collects, computes, announces and
assigns roles. Humans author content and choose the points. No NLP, no judging,
no "AI decides the winner".
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import sys
from typing import Literal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import discord
from discord import app_commands
from discord.ext import commands

import db as D
import services as V
import ui

log = logging.getLogger("hub")
TICK_SECONDS = 20


class _SetupAskView(discord.ui.View):
    """Ephemeral picker. `role` is required so `guild` is injected into the
    callback - a RoleSelect with no guild cannot list the server's roles.

    Module scope on purpose: HubBot.setup_hook() adds it as a persistent view so a
    half-finished /setup survives a restart, and that runs before build_tree() ever
    executes. Defined inside build_tree() it was a module-level NameError - and it
    only surfaced on a real login, which is why no fake-backed suite saw it.
    """

    def __init__(self, conn):
        super().__init__(timeout=None)
        self.conn = conn
        self.add_item(ui.StaffRoleSelect(conn))

    async def on_error(self, interaction, error, item):
        # timeout=None means no default handler: without this a raise here is logged and
        # the picker just sits there looking tappable forever.
        log.exception("setup role picker failed", exc_info=error)
        await ui.reply(interaction, content=f"⚠️ Could not use that: `{type(error).__name__}`")


class HubBot(commands.Bot):
    def __init__(self, conn):
        intents = discord.Intents.default()
        intents.members = True          # role assignment + cached members
        super().__init__(command_prefix="!", intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.conn = conn
        self.hub_channel_id: int | None = None
        # evening_id -> last time we warned staff it could not post. In-memory on
        # purpose: a restart re-alerts once, which is correct after a restart.
        self._staff_warned: dict[int, dt.datetime] = {}

    # ------------------------------------------------------------------ startup
    async def setup_hook(self) -> None:
        ui.install(self, self.conn)
        # _SetupAskView is deliberately NOT registered here. A view is resumable only when
        # it is persistent, and this one carries no message_id, so `add_view` gave a false
        # promise: after a restart the picker is simply gone and /setup is re-run - which is
        # what the confirm button (SetupProvisionView, persistent, id-bearing) is for.
        # Panels refresh themselves after an award. They need the client (to
        # resolve channels), and views are built long before any interaction, so
        # the bot hands itself over once - never per-click.
        ui.bind_bot(self)
        # /setup names this "hub" (announcements); /setup-channel calls it
        # hub_channel_id. Both are honoured; whichever was configured last wins.
        self.hub_channel_id = (D.cfg(self.conn, "hub_channel_id")
                               or D.cfg(self.conn, "hub:hub"))
        self.loop.create_task(self.clock())
        await self.backfill()
        log.info("registered %d persistent views; %d evenings pending",
                 len(ui.PERSISTENT_VIEWS), self.conn.execute(
                     "SELECT COUNT(*) c FROM evening WHERE status='scheduled'").fetchone()["c"])

    async def on_ready(self):
        log.info("logged in as %s (%s)", self.user, self.user.id)
        guild_id = os.environ.get("HUB_GUILD", "").strip()
        try:
            if guild_id:
                # Guild-scoped sync is instant. A global sync can take up to an
                # hour, which on a fresh deploy reads as "the bot is broken".
                g = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                log.info("commands synced to guild %s", guild_id)
            else:
                await self.tree.sync()
        except discord.HTTPException:
            log.warning("command sync failed; will retry on next start")
        except ValueError:
            log.warning("HUB_GUILD=%r is not a snowflake; falling back to global sync",
                        guild_id)

    # ------------------------------------------------------------- the scheduler
    async def clock(self) -> None:
        """Posts each evening's cards at 16:00 IST and locks answers on time.

        Nothing here decides a point. The only automation is *when* content
        appears and when the window shuts - because those are the two things a
        human being asleep gets wrong.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.tick()
            except Exception:                                  # noqa: BLE001
                log.exception("clock tick failed; continuing")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: dt.datetime | None = None) -> dict:
        now = V.ist(now or dt.datetime.now(V.IST))
        opened, locked = [], []
        # channel_id is resolved inside post_evening from config; the evening row
        # itself only carries one once it has actually posted.
        due = self.conn.execute(
            "SELECT e.* FROM evening e JOIN season s ON s.id=e.season_id "
            "WHERE e.status='scheduled' AND e.opens_at<=? AND s.status='active' "
            "ORDER BY e.day, e.league", (now.isoformat(timespec="seconds"),)).fetchall()
        for ev in due:
            if await self.post_evening(dict(ev)):
                opened.append(ev["id"])
        stale = self.conn.execute(
            "SELECT * FROM evening WHERE status='open' AND closes_at<=?",
            (now.isoformat(timespec="seconds"),)).fetchall()
        for ev in stale:
            V.close_evening(self.conn, ev["id"], None, reason="auto timer")
            locked.append(ev["id"])
            await self._edit_evening_cards(ev["id"])
        return {"opened": opened, "locked": locked}

    async def backfill(self, now: dt.datetime | None = None) -> int:
        """If the bot was DOWN during an evening, post it anyway. A missing night
        is worse than a late one - players cannot tell 'cancelled' from 'the bot
        broke', and both cost the same trust.

        Window is a rolling 24h on the full timestamp, NOT a date comparison: a
        bot that restarts at 00:30 must still see yesterday's 16:00 evening. Also
        picks up 'open' rows stranded by a crash (posted, never locked), and locks
        those instead of double-posting them.
        """
        now = V.ist(now or dt.datetime.now(V.IST))
        since = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        until = now.isoformat(timespec="seconds")
        scheduled = self.conn.execute(
            "SELECT e.* FROM evening e JOIN season s ON s.id=e.season_id "
            "WHERE ((e.status='scheduled' AND NOT EXISTS (SELECT 1 FROM question q "
            "  WHERE q.evening_id=e.id AND q.correct_option IS NOT NULL)) "
            " OR (e.status='open')) AND e.opens_at<=? AND e.opens_at>=? "
            "AND s.status='active' ORDER BY e.day, e.league LIMIT 9",
            (until, since)).fetchall()
        posted = 0
        recovered_ids: set[int] = set()
        for ev in scheduled:
            log.warning("recovering evening %s %s (%s)", ev["day"], ev["league"], ev["status"])
            if ev["status"] == "open":
                # posted before the crash: give it a window, do not double-post
                if V.parse_iso(ev["closes_at"]) <= now:
                    await self._extend_window(ev["id"], now)
                    await self._edit_evening_cards(ev["id"])
                    posted += 1
                continue
            if await self.post_evening(dict(ev), backfill=True, now=now):
                posted += 1
                recovered_ids.add(ev["id"])
        stranded = self.conn.execute(
            "SELECT * FROM evening WHERE status='open' AND message_id IS NOT NULL "
            "AND closes_at<=?", (until,)).fetchall()
        for ev in stranded:
            if ev["id"] in recovered_ids:
                continue          # posted seconds ago by this very method
            log.warning("evening %s %s was opened but never closed (crash?) - locking",
                        ev["day"], ev["league"])
            V.close_evening(self.conn, ev["id"], None, reason="recovered after downtime")
            await self._edit_evening_cards(ev["id"])
        return posted + len(stranded)

    # ------------------------------------------------------------ card posting
    async def _extend_window(self, evening_id: int,
                            now: dt.datetime | None = None) -> None:
        """A recovered evening gets its FULL original window, measured from now.

        Without this the bot posts a card whose deadline passed hours ago, every
        answer is rejected, and the night is silently destroyed - strictly worse
        than not posting. Same length, same speed-bonus curve, so the night stays
        comparable to everyone else's.
        """
        # Re-read, never trust the row the caller handed us: tick() may have
        # locked this evening between the query and now.
        fresh = self.conn.execute("SELECT * FROM evening WHERE id=?", (evening_id,)).fetchone()
        opens, closes = V.parse_iso(fresh["opens_at"]), V.parse_iso(fresh["closes_at"])
        length = max(dt.timedelta(seconds=60), closes - opens)
        # ONE clock per operation. Using the real wall clock here while the caller
        # reasons about `now` is what let a recovered evening be "expired" the
        # instant it was posted.
        new_open = V.ist(now or dt.datetime.now(V.IST))
        with self.conn:
            self.conn.execute("UPDATE evening SET opens_at=?, closes_at=?, status='open' "
                              "WHERE id=?",
                              (new_open.isoformat(timespec="seconds"),
                               (new_open + length).isoformat(timespec="seconds"), evening_id))
            # shift every question deadline by the same delta so the speed-bonus
            # curve is measured from the moment cards actually appear
            delta = new_open - opens
            rows = self.conn.execute("SELECT id, answer_deadline FROM question "
                                     "WHERE evening_id=?", (evening_id,)).fetchall()
            for r in rows:
                new_deadline = V.parse_iso(r["answer_deadline"]) + delta
                self.conn.execute("UPDATE question SET answer_deadline=? WHERE id=?",
                                  (new_deadline.isoformat(timespec="seconds"), r["id"]))
        D.audit(self.conn, "evening.window_recovered", None, evening_id,
                {"opens": fresh["opens_at"], "closes": fresh["closes_at"]},
                {"opens": new_open.isoformat(timespec="seconds"),
                 "reason": "backfill after downtime"})

    async def post_evening(self, ev: dict, backfill: bool = False,
                           now: dt.datetime | None = None) -> bool:
        """Open one evening by posting its cards. Returns False if it is still
        'scheduled' and will be retried, True once it is genuinely 'open'.

        The order here is the invariant: **content is checked and sent, then the
        status flips.** The first version opened the evening before checking, so an
        L1 night with no questions became `open` with zero cards in the channel —
        submissions were accepted against nothing, the scheduler stopped retrying it
        (its query only looks at `scheduled`), and the night had to be noticed and
        fixed by hand, mid-season, from a log line nobody reads.
        """
        # The channel_id fallback is how a night re-posts to wherever it went before
        # after the bot is moved between servers. `or 0` there was a real hazard: it
        # asked for channel 0 when the evening had NEVER posted, and any permissive
        # channel resolver answers that - so a bot with no channel config at all
        # would "post" to a channel that does not exist and mark the night open.
        channel = await self._resolve_channel(ev)
        if channel is None and ev["channel_id"]:
            channel = self.get_channel(ev["channel_id"])
        if channel is None:
            log.error("evening %s: no channel for league %s — /setup mode:STATUS will "
                      "say which id is dead", ev["id"], ev["league"])
            await self._tell_staff(
                f"⚠️ **{ev['day']} {ev['league'].upper()} did not open** — no channel is "
                f"wired. Run /setup mode:STATUS.", ev["id"])
            return False

        if ev["league"] == "l1":
            qs = self.conn.execute(
                "SELECT * FROM question WHERE evening_id=? ORDER BY ordinal",
                (ev["id"],)).fetchall()
            if not qs:
                log.error("evening %s has no questions authored - nothing posted", ev["id"])
                # NOT opened, NOT consumed: still 'scheduled', so the next tick tries
                # again the moment staff author the night. A night that quietly becomes
                # "open with nothing in it" is worse than a night that visibly never
                # started, because the second one loses points for the whole league.
                await self._tell_staff(
                    f"⚠️ **{ev['day']} 📚 L1 could not open** — no questions authored for "
                    f"it. Add them with `Author tonight` on the panel and it will post on "
                    f"the next tick. Nothing has been scored, so nobody is penalised.",
                    ev["id"])
                return False
        else:
            qs = []

        if backfill:
            await self._extend_window(ev["id"], now)
        V.open_evening(self.conn, ev["id"], channel.id, None)
        if backfill:
            # Say it out loud. A card that appears hours late with no explanation
            # reads as a broken season; one that says why reads as a server that
            # recovered. Players forgive late; they do not forgive silence.
            await channel.send(embed=discord.Embed(
                colour=discord.Colour.gold(),
                description="⚠️ **Late start** — the bot was offline when this evening was "
                            "due. Everyone gets a **fresh full window from right now**, so "
                            "this night counts exactly like a normal one."))
        if ev["league"] == "l1":
            for q in qs:
                opts = json.loads(q["options"])
                e = ui.question_embed(self.conn, q, ev)
                msg = await channel.send(embed=e, view=ui.answer_view(self.conn, q["id"], opts))
                V.set_question_message(self.conn, q["id"], msg.id)
                await channel.send(content="**Staff**",
                                   embed=discord.Embed(description=(
                                       f"Question {q['ordinal']} · tier `{q['tier']}` — "
                                       f"tap the correct option after the timer."),
                                       colour=discord.Colour.dark_grey()),
                                   view=ui.question_grade_view(self.conn, q["id"], len(opts), opts))
        else:
            e = discord.Embed(title=f"{ui.LEAGUE_META[ev['league']][0]} {ev['day']} · "
                                    f"{ev['league'].upper()}",
                              description=D.cfg(self.conn, f"prompt:{ev['day']}:{ev['league']}",
                                                "_Staff: post the scenario/hangar card in this "
                                                "thread, then players submit below._"),
                              colour=ui.LEAGUE_META[ev["league"]][2])
            e.set_footer(text=f"Window closes <t:{ui._ts(ev['closes_at'])}:F> · "
                              f"one submission, editable until then · {ui.HONESTY_NOTE}")
            msg = await channel.send(embed=e, view=ui.submit_view(self.conn, ev["id"]))
            self.conn.execute("UPDATE evening SET message_id=? WHERE id=?", (msg.id, ev["id"]))
        D.audit(self.conn, "evening.autopost", None, ev["id"], None,
                {"league": ev["league"], "backfill": backfill})
        return True

    async def _tell_staff(self, text: str, evening_id: int | None = None) -> None:
        """Rate-limited by design: tick() runs every 20 SECONDS and an unopened
        night stays 'scheduled' until staff author it, so an unthrottled warning is
        180 identical pings an hour in the one channel people rely on for real
        problems. One per night per hour, which is nagging, not spam."""
        if evening_id is not None:
            now = V.ist(dt.datetime.now(V.IST))
            warned = getattr(self, "_staff_warned", None)
            if warned is None:
                warned = self._staff_warned = {}      # MUST be stored on the bot:
            last = warned.get(evening_id)             # `getattr(self, x, {})` hands
            if last and (now - last) < dt.timedelta(hours=1):
                return                                # back a NEW dict every call,
            warned[evening_id] = now                  # so nothing is ever throttled
        await self._staff_send(text)

    async def _staff_send(self, text: str) -> None:
        """Shout into #staff-only when a night cannot run.

        A silent `return False` in a scheduler is the worst possible failure: the
        operator sees nothing, the players see nothing, and the season quietly loses
        a night. If /setup has not been run there is no staff channel to shout into,
        so this degrades to the log line above rather than raising inside the loop.
        """
        chan_id = D.cfg(self.conn, V._hubkey("channel", "staff"))
        if not chan_id:
            return
        try:
            chan = self.get_channel(chan_id) or await self.fetch_channel(chan_id)
            if chan is not None:
                await chan.send(content=text)
        except (discord.DiscordException, AttributeError):
            pass      # never let a failed warning abort the tick

    async def _resolve_channel(self, ev):
        # order matters: a hand-set /setup-channel beats provisioning, which beats
        # the announcements fallback. So /setup works with zero manual wiring, and an
        # owner who points a league somewhere else is never overridden on restart.
        configured = (D.cfg(self.conn, f"channel:{ev['league']}")
                       or D.cfg(self.conn, V._hubkey("channel", f"league_{ev['league']}"))
                       or self.hub_channel_id)
        if not configured:
            return None
        chan = self.get_channel(configured)
        if chan is None:
            try:
                chan = await self.fetch_channel(configured)
            except discord.HTTPException:
                chan = None
        return chan

    async def _edit_evening_cards(self, evening_id: int) -> None:
        """Lock the visible state. We only edit the EMBED, never strip the view -
        `message.edit(view=empty)` is rejected by Discord, and stale buttons are
        already harmless because submit_answer checks evening status."""
        rows = self.conn.execute(
            "SELECT q.id, q.message_id, e.channel_id FROM question q "
            "JOIN evening e ON e.id=q.evening_id WHERE q.evening_id=? "
            "AND q.message_id IS NOT NULL", (evening_id,)).fetchall()
        for r in rows:
            chan = self.get_channel(r["channel_id"])
            if chan is None:
                continue
            try:
                msg = await chan.fetch_message(r["message_id"])
            except Exception:                                   # noqa: BLE001
                # Deleted message, missing access, a channel moved since: a cosmetic
                # re-render must NEVER be able to break grading or a recovery.
                continue
            try:
                q = self.conn.execute("SELECT * FROM question WHERE id=?", (r["id"],)).fetchone()
                ev = self.conn.execute("SELECT * FROM evening WHERE id=?",
                                       (evening_id,)).fetchone()
                await msg.edit(embed=ui.question_embed(self.conn, q, dict(ev)))
            except Exception:                                   # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# the handful of slash commands that genuinely need to exist
# --------------------------------------------------------------------------- #

LEAGUE_CHOICES = [app_commands.Choice(name="📚 League 1 — Knowledge", value="l1"),
                  app_commands.Choice(name="⚔️ League 2 — Strategy", value="l2"),
                  app_commands.Choice(name="🔧 League 3 — Hangar", value="l3")]


def build_tree(bot: HubBot) -> None:

    @bot.tree.command(description="Post The Hub panel with tonight's leagues")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_panel(i: discord.Interaction, channel: discord.TextChannel):
        await i.response.defer(ephemeral=True)   # posts to another channel before answering
        D.set_cfg(bot.hub_conn, "hub_channel_id", channel.id)
        e = ui.hub_embed(bot.hub_conn)
        msg = await channel.send(embed=e, view=ui.HubPanelView(bot.hub_conn))
        bot.hub_channel_id = channel.id
        await ui.reply(i, content=f"Pinned panel is live in {channel.mention} "
                                  f"(message {msg.id}).")

    # ------------------------------------------------------------------ #
    # /setup - the one command. Ask a question, create the whole server.
    # ------------------------------------------------------------------ #
    @bot.tree.command(description="Create every channel and role The Hub needs")
    # Without this, a DM invocation reaches setup_plan_embed with interaction.guild None.
    @app_commands.guild_only()
    # Literal, not Choice: discord.py has no supported annotation for an optional
    # `app_commands.Choice[str]` parameter (it raises "unsupported type annotation
    # <class Interaction>" while building the tree). Literal gives the same dropdown.
    @app_commands.describe(mode="PLAN reads it, STATUS audits the wiring, SETUP builds it")
    @app_commands.checks.has_permissions(manage_roles=True, manage_channels=True)
    async def setup(i: discord.Interaction,
                    mode: Literal["plan", "status", "setup"] = "plan") -> None:
        if i.guild is None:                      # guild_only() should prevent this
            return await i.response.send_message("Run this **in the server**, not in a DM.",
                                                 ephemeral=True)
        conn = i.client.conn
        # Defer FIRST, before touching the database. Every branch below reads config and
        # walks the guild; a slow WAN round trip to Supabase that slips past 3s makes
        # Discord forget the interaction entirely, and the answer then 404s no matter how
        # correct it is. "Thinking..." is always cheaper than an unanswerable command.
        await i.response.defer(ephemeral=True)
        if mode == "plan":
            return await ui.reply(i, embed=ui.setup_plan_embed(
                i, conn, D.cfg(conn, "staff_role_id") or 0))
        if mode == "status":
            return await ui.reply(i, embed=_setup_status(conn, i.guild))
        view = _SetupAskView(conn)
        if D.cfg(conn, "staff_role_id"):
            view.stop()      # already wired; SETUP would only re-confirm it
        await ui.reply(i, view=view,
                       content="Which role should **run the quizzes**? Its members get every "
                               "staff control, and the id is stored - renaming that role later "
                               "changes nothing.\nI need **Manage Roles** and **Manage "
                               "Channels** to do this, and nothing else.")

    @setup.error
    async def setup_error(i: discord.Interaction, error):
        if isinstance(error, discord.app_commands.MissingPermissions):
            return await ui.reply(
                i, content="⚠️ That needs **Manage Roles** + **Manage Channels** on "
                            "your account, not on the bot. Administrator is not required.")
        if isinstance(error, discord.NotFound) or isinstance(error.__cause__, discord.NotFound):
            # The command ran fine; only the channel to answer through was gone. Say so in
            # a sentence instead of a traceback on a race nobody caused.
            log.warning("setup answered too late - the interaction had already expired")
            return await ui.reply(
                i, content="⚠️ My reply could not be delivered (the command timed out). "
                           "Nothing was created - run /setup again; a gateway reconnect is the "
                           "usual cause and the second try always lands.")
        log.exception("setup failed", exc_info=error)
        await ui.reply(i, content=f"⚠️ Setup failed: `{type(error).__name__}`")


def _setup_status(conn, guild) -> discord.Embed:
    """Re-resolve every stored id and report anything that no longer resolves.

    This is the safety net for the id-based design: if an admin deletes a channel
    outside the bot, the stored id points at nothing, and the ONLY way to notice is
    to look. So look.
    """
    e = discord.Embed(title="Hub wiring — by Discord id", colour=0x95a5a6)
    bad, ok = [], []
    for key, name, staff_only, _slow, why in V.PROVISION_CHANNELS:
        cid_ = D.cfg(conn, V._hubkey("channel", key))
        chan = guild.get_channel(cid_) if (cid_ and guild) else None
        if chan is None:
            bad.append(f"✖️ **#{name}** — {why}"
                       + (f" (stored id `{cid_}` is dead)" if cid_ else " (never created)"))
        else:
            ok.append(f"✅ **#{chan.name}** — `{chan.id}`"
                      + (" · renamed from the original, still wired"
                         if chan.name != name else ""))
    for key, name, _c, _h in V.PROVISION_ROLES:
        rid = (D.cfg(conn, "staff_role_id") if key == "staff"
               else D.cfg(conn, V._hubkey("role", key)))
        role = guild.get_role(rid) if rid else None
        if rid and role is None:
            bad.append(f"✖️ role **{name}** — stored id `{rid}` no longer exists")
        elif role is not None:
            ok.append(f"✅ role **{role.name}** — `{role.id}`")
    desc = "\n".join(ok) or "*nothing is wired yet — run /setup mode:SETUP*"
    if bad:
        desc += "\n\n**Broken:**\n" + "\n".join(bad) + \
            "\n\nFix with `/setup mode:SETUP` (re-adopts or recreates), or " \
            "`/setup-channel` for one channel."
    e.description = desc[:4000]
    e.set_footer(text="Renaming a channel or role is NOT in the broken list on purpose: "
                      "the bot follows the id, so a new name is cosmetic.")
    return e


    @bot.tree.command(name="setup-channel", description="Set where each league posts")
    @app_commands.choices(league=LEAGUE_CHOICES)
    @app_commands.describe(league="Which league this channel hosts")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_channel(i: discord.Interaction, league: app_commands.Choice[str],
                            channel: discord.TextChannel):
        D.set_cfg(bot.hub_conn, f"channel:{league.value}", channel.id)
        await i.response.send_message(f"{league.value.upper()} now posts in {channel.mention}.",
                                      ephemeral=True)

    @bot.tree.command(name="season-create",
                      description="Create a 1-month season and its full calendar")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(name="e.g. Season 1",
                           start_day="YYYY-MM-DD — leave blank for next Monday",
                           weeks="4 = one month")
    async def season_create(i: discord.Interaction, name: str, start_day: str = "",
                            weeks: app_commands.Range[int, 1, 8] = 4):
        # Blank is the common case: the rotation always starts on Monday, so the
        # sensible default is "the next Monday" and nobody has to open a calendar.
        if start_day.strip():
            day = start_day.strip()
        else:
            today = dt.date.today()
            day = (today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)).isoformat()
        try:
            res = V.create_season(bot.hub_conn, name, day, weeks)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        nights = res["nights_per_league"]
        await i.response.send_message(
            f"📅 **{name}** · {res['evenings']} evenings from {day}\n"
            f"{' · '.join(f'{k.upper()} {v} nights' for k, v in nights.items())} · "
            f"Sunday {res['grand_final_day']} is the Grand (all three leagues).")

    @bot.tree.command(name="question-add",
                      description="Author a League 1 question (picks the night for you)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(prompt="The question text",
                           options="2-4 options separated by |",
                           tier="easy / medium / hard",
                           points="points for a correct answer (blank = tier default)")
    async def question_add(i: discord.Interaction, prompt: str = "", options: str = "",
                           tier: str = "medium", points: str = "", day: str = "",
                           league: str = "l1"):
        """Night-first, then the text. If you already know the night, pass day+league;
        if you do not, you get a picker - there is no id to look up.
        """
        if not prompt or not options:
            e = discord.Embed(title="📚 Which night?", colour=0x2f3136,
                              description="Pick the night, then fill the modal. "
                                          "No evening ids, no dates to type.")
            return await i.response.send_message(embed=e,
                                                 view=ui.PickNightView(bot.hub_conn, "question"),
                                                 ephemeral=True)
        if not day:
            return await i.response.send_message(
                "That needs a `day` (YYYY-MM-DD) — or leave prompt blank and pick the night "
                "from the list instead.", ephemeral=True)
        ev = bot.hub_conn.execute("SELECT id FROM evening WHERE day=? AND league=?",
                                  (day, league)).fetchone()
        if not ev:
            return await i.response.send_message(
                f"No {league.upper()} night on {day}. Under the rotation L1 plays Mon/Thu/Sun.",
                ephemeral=True)
        try:
            res = V.add_question(bot.hub_conn, ev["id"], None, prompt,
                                 [o.strip() for o in options.split("|") if o.strip()],
                                 tier=tier,
                                 points_per_correct=int(points) if points.strip().isdigit() else None)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        await i.response.send_message(
            f"✅ Q{res['ordinal']} queued ({tier}, closes {res['answer_deadline'][:16]}). "
            "Nothing players see until 16:00.", ephemeral=True)

    @bot.tree.command(name="scenario-set",
                      description="Set a League 2/3 night's prompt (picker, no dates)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(prompt="The card players see", day="YYYY-MM-DD (optional)",
                           league="l2 or l3 (optional)")
    async def scenario_set(i: discord.Interaction, prompt: str = "", day: str = "",
                           league: str = "l2"):
        if not prompt or not day:
            e = discord.Embed(title="⚔️ Which night?", colour=0x2f3136,
                              description="Pick the night, then write the card in the modal.")
            return await i.response.send_message(embed=e,
                                                 view=ui.PickNightView(bot.hub_conn, "prompt"),
                                                 ephemeral=True)
        try:
            res = V.set_scenario_prompt(bot.hub_conn, day, league, prompt,
                                        actor_id=i.user.id)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        note = ("The bot posts it at 16:00." if not res["posted"]
                else "This night already posted — use 🔄 on its card to re-render.")
        await i.response.send_message(f"✅ {league.upper()} prompt saved for {day}. {note}",
                                      ephemeral=True)

    @bot.tree.command(name="season-preview", description="List evenings and their state")
    @app_commands.checks.has_permissions(administrator=True)
    async def season_preview(i: discord.Interaction, day: str = ""):
        await i.response.defer(ephemeral=True)
        if day:
            rows = bot.hub_conn.execute(
                "SELECT id,league,status,opens_at FROM evening WHERE day=? ORDER BY league",
                (day,)).fetchall()
        else:
            rows = bot.hub_conn.execute(
                "SELECT id,league,status,opens_at FROM evening ORDER BY day,league LIMIT 12"
            ).fetchall()
        e = discord.Embed(title="Evenings", colour=0x2f3136)
        e.description = "\n".join(f"`{r['id']}` {r['league'].upper()} · {r['status']} · "
                                  f"{r['opens_at'][:16]}" for r in rows) or "*none*"
        await i.followup.send(embed=e, ephemeral=True)

    @bot.tree.command(name="pin-board",
                      description="Pin the permanent auto-updating leaderboard here")
    @app_commands.checks.has_permissions(administrator=True)
    async def pin_board(i: discord.Interaction):
        """Three views on one card: this month, the league tables, lifetime.
        It rewrites itself whenever points move - no refresh button needed."""
        await i.response.defer(ephemeral=True)
        msg = await i.channel.send(embed=ui.board_embed(bot.hub_conn, "season", None),
                                   view=ui.BoardView(bot.hub_conn))
        D.set_cfg(bot.hub_conn, f"board:{msg.id}", i.channel.id)
        await i.followup.send(f"📌 Leaderboard pinned here. It updates itself on every "
                              f"award and on season rollover.")

    @bot.tree.command(name="pin-checkout",
                      description="Pin the pending-checkout queue (staff pay, bot lists)")
    @app_commands.checks.has_permissions(administrator=True)
    async def pin_checkout(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        rows = V.pending_payouts(bot.hub_conn)
        view = ui.CheckoutView(bot.hub_conn) if rows else ui.no_controls()
        msg = await i.channel.send(embed=ui.checkout_embed(bot.hub_conn), view=view)
        D.set_cfg(bot.hub_conn, f"checkout:{msg.id}", i.channel.id)
        await i.followup.send(
            f"📌 Checkout queue pinned. {len(rows)} row(s) pending. "
            "The bot never pays: send the coins in-server, then press the row's button.")

    @bot.tree.command(name="queue-payouts",
                      description="Turn this season's standings into pending checkouts")
    @app_commands.checks.has_permissions(administrator=True)
    async def queue_payouts(i: discord.Interaction):
        res = V.queue_payouts(bot.hub_conn)
        await i.response.send_message(
            f"🧾 {res['created']} checkout(s) queued, {res['skipped']} already existed. "
            f"{V.pending_count(bot.hub_conn)} pending in total.", ephemeral=True)

    @bot.tree.command(name="tonight", description="Open tonight's leagues right now")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_tonight(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        n = 0
        for ev in V.todays_evenings(bot.hub_conn):
            if ev["status"] == "scheduled" and await bot.post_evening(ev):
                n += 1
        await i.followup.send(f"Posted {n} league card set(s) for today." if n
                              else "Nothing left to post today (or a channel is unset).",
                              ephemeral=True)

    @bot.tree.command(name="clock-tick", description="Run the scheduler immediately")
    @app_commands.checks.has_permissions(administrator=True)
    async def clock_tick(i: discord.Interaction):
        # tick() can open, lock and post several evenings: minutes, not milliseconds
        await i.response.defer(ephemeral=True)
        res = await bot.tick()
        await ui.reply(i, content=f"opened={res['opened'] or '—'} locked={res['locked'] or '—'}")

    @bot.tree.command(name="export", description="Download the season ledger as CSV")
    @app_commands.checks.has_permissions(administrator=True)
    async def export(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        rows = bot.hub_conn.execute(
            "SELECT l.day, l.league, l.player_id, l.points, a.reason, a.applied_by, a.ts "
            "FROM ledger l LEFT JOIN award a ON a.player_id=l.player_id "
            "AND a.evening_id=l.evening_id ORDER BY l.day, l.league, l.points DESC").fetchall()
        csv = "day,league,player_id,points,reason,awarded_by,ts\n" + "\n".join(
            ",".join(str(x) for x in tuple(r)) for r in rows)
        await i.followup.send(file=discord.File(fp=__import__("io").BytesIO(csv.encode()),
                                                filename="hub-ledger.csv"),
                              content=f"{len(rows)} ledger rows. Every point traceable.",
                              ephemeral=True)

    @bot.tree.error
    async def on_error(i: discord.Interaction, err: app_commands.AppCommandError):
        msg = str(err)
        if isinstance(err, app_commands.MissingPermissions) or "requires" in msg.lower():
            msg = "Administrator or **Hub Staff** only."
        # Was: `followup.send` whenever a response was already "done". On an EXPIRED
        # interaction that raises NotFound from inside the error handler - which is the
        # traceback that reached the log instead of an explanation that reached the user.
        if isinstance(err, discord.NotFound) or isinstance(err.__cause__, discord.NotFound):
            msg = ("my reply could not be delivered (the command timed out while I was "
                   "working). Nothing was changed - try again.")
        await ui.reply(i, content=f"⚠️ {msg}")


SETUP_NOTES = """
Discord portal checklist (no privileges needed, but you must own the server invite):
  1. https://discord.com/developers/applications → New Application
  2. Bot → Reset Token → copy it to HUB_TOKEN
  3. Bot → PRIVILEGED GATEWAYS → enable SERVER MEMBERS INTENT   (role assignment needs it)
     (do NOT enable Message Content: this bot reads buttons/modals, never messages)
  4. OAuth2 → URL Generator → scopes: bot + applications.commands
     permissions: Send Messages, Embed Links, Read Message History, Manage Roles,
                  Read Message/Server Context
  5. Open the generated URL, invite to The Hub.
  6. Invite with ONLY: Send Messages, Embed Links, Read Message History,
     View Channels, Manage Channels, Manage Roles, Mention Everyone.
     (Manage Channels + Manage Roles are what /setup needs to build the server.
      NOT Administrator. The bot never asks for more than it uses.)
  7. Run:  python3 bot/main.py --db hub.db     (or deploy on Railway, see README)
  8. In Discord, in order:
       /setup  mode:PLAN     read what it would create
       /setup  mode:SETUP    pick the ONE staff role; the bot creates the
                             category, 9 channels and 5 champion roles, and
                             stores every Discord id in its own database
       /setup-panel          the hub card, into #staff-only
       /pin-board            the permanent leaderboard, into #league-table
       /pin-checkout         the pending-checkout queue staff clear by hand
       /season-create        (blank start day = next Monday)
     /setup-channel still exists to point a league at a different channel than the
     one /setup made - it overrides, it is not a required step.
     Everything after this is buttons and modals. You never type an evening id, a
     player id or a channel id - the pickers carry them.

  Renaming afterwards is FREE. The bot follows ids, not names: rename #trivia-night
  to #quiz-night, recolour "Strategy Champion", move channels between categories,
  and /setup mode:STATUS will show them as wired (with a "renamed" note), never as
  broken. Deleting one is the only thing that breaks, and STATUS names it.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    def _default_db():
        # On Railway the persistent volume is mounted at /data; a DB written to
        # the build directory is deleted on every redeploy, which silently wipes
        # the season. Respect an explicit HUB_DB first, then /data, then CWD.
        # HUB_DB may instead be a postgres:// URL (Supabase), which is the case that
        # needs no volume at all - so it is returned verbatim and never given a fallback.
        env = (os.environ.get("HUB_DB") or "").strip()
        if env:
            return env
        return "/data/hub.db" if os.path.isdir("/data") else "hub.db"

    ap.add_argument("--db", default=_default_db())
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify config and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.setup:
        print(SETUP_NOTES)
        return 0
    token = os.environ.get("HUB_TOKEN", "").strip()
    # sqlite takes a Path; a postgres URL must stay a str, because pathlib collapses
    # "postgres://" to "postgres:/" and the URL would then be treated as a filename.
    try:
        target = (args.db if D.is_pg_target(args.db) else pathlib.Path(args.db))
    except (OSError, ValueError) as exc:
        # A 600-char JSON blob that carries no host reaches here, and pathlib answers
        # "File name too long" - an OSError that looks like a disk problem, not a config one.
        print(f"\n\u2717 HUB_DB is not a usable database target: {exc}\n"
              "  Paste Supabase's \"Connection URI\" (one line, starts postgresql://), or set\n"
              "  HUB_DB=/data/hub.db to use a SQLite file on a Volume.\n", file=sys.stderr)
        return 78
    try:
        conn = D.connect(target)
    except D.OperationalError as exc:
        # Railway restarts a non-zero exit forever, so the first log lines are the only
        # thing a staff member reads at 2am. Print the diagnosis, not a 40-line traceback.
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        print("\n\u2717 The bot could not open its database, so it will not start.\n"
              + "\n".join("  " + ln for ln in lines) + "\n", file=sys.stderr)
        return 78                                     # EX_CONFIG: a variable is wrong, not the code
    bot = HubBot(conn)
    build_tree(bot)
    if args.check:
        is_pg = D.is_pg_target(args.db)
        on_volume = str(args.db).startswith("/data/")
        print(f"db            : {args.db}"
              + (" (postgres)" if is_pg else " (sqlite, WAL)")
              + ("" if (is_pg or on_volume) else "  ⚠️ NOT on a volume"))
        if is_pg:
            # The whole point of the Postgres move: persistence is the server's job, so the
            # volume warning would be noise - and a false "you are safe" would be worse. Say
            # what actually protects the data here instead.
            print("                ✓ hosted Postgres: survives redeploys without a volume. "
                  "Keep HUB_DB out of git;\n                  the URL is the only credential "
                  "needed to rewrite a season, so use the pooler + a strong password.")
        elif not on_volume:
            print("                ⚠ Railway wipes the container filesystem on every "
                  "redeploy.\n                  Mount a volume at /data and set "
                  "HUB_DB=/data/hub.db, or point HUB_DB at a postgres:// URL.")
        print(f"persistent views: {len(ui.PERSISTENT_VIEWS)} registered for restart-proof dispatch")
        print(f"seasons       : {conn.execute('SELECT COUNT(*) c FROM season').fetchone()['c']}")
        print(f"token set     : {bool(token)}")
        return 0
    if not token:
        print("HUB_TOKEN is not set. Get it from the Discord developer portal, then:\n"
              "  export HUB_TOKEN=...\n  python3 bot/main.py --db hub.db\n"
              "Run --setup for the full checklist.")
        return 2
    bot.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
