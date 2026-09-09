"""Button UI for The Hub Knowledge Season.

Two rules make every panel restart-proof, and they are the whole point of this file:

  1. All state lives in the custom_id string (`hub:ans:812:3` = question 812, option 3)
     or in SQLite. A View instance NEVER holds an event id, a page index, or "the entry
     I'm currently looking at". So a panel pinned on day 1 and clicked on day 20 after
     four restarts routes to identical code.
  2. Every view is timeout=None with hand-written custom_ids, registered with
     bot.add_view() BEFORE bot.run() (see install()). Discord.py 2.7.1 has no
     PersistentView class - `is_persistent()` (no timeout + explicit custom_ids on all
     children) is what actually enables routing.

Because buttons must exist at class-definition time, an option row is a FIXED set of
6 buttons; unused ones render disabled. That is deliberate: dynamic per-question
buttons either break persistence or need DynamicItem template machinery that cannot be
verified offline. Six fixed slots, of which you normally use 2-6, is boring and correct.
"""
from __future__ import annotations

import datetime as dt
import logging

import discord

import services as V
from loader import load_db

# Same logger main.py writes to, so an operator watching one stream sees the whole story.
# ui.py needs it for exactly one thing: telling the log when an answer was DROPPED, which
# is otherwise invisible (Discord shows "this interaction failed" and nothing else does).
log = logging.getLogger("hub")

MAX_OPTIONS = 4          # A-D on ONE row. 5 is the hard per-row cap and the clear
                         # button takes a slot; discord.py will not warn you - the
                         # API rejects the message at SEND time, mid-evening.
BUTTONS_PER_ROW = 5
HUB_ROLE = "Hub Staff"
LEAGUE_META = {
    "l1": ("📚", "League 1 — Knowledge", discord.Colour.blurple()),
    "l2": ("⚔️", "League 2 — Strategy", discord.Colour.red()),
    "l3": ("🔧", "League 3 — Hangar", discord.Colour.orange()),
}
PHASE = {"scheduled": "⏳ Scheduled", "open": "🟢 **OPEN**",
         "locked": "🔒 Locked — awaiting staff", "graded": "✅ Graded",
         "void": "🚫 Void"}
LETTERS = "ABCDEF"


# --------------------------------------------------------------------------- #
# id codec  (the only place that knows how a custom_id is shaped)
# --------------------------------------------------------------------------- #

def cid(view: str, *parts) -> str:
    return ":".join(("hub", view, *(str(p) for p in parts)))


def parse_cid(custom_id: str, expecting: int) -> tuple[str, ...] | None:
    """`hub:ans:812:3` -> ('812','3'). Returns None for a malformed id."""
    bits = (custom_id or "").split(":")
    if len(bits) != 2 + expecting or bits[0] != "hub":
        return None
    return tuple(bits[2:])


def ints(custom_id: str, expecting: int) -> tuple[int, ...] | None:
    got = parse_cid(custom_id, expecting)
    if got is None:
        return None
    try:
        return tuple(int(x) for x in got)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# base view
# --------------------------------------------------------------------------- #

class HubView(discord.ui.View):
    """Never expires, never stores state, explains itself on failure.

    Permission is enforced TWICE on purpose: interaction_check (what discord.py's
    dispatcher runs) AND self._authorized() at the top of every staff callback.
    A stale/inline view or a refactor that bypasses interaction_check must never be
    able to let a player grade their own night.
    """

    def __init__(self, conn, *, staff: bool = False):
        super().__init__(timeout=None)
        self.conn = conn
        self.staff_only = staff

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.staff_only and not is_staff(interaction.user):
            await interaction.response.send_message(
                "That control is for **Hub Staff**.", ephemeral=True)
            return False
        return True

    async def _authorized(self, interaction) -> bool:
        """Second gate. Returns False after telling the user why.

        Delegates to gate(), the same check a bare Item (StaffRoleSelect) has to use - the
        divergence between these two is what let `self._authorized` onto a non-View class.
        """
        if gate(interaction, getattr(self, "conn", None)):
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "That control is for **Hub Staff**.", ephemeral=True)
        return False

    async def on_error(self, interaction, error, item):
        try:
            await interaction.response.send_message(
                f"⚠️ That control failed: `{type(error).__name__}: {error}`",
                ephemeral=True)
        except discord.HTTPException:
            pass


def is_staff(user, staff_role_id: int | None = None) -> bool:
    """Staff = administrator, or a holder of the role /setup stored BY ID.

    Matching on the NAME alone is what makes a rename break the bot: an owner who
    renames "Hub Staff" to "Quiz Team" would silently revoke every grading button -
    and, worse in the other direction, anyone could mint a role called "Hub Staff"
    and gain them. So the stored id wins whenever it exists; the name check is only
    a fallback for a server that has never run /setup.
    """
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    roles = list(getattr(user, "roles", []) or [])
    if staff_role_id:
        return int(staff_role_id) in {int(r.id) for r in roles}
    return any(getattr(r, "name", None) == HUB_ROLE for r in roles)


def gate(interaction, conn, *, staff_only: bool = True, owner_bypass: bool = False) -> bool:
    """May this interaction proceed? Module level, because BOTH a View and a bare Item
    need to ask, and an Item has no `_authorized` to inherit.

    owner_bypass is for controls that only exist on an ephemeral message: whoever ran the
    command is allowed even before `staff_role_id` is stored. Without it the very first
    /setup deadlocks - the picker's "confirm" button is a staff-only HubView, and at that
    moment nobody is staff yet.
    """
    if not staff_only:
        return True
    user = interaction.user
    if owner_bypass:
        author = getattr(getattr(interaction, "message", None), "author", None)
        if author is not None and user.id == author.id:
            return True
    return is_staff(user, cfg_safe(conn, "staff_role_id"))


def cfg_safe(conn, key, default=None):
    """cfg() that never raises on a connection object this module may not fully own."""
    try:
        return V.dbmod.cfg(conn, key, default)
    except Exception:
        return default


def install(bot, conn) -> None:
    """Register every persistent view BEFORE login, with no message_id, so
    dispatch works for panels posted days ago. Views here must be constructible
    with only (conn,) - that is why they take no ids."""
    for cls in PERSISTENT_VIEWS:
        bot.add_view(cls(conn))
    bot.hub_conn = conn


PERSISTENT_VIEWS: list[type] = []


def persistent(cls):
    PERSISTENT_VIEWS.append(cls)
    return cls


class StaffRoleSelect(discord.ui.RoleSelect):
    """The ONE question /setup asks. Everything else is derived from it.

    No placeholder: discord.py validates the string and rejects the message at send
    time, mid-flow, so the guidance lives in the command description instead.
    """

    def __init__(self, conn):
        super().__init__(custom_id=cid("st", "role"), min_values=1, max_values=1)
        self.conn = conn

    async def callback(self, interaction: discord.Interaction):
        # NOT self._authorized(): that is a HubView method and this is an Item. It raised
        # AttributeError on every real click, so the picker looked dead. See ui.gate().
        if not gate(interaction, self.conn, owner_bypass=True):
            await interaction.response.send_message(
                "Only the person who ran `/setup` can pick the staff role.", ephemeral=True)
            return
        # ACKNOWLEDGE FIRST, WORK SECOND - the same rule /setup follows, and the one this
        # callback broke. Measured on a real click: 16 statements run before the answer
        # (the 1 write below + the 15 config reads setup_plan_embed makes through
        # provision_plan), and every one is a Supabase round trip. At 1.4 ms total on local
        # SQLite that is free; on a WAN link it is 16 x RTT, so any link slower than
        # ~190 ms per round trip - Railway and Supabase in different regions, say - walks
        # past Discord's 3-second acknowledgement window. Past that the token is not merely
        # late, it is GONE: every way of answering then raises `404 Not Found (error code:
        # 10062): Unknown interaction`, which is what the deploy log showed as `setup role
        # picker failed`. "A select is already an answer" is true of the click, not of a
        # callback that queries a database before replying.
        # defer() on a component sends DEFERRED_UPDATE_MESSAGE - it acknowledges with no
        # visible change and buys the full 15 minutes - so the answer that follows must
        # EDIT this message (ui.reply does) rather than send a new one.
        await interaction.response.defer()
        role = self.values[0]
        # The confirm button is staff-only, and until provision() runs nothing is stored -
        # so record the pick now. provision() re-writes the same key from the same value;
        # this only exists to let the picker's own author click it.
        try:
            V.dbmod.set_cfg(self.conn, "staff_role_id", int(role.id))
        except Exception:
            pass                       # a failed convenience write must not eat the click
        self._stop_the_picker()
        # Never a bare edit_message(): ui.reply picks the method that is still legal for
        # this interaction and cannot raise on a dead token. clear_content because the
        # picker's prompt text is superseded by the plan below it.
        if await reply(interaction, embed=setup_plan_embed(interaction, self.conn, role.id),
                       view=SetupProvisionView(self.conn, role.id),
                       clear_content=True) == "dropped":
            log.warning("setup picker answered too late - the interaction had already "
                        "expired; the role id WAS stored, so /setup mode:PLAN still works")

    def _stop_the_picker(self) -> None:
        """Disable the picker we just consumed, so the message cannot be re-used.

        `self.view`, NOT `interaction.view`. discord.py puts the live view on the Item
        (`View.add_item` sets `item._view`; the dispatcher runs
        `item.view._dispatch_item(item, interaction)`), and `discord.Interaction` has no
        `view` attribute at all - so the previous `getattr(interaction, "view", None)` was
        None on every real click, and the picker stayed tappable after being answered.
        Two views can hold this class (the one /setup built and the copy install()
        registered at boot); `self.view` is the one that was actually clicked.

        Safe to call from inside the callback: View.stop() resolves the view's stopped
        future, cancels its timeout task and drops it from the view store - it does not
        cancel the task this callback is running in.
        """
        v = self.view
        if v is not None:
            v.stop()


def setup_plan_embed(interaction, conn, staff_role_id: int) -> discord.Embed:
    """What /setup WOULD do, against the real guild, having touched nothing.

    A dry run of the one irreversible-feeling action in the whole bot, and the
    place where "these already exist, I will adopt them" is shown before it happens.
    """
    guild = interaction.guild
    if guild is None:
        # Reached only as a backstop: /setup is @guild_only now, so Discord refuses the
        # DM itself. Before that guard existed this line was `AttributeError:
        # 'NoneType' object has no attribute 'roles'` on every DM invocation.
        raise ValueError("setup_plan_embed needs a guild")
    plan = V.provision_plan(conn)
    names_role = {r.name for r in guild.roles}
    names_chan = {c.name for c in guild.channels}
    lines = []
    for r in plan["roles"]:
        if r["key"] == "staff":
            lines.append(f"🎭 **role** {r['name']} — replaced by the staff role you "
                         f"picked, unless it does not exist yet")
            continue
        tag = "♻️" if (r["id"] or r["name"] in names_role) else "🆕"
        lines.append(f"{tag} **role** {r['name']}")
    lines.append("")
    if plan["category"]["id"] or V.CATEGORY_NAME in names_chan:
        lines.append(f"♻️ **category** {V.CATEGORY_NAME} — adopting the existing one")
    else:
        lines.append(f"🆕 **category** {V.CATEGORY_NAME}")
    for c in plan["channels"]:
        tag = "♻️" if (c["id"] or c["name"] in names_chan) else "🆕"
        lines.append(f"{tag} **#{c['name']}** — {c['why']}"
                     + (" · **staff only**" if c["staff_only"] else ""))
    e = discord.Embed(title="Setup plan — nothing has been created", colour=0x3498db)
    e.description = "\n".join(lines)
    e.set_footer(text="Every id is stored, so renaming or reordering any of these later "
                      "cannot break the bot. I need Manage Roles + Manage Channels only.")
    return e


class _GuildWorkspace:
    """Adapts a discord.py Guild to the tiny protocol services.provision() speaks.

    Deliberately THIN: every Discord API call lives here, every decision about what
    to create lives in services.py where it can be tested without a guild.
    """

    def __init__(self, guild, conn=None):
        self.guild, self.conn = guild, conn

    def get_role(self, role_id):
        return self.guild.get_role(int(role_id)) if role_id else None

    def find_role(self, name):
        return next((r for r in self.guild.roles if r.name == name), None)

    def get_channel(self, channel_id):
        return self.guild.get_channel(int(channel_id)) if channel_id else None

    def find_channel(self, key, name):
        """By name only. Stored-id lookups go through get_channel(), because only
        the caller knows which config key holds which id."""
        want = str(name).lower()
        return next((c for c in self.guild.channels if str(c.name).lower() == want), None)

    async def create_role(self, *, name, colour=0, hoist=False, reason=None):
        # No permissions on purpose: a champion role is decoration. It must NOT be
        # able to kick, ban or edit anything just because the bot made it.
        return await self.guild.create_role(name=name, colour=discord.Colour(colour),
                                            hoist=hoist, reason=reason)

    async def create_category(self, *, name, overwrites=None, reason=None):
        return await self.guild.create_category(name=name,
                                                overwrites=_to_overwrites(self.guild, overwrites),
                                                reason=reason)

    async def create_channel(self, *, name, category=None, overwrites=None,
                             slowmode=0, reason=None):
        kw = {"reason": reason, "category": category}
        if overwrites:
            kw["overwrites"] = _to_overwrites(self.guild, overwrites)
        if slowmode:
            kw["slowmode_delay"] = int(slowmode)
        return await self.guild.create_text_channel(name, **kw)


def _to_overwrites(guild, logical):
    """logical dict -> {Role: PermissionOverwrite}. Only ever read/write/view."""
    if not logical:
        return {}
    out = {}
    who = {"everyone": guild.default_role, "staff": guild.get_role(logical["staff_id"])}
    for key, perms in logical.items():
        if key == "staff_id" or key not in who or who[key] is None:
            continue
        # exactly three flags: see it, type in it, scroll it. No manage_channels,
        # no manage_roles, no mentions - "no extra permission" means the bot asks
        # for none of those either.
        out[who[key]] = discord.PermissionOverwrite(
            view_channel=bool(perms["read"]), send_messages=bool(perms["write"]),
            read_message_history=bool(perms["read"]))
    return out


@persistent
class SetupProvisionView(HubView):
    """Confirm-and-run. Persistent so an abandoned /setup can be finished later by
    anyone with staff rights instead of being re-typed."""

    def __init__(self, conn, staff_role_id: int = 0):
        # Default 0 on purpose: install() rebuilds every persistent view with only
        # (conn,) after a restart, so the id comes from config - which is exactly
        # why /setup stores it. A button clicked days later still knows who asked.
        super().__init__(conn, staff=True)
        self.staff_role_id = int(staff_role_id)

    @discord.ui.button(label="✅ Create everything", style=discord.ButtonStyle.success,
                       custom_id=cid("st", "run"))
    async def run(self, i, b):
        if not await self._authorized(i):
            return
        await i.response.defer(thinking=True, ephemeral=True)
        try:
            staff_id = self.staff_role_id or int(
                V.dbmod.cfg(self.conn, "staff_role_id") or 0)
            if not staff_id:
                return await i.followup.send(
                    "⚠️ I have no stored staff role - run `/setup mode:SETUP` again to "
                    "pick one.", ephemeral=True)
            res = await V.provision(self.conn, _GuildWorkspace(i.guild, self.conn),
                                    staff_id, actor_id=i.user.id)
        except discord.Forbidden:
            return await i.followup.send(
                "⚠️ I need **Manage Roles** and **Manage Channels** to do this, and "
                "nothing else. Administrator is not required and I will not ask for it.",
                ephemeral=True)
        await i.followup.send(embed=provision_report(res), ephemeral=True)

    @discord.ui.button(label="✖️ Cancel", style=discord.ButtonStyle.secondary,
                       custom_id=cid("st", "cancel"))
    async def cancel(self, i, b):
        if not await self._authorized(i):
            return
        await i.response.send_message("Setup cancelled — nothing was created.",
                                      ephemeral=True)


def provision_report(res) -> discord.Embed:
    e = discord.Embed(title="Hub is set up", colour=0x2ecc71)
    made = "\n".join(f"🆕 {m['name']} — `{m['id']}`" for m in res["created"]) or \
        "*nothing new*"
    used = "\n".join(f"♻️ {m['name']} — adopted by {m['adopted']}"
                      for m in res["reused"]) or "*nothing existing*"
    e.description = (f"**Created**\n{made}\n\n**Adopted** (already existed — these ids "
                      f"are now what the bot uses)\n{used}")
    e.set_footer(text="Rename or move any of them freely: lookups go by id, never by "
                       "name. Next step: /season-create")
    return e


# --------------------------------------------------------------------------- #
# player: answer a League 1 question
# --------------------------------------------------------------------------- #

@persistent
class AnswerView(HubView):
    @discord.ui.button(label="A", style=discord.ButtonStyle.primary, custom_id=cid("ans", 0, 0))
    async def opt_0(self, i, b): await self.pick(i, b)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary, custom_id=cid("ans", 0, 1))
    async def opt_1(self, i, b): await self.pick(i, b)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary, custom_id=cid("ans", 0, 2))
    async def opt_2(self, i, b): await self.pick(i, b)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary, custom_id=cid("ans", 0, 3))
    async def opt_3(self, i, b): await self.pick(i, b)

    # Only A-D: 5 buttons is the hard per-row cap and this card's 5th slot is
    # "clear" (row 1). A 6th option would silently overflow row 0 - the API
    # rejects it at send time, which is the worst possible moment to learn it.

    @discord.ui.button(label="↩︎ Clear my answer", style=discord.ButtonStyle.secondary,
                       custom_id=cid("ans", 0, "clear"), row=1)
    async def clear(self, i, b):
        got = parse_cid(b.custom_id, 2)
        if got is None or not got[0].isdigit():
            return await i.response.send_message("Stale button.", ephemeral=True)
        cleared = V.clear_answer(self.conn, int(got[0]), i.user.id)
        await i.response.send_message(
            "✅ Cleared - answer again before the timer." if cleared else
            "❌ Can't clear now: answers are locked once staff grade.", ephemeral=True)

    async def pick(self, interaction, button):
        # custom_id is hub:ans:<question_id>:<option> -> 2 parts after the prefix
        got = ints(button.custom_id, 2)
        if not got:
            return await interaction.response.send_message("Stale button.", ephemeral=True)
        question_id, option = got
        res = V.submit_answer(self.conn, question_id, interaction.user.id, option)
        if not res["accepted"]:
            return await interaction.response.send_message(res["message"], ephemeral=True)
        word = "updated" if res["change"] == "edited" else "recorded"
        await interaction.response.send_message(
            f"✅ {word}. You can change it until the timer ends — your **original "
            f"timestamp** is what counts for the speed bonus, so there is no reason "
            f"to panic-re-answer.", ephemeral=True)


def check_rows(view: discord.ui.View) -> None:
    """Guard the per-row limit ourselves: discord.py does NOT validate button rows
    (only select options), so an over-full card fails at SEND time - in front of
    200 players, mid-evening. We simulate Discord's packing: items fill a row to 5,
    an explicit row starts a new one, and there are at most 5 rows."""
    rows: dict[int, int] = {}
    auto = 0
    for child in view.children:
        declared = getattr(child, "_underlying", None) and             getattr(child._underlying, "row", None)
        if declared is not None:
            auto = declared + 1
            idx = declared
        else:
            while rows.get(auto, 0) >= BUTTONS_PER_ROW:
                auto += 1
            idx = auto
        rows[idx] = rows.get(idx, 0) + 1
        if rows[idx] > BUTTONS_PER_ROW:
            raise ValueError(
                f"button row {idx} would hold {rows[idx]} buttons (Discord allows "
                f"{BUTTONS_PER_ROW}). Use fewer than 4 options, or give an item an "
                f"explicit row.")
    if rows and max(rows) > 4:
        raise ValueError("a view may use at most 5 rows")
    if len(view.children) > 25:
        raise ValueError("a view may hold at most 25 items")


def answer_view(conn, question_id: int, options: list[str]) -> AnswerView:
    """Same class as the registered template, with the real question id baked into
    each custom_id. Unused slots render disabled, so nobody taps a ghost option."""
    if len(options) > MAX_OPTIONS:
        raise ValueError(f"{len(options)} options - League 1 cards allow {MAX_OPTIONS} "
                         f"answer buttons (A-D). Trim the question.")
    view = AnswerView(conn)
    for child in view.children:
        tail = (child.custom_id or "").split(":")[-1]
        if tail == "clear":
            child.custom_id = cid("ans", question_id, "clear")
            continue
        idx = int(tail)
        child.custom_id = cid("ans", question_id, idx)
        if idx < len(options):
            label = options[idx]
            child.label = (label[:70] + "…") if len(label) > 71 else label
            child.disabled = False
        else:
            child.label = "—"
            child.disabled = True
    check_rows(view)
    return view


# --------------------------------------------------------------------------- #
# player: submit a League 2/3 answer
# --------------------------------------------------------------------------- #

@persistent
class SubmitView(HubView):
    @discord.ui.button(label="📝 Submit my answer", style=discord.ButtonStyle.success,
                       custom_id=cid("sub", 0, 0))
    async def open_modal(self, interaction, button):
        got = ints(button.custom_id, 2)      # hub:sub:<evening_id>:0
        if not got:
            return await interaction.response.send_message("Stale button.", ephemeral=True)
        evening_id = got[0]
        ev = self.conn.execute("SELECT * FROM evening WHERE id=?", (evening_id,)).fetchone()
        if not ev or ev["status"] not in ("open", "scheduled"):
            return await interaction.response.send_message(
                "Tonight's window has closed.", ephemeral=True)
        await interaction.response.send_modal(AnswerModal(self.conn, evening_id))


# Stated where it is read, not buried in a rules channel: the bot cannot tell
# whether a model wrote a paragraph, so the rule is a promise to the players.
HONESTY_NOTE = ("Solo entry, in your own words. No AI — this is judged by people, "
                "and an entry that reads like a model's can be asked about.")


class AnswerModal(discord.ui.Modal, title="Your answer"):
    def __init__(self, conn, evening_id: int):
        super().__init__(timeout=900)
        self.conn, self.evening_id = conn, evening_id

    answer = discord.ui.TextInput(
        label="Answer", style=discord.TextStyle.paragraph, min_length=20, max_length=4000,
        placeholder="1) what you run and why  2) first 20 seconds  "
                    "3) what breaks it + the switch  4) one thing they won't expect")

    async def on_submit(self, interaction: discord.Interaction):
        res = V.create_submission(self.conn, self.evening_id, interaction.user.id,
                                  self.answer.value)
        if not res["accepted"]:
            return await interaction.response.send_message(res["message"], ephemeral=True)
        notes = [f"✅ {res['word_count']} words",
                 "re-updated" if res["rework"] else "recorded"]
        if res.get("duplicate_of"):
            notes.append("⚠️ very similar to another entry tonight — staff may ask you "
                         "to explain it in a ticket")
        await interaction.response.send_message(
            " · ".join(notes) + "\nPoints are posted after the deadline; the leaderboard "
            "updates itself.\n*" + HONESTY_NOTE + "*", ephemeral=True)

    async def on_error(self, interaction, error):
        await interaction.response.send_message(f"⚠️ Could not save: `{error}`",
                                                ephemeral=True)


# --------------------------------------------------------------------------- #
# staff: grade one League 1 question (tap the right option - the bot does maths)
# --------------------------------------------------------------------------- #

@persistent
class QuestionGradeView(HubView):
    def __init__(self, conn):
        super().__init__(conn, staff=True)

    @staticmethod
    def _question_id(button) -> int | None:
        """`hub:qv:<question_id>:<option|none>` -> question_id. One place parses,
        so a renamed id cannot silently break grading."""
        got = parse_cid(button.custom_id, 2)
        if got is None or not got[0].isdigit():
            return None
        return int(got[0])

    @discord.ui.button(label="✅ A", style=discord.ButtonStyle.success, custom_id=cid("qv", 0, 0))
    async def v_0(self, i, b): await self.mark(i, b, 0)

    @discord.ui.button(label="✅ B", style=discord.ButtonStyle.success, custom_id=cid("qv", 0, 1))
    async def v_1(self, i, b): await self.mark(i, b, 1)

    @discord.ui.button(label="✅ C", style=discord.ButtonStyle.success, custom_id=cid("qv", 0, 2))
    async def v_2(self, i, b): await self.mark(i, b, 2)

    @discord.ui.button(label="✅ D", style=discord.ButtonStyle.success, custom_id=cid("qv", 0, 3))
    async def v_3(self, i, b): await self.mark(i, b, 3)

    @discord.ui.button(label="🎯 Points", style=discord.ButtonStyle.secondary,
                       custom_id=cid("qv", 0, "points"), row=1)
    async def points(self, i, b):
        """Set THIS question's value. Difficulty varies per question, so the tier
        default is only ever a suggestion: staff decide the number, the bot still
        does every bit of arithmetic (speed bonus, first-correct, Sunday x1.5)."""
        if not await self._authorized(i):
            return
        got = ints(b.custom_id, 2)
        if not got:
            return
        await i.response.send_modal(PointsModal(self.conn, got[0]))

    @discord.ui.button(label="🚫 No correct option (0 pts, logged)",
                       style=discord.ButtonStyle.danger, custom_id=cid("qv", 0, "none"),
                       row=1)          # own row: 4 verdicts fill row 0
    async def none(self, i, b):
        if not await self._authorized(i):
            return
        qid = self._question_id(b)
        if qid is None:
            return
        res = V.grade_question(self.conn, qid, None, i.user.id)
        await i.response.send_message(
            f"Question voided, nobody awarded. {res['questions_left_ungraded']} left "
            f"on this evening.", ephemeral=True)
        await _reedit_question_card(self.conn, i, qid)

    async def mark(self, interaction, button, option: int):
        if not await self._authorized(interaction):
            return
        got = ints(button.custom_id, 2)      # hub:qv:<question_id>:<option>
        if not got:
            return await interaction.response.send_message("Stale button.", ephemeral=True)
        question_id = got[0]
        try:
            res = V.grade_question(self.conn, question_id, option, interaction.user.id)
        except ValueError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        top = ", ".join(f"<@{p}> +{p_}" for p, p_ in res["winners"][:5]) or "nobody"
        msg = (f"📊 **{res['players_scored']}** scored · **{res['points_this_question']}** pts "
               f"paid out · {res['questions_left_ungraded']} question(s) left tonight\n"
               f"Top: {top}")
        if res["evening_complete"]:
            msg += "\n\n✅ All 3 graded — the evening is ready to finalise."
        await interaction.response.send_message(msg, ephemeral=True)
        await _reedit_question_card(self.conn, interaction, question_id)


def question_grade_view(conn, question_id: int, n_options: int,
                        options: list[str] | None = None) -> QuestionGradeView:
    """Staff row: one button per option, labelled with the option TEXT.

    Staff should never have to count "was it B or C" while 40 people wait - and a
    mis-tap here is a wrong answer for everyone, so the label is unambiguous.
    """
    view = QuestionGradeView(conn)
    # Identify the void control by its custom_id tail, NOT by index: index math is
    # how the E slot silently stayed enabled next to it.
    for child in view.children:
        tail = (child.custom_id or "").split(":")[-1]
        if tail == "none":
            child.custom_id = cid("qv", question_id, "none")
            continue
        if tail == "points":
            child.custom_id = cid("qv", question_id, "points")
            continue
        idx = int(tail)
        child.custom_id = cid("qv", question_id, idx)
        if idx >= n_options or not (options and idx < len(options)):
            child.label, child.disabled = "—", True    # unusable, unmistakably so
            continue
        text = options[idx]
        text = (text[:22] + "…") if len(text) > 23 else text
        child.label = f"✅ {LETTERS[idx]} {text}".strip()
    check_rows(view)
    return view


class PointsModal(discord.ui.Modal, title="Points for a correct answer"):
    """Per-question base value, keyed by QUESTION id, not evening id: one night may
    hold an easy freebie and a monster, and they must not share a number.

    NOT pre-multiplied. On Sunday the bot still applies x1.5, so staff answer one
    question - "how hard is this?" - and never do economy arithmetic."""

    def __init__(self, conn, question_id: int):
        super().__init__(timeout=600)
        self.conn, self.question_id = conn, question_id

    points = discord.ui.TextInput(
        label="Points for a correct answer (0-40)", max_length=2, min_length=1,
        placeholder="6")
    why = discord.ui.TextInput(label="Why this value (optional, goes in the audit log)",
                                required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            return await interaction.response.send_message("That is not a whole number.",
                                                            ephemeral=True)
        q = self.conn.execute("SELECT correct_option FROM question WHERE id=?",
                              (self.question_id,)).fetchone()
        if q is None:
            return await interaction.response.send_message("That question no longer exists.",
                                                            ephemeral=True)
        try:
            V.set_question_points(self.conn, self.question_id, value,
                                  actor_id=interaction.user.id,
                                  why=_modal_text(self, "why") or None)
        except ValueError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        # Already graded? Then the new number is meaningless until the night is
        # recomputed - re-grade it rather than leave the board showing old maths.
        if q["correct_option"] is not None:
            res = V.grade_question(self.conn, self.question_id, q["correct_option"],
                                   actor_id=interaction.user.id)
            await _reedit_question_card(self.conn, interaction, self.question_id)
            await interaction.response.send_message(
                f"🎯 **{value}** per correct answer · re-graded, "
                f"{res['players_scored']} player(s) on {res['points_this_question']} pts.",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                f"🎯 **{value}** per correct answer · applied when you grade. "
                "Speed bonus, first-correct and Sunday x1.5 still come from the bot.",
                ephemeral=True)
        await _refresh_boards(self.conn, interaction)


async def _reedit_question_card(conn, interaction, question_id: int) -> None:
    """Re-render the question card in place so its footer shows the truth
    (correct option + points), and swap the staff row to a locked state."""
    row = conn.execute("SELECT message_id FROM question WHERE id=?", (question_id,)).fetchone()
    if not row or not row["message_id"]:
        return
    try:
        msg = await interaction.channel.fetch_message(row["message_id"])
        await msg.edit(view=discord.ui.View(timeout=None))
    except (discord.HTTPException, AttributeError):
        pass       # card gone / no channel: never fail the grade over a cosmetic edit


# --------------------------------------------------------------------------- #
# staff: L2/L3 band buttons
# --------------------------------------------------------------------------- #

@persistent
class GradingView(HubView):
    def __init__(self, conn, evening_id: int = 0):
        super().__init__(conn, staff=True)
        self.evening_id = evening_id

    @discord.ui.button(label="🌟 EXCELLENT 25", style=discord.ButtonStyle.success,
                       custom_id=cid("gr", 0, 25))
    async def excellent(self, i, b): await self.award(i, b, 25, "Excellent")

    @discord.ui.button(label="✅ GOOD 15", style=discord.ButtonStyle.primary,
                       custom_id=cid("gr", 0, 15))
    async def good(self, i, b): await self.award(i, b, 15, "Good")

    @discord.ui.button(label="➖ AVERAGE 8", style=discord.ButtonStyle.secondary,
                       custom_id=cid("gr", 0, 8))
    async def average(self, i, b): await self.award(i, b, 8, "Average")

    @discord.ui.button(label="⬇️ POOR 2", style=discord.ButtonStyle.secondary,
                       custom_id=cid("gr", 0, 2))
    async def poor(self, i, b): await self.award(i, b, 2, "Poor")

    @discord.ui.button(label="🔢 CUSTOM", style=discord.ButtonStyle.primary,
                       custom_id=cid("gr", 0, "custom"))
    async def custom(self, i, b):
        got2 = parse_cid(b.custom_id, 2)
        if not got2 or not got2[0].isdigit():
            return await i.response.send_message("Stale button.", ephemeral=True)
        await i.response.send_modal(CustomPointModal(self.conn, int(got2[0])))

    @discord.ui.button(label="⏭️ Skip / 0", style=discord.ButtonStyle.danger,
                       custom_id=cid("gr", 0, "zero"))
    async def zero(self, i, b):
        if not await self._authorized(i):
            return
        got = ints(b.custom_id, 2)            # hub:gr:<evening_id>:zero -> not ints, see below
        if not got:
            got2 = parse_cid(b.custom_id, 2)
            if not got2 or not got2[0].isdigit():
                return await i.response.send_message("Stale button.", ephemeral=True)
            return await self._award(i, int(got2[0]), 0, "Skipped", "next entry")
        await self._award(i, got[0], 0, "Skipped", "next entry")

    async def award(self, interaction, button, points: int, band: str):
        if not await self._authorized(interaction):
            return
        got = ints(button.custom_id, 2)       # hub:gr:<evening_id>:<points>
        if not got:
            return await interaction.response.send_message("Stale button.", ephemeral=True)
        await self._award(interaction, got[0], points, band, "next entry")

    async def _award(self, interaction, evening_id: int, points: int, band: str, label: str):
        nxt = self.conn.execute(
            "SELECT id, player_id, flag FROM submission WHERE evening_id=? "
            "AND points_in IS NULL AND status='submitted' ORDER BY submitted_at LIMIT 1",
            (evening_id,)).fetchone()
        if not nxt:
            return await interaction.response.send_message(
                "🎉 Nothing left to grade tonight.", ephemeral=True)
        res = V.award_submission(self.conn, nxt["id"], points, interaction.user.id, band=band)
        # A flag never changes the arithmetic - the staff number stands. It is
        # repeated here so the decision is made with the context in front of them.
        note = f"\n\n⚠️ Flagged `{nxt['flag']}` before awarding." if nxt["flag"] else ""
        mult = self.conn.execute("SELECT multiplier FROM evening WHERE id=?",
                                 (evening_id,)).fetchone()["multiplier"]
        after = (f" → **{res['points']}** after ×{mult:g}"
                 if res["points"] != points else "")
        left = res["submissions_left"]
        await interaction.response.send_message(
            f"⚖️ <@{nxt['player_id']}> — **{band} {points}**{after} · {left} left tonight"
            + ("\n\n✅ Evening fully graded." if left == 0 else "") + note,
            ephemeral=True)
        # The permanent leaderboard refreshes on the award itself - a player who
        # just moved up should not have to ask staff to update a card.
        await _refresh_boards(self.conn, interaction)


class CustomPointModal(discord.ui.Modal, title="Custom points (0–25)"):
    def __init__(self, conn, evening_id: int):
        super().__init__(timeout=600)
        self.conn, self.evening_id = conn, evening_id

    points = discord.ui.TextInput(label="Points 0-25", max_length=2, min_length=1,
                                   placeholder="17")
    note = discord.ui.TextInput(label="Why (optional, shown in audit)", required=False,
                                 max_length=200, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            return await interaction.response.send_message(
                "That is not a whole number.", ephemeral=True)
        nxt = self.conn.execute(
            "SELECT id FROM submission WHERE evening_id=? AND points_in IS NULL "
            "AND status='submitted' ORDER BY submitted_at LIMIT 1",
            (self.evening_id,)).fetchone()
        if not nxt:
            return await interaction.response.send_message("Nothing left to grade.", ephemeral=True)
        try:
            res = V.award_submission(self.conn, nxt["id"], value, interaction.user.id,
                                     band=V.band_for(value), note=self.note.value or None)
        except ValueError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        await interaction.response.send_message(
            f"⚖️ CUSTOM {value} → **{res['points']}** · logged with your reason",
            ephemeral=True)


# --------------------------------------------------------------------------- #
# staff: author content without ever typing an id
# --------------------------------------------------------------------------- #

NIGHT_LABELS = {"question": ("📚 Author a League 1 question",
                             "Pick the night. Nothing is posted until 16:00."),
                "prompt": ("⚔️ Set a League 2 / 3 night",
                           "Pick the night and write the card players will see.")}


def night_options(conn, mode: str) -> list[discord.SelectOption]:
    """The only place an evening id is ever written down, and it is never shown
    to a human as a number - it lives in the option value."""
    out = []
    for ev in V.authorable_nights(conn):
        if mode == "question" and ev["league"] != "l1":
            continue                      # L2/L3 have no questions, they would be a trap
        if mode == "prompt" and ev["league"] == "l1":
            continue
        # v4.1: Sunday is not a reward tier. The card says what it is - the night
        # where all three leagues run - and stops implying the points are bigger.
        grand = " · all three leagues tonight" if ev.get("difficulty") == "grand" else ""
        if mode == "question":
            desc = f"{ev['questions']}/3 questions authored{grand}"
        else:
            desc = "prompt set ✓" if ev["has_prompt"] else f"no prompt yet{grand}"
        out.append(discord.SelectOption(
            label=f"{ev['day']} · {ev['league'].upper()}"[:100],
            value=str(ev["id"]),
            description=desc[:100],
            emoji=LEAGUE_META[ev["league"]][0]))
    return out


class NightSelect(discord.ui.Select):
    def __init__(self, conn, mode: str):
        self.conn, self.mode = conn, mode
        super().__init__(placeholder="Pick a night…", min_values=1, max_values=1,
                         options=night_options(conn, mode) or [
                             discord.SelectOption(label="Nothing authorable right now",
                                                  value="0", description="create a season first")])

    async def callback(self, interaction: discord.Interaction):
        if not await self.view._authorized(interaction):
            return
        if self.values[0] == "0":
            return await interaction.response.send_message(
                "No upcoming nights. Create a season from the admin panel first.",
                ephemeral=True)
        night_id = int(self.values[0])
        modal = (QuestionAuthorModal(self.conn, night_id) if self.mode == "question"
                 else PromptAuthorModal(self.conn, night_id))
        await interaction.response.send_modal(modal)


@persistent
class PickNightView(HubView):
    """One select, no ids, no dates. Ephemeral, so it is a tool and not clutter.

    Both children are added through `add_item`, deliberately, in order:
      * `View.children` returns a COPY, so reordering after the fact is a no-op;
      * a decorated Cancel button is appended during `super().__init__()`, which
        would put it above the picker;
      * setting `.row` before `add_item` makes discord.py's row-weight tracker
        reject the item and silently REMOVE it, so the picker vanishes;
      * `@discord.ui.select` injects its own placeholder/row kwargs into the item
        constructor, which NightSelect does not accept.
    Auto-placement (select row 0, button row 1) is what the tests then verify.
    """

    def __init__(self, conn, mode: str = "question"):
        super().__init__(conn, staff=True)
        self.mode = mode if mode in NIGHT_LABELS else "question"
        sel = NightSelect(conn, self.mode)
        sel.custom_id = cid("pk", self.mode)          # stable: dispatch survives a restart
        if len(sel.options) == 1:                     # the "nothing authorable" filler
            sel.disabled = True
        self.add_item(sel)
        cancel = discord.ui.Button(label="✖️ Cancel", style=discord.ButtonStyle.secondary,
                                   custom_id=cid("pk", "cancel"))
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _cancel(self, interaction: discord.Interaction):
        if not await self._authorized(interaction):
            return
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = False) -> None:
    """add_field has no `overwrite` kwarg; editing an existing field by name is on
    us. Embeds are rebuilt from SQLite every refresh, so in practice this adds -
    but a helper that only ever appends would eventually blow past Discord's
    25-field ceiling on a long-lived pinned card."""
    for f in embed.fields:
        if f.name == name:
            f.value = value
            f.inline = inline
            return
    embed.add_field(name=name, value=value, inline=inline)


def _modal_text(modal, name: str) -> str:
    item = getattr(modal, name, None)
    return (getattr(item, "value", "") or "").strip()


class QuestionAuthorModal(discord.ui.Modal, title="Author a League 1 question"):
    """Prompt, options, tier, and the POINTS for this one question - in a single
    modal. Tier and points are free text because Discord modals cannot hold a
    dropdown; a blank points field takes the tier default, so nobody is forced
    to know the table by heart."""

    def __init__(self, conn, evening_id: int):
        super().__init__(timeout=1800)
        self.conn, self.evening_id = conn, evening_id

    prompt = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph,
                                   max_length=1000,
                                   placeholder="Which mech counters a Snow study build?")
    options = discord.ui.TextInput(label="Options, separated by |", max_length=1500,
                                    placeholder="Yagorath | Orion | Skyship | Blacklight")
    tier = discord.ui.TextInput(label="Tier: easy / medium / hard", max_length=6,
                                 placeholder="medium")
    points = discord.ui.TextInput(label="Points for a correct answer (blank = tier default)",
                                  required=False, max_length=2, placeholder="6")

    async def on_submit(self, interaction: discord.Interaction):
        opts = [o.strip() for o in _modal_text(self, "options").split("|") if o.strip()]
        if not 2 <= len(opts) <= 4:
            return await interaction.response.send_message(
                f"Discord cards hold 4 options. You gave {len(opts)} - separate them "
                "with | and keep it between 2 and 4.", ephemeral=True)
        tier = _modal_text(self, "tier").lower() or "medium"
        if tier not in ("easy", "medium", "hard"):
            return await interaction.response.send_message(
                f"`{tier}` is not a tier. Type easy, medium or hard.", ephemeral=True)
        raw = _modal_text(self, "points")
        base: int | None = None
        if raw:
            try:
                base = int(raw)
            except ValueError:
                return await interaction.response.send_message(
                    "Points must be a whole number (or leave it blank for the default).",
                    ephemeral=True)
            if not 0 <= base <= 40:
                return await interaction.response.send_message(
                    "Points must be 0-40. 40 is a brutal Sunday question.", ephemeral=True)
        suggested = V.default_points(tier)
        try:
            res = V.add_question(self.conn, self.evening_id, None,   # None = next free slot
                                 _modal_text(self, "prompt"), opts, tier=tier,
                                 points_per_correct=base)
        except ValueError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        paid = base if base is not None else suggested
        # nothing to caveat any more: Sunday pays the same as Tuesday
        maths = f"{paid} + speed bonus"
        return await interaction.response.send_message(
            f"✅ **Q{res['ordinal']}** queued for that night. Pays {maths}.\n"
            f"Deadline {res['answer_deadline'][:16]} · nothing players see until 16:00.",
            ephemeral=True)


class PromptAuthorModal(discord.ui.Modal, title="Set the night's prompt"):
    def __init__(self, conn, evening_id: int):
        super().__init__(timeout=1800)
        self.conn, self.evening_id = conn, evening_id

    prompt = discord.ui.TextInput(label="The card players see",
                                  style=discord.TextStyle.paragraph, max_length=1500,
                                  placeholder="Skyship, 11 slots, enemy Orion + Citadel. "
                                              "What do you run and why?")
    deadline = discord.ui.TextInput(label="Submit deadline (HH:MM IST, blank = 20:00)",
                                    required=False, max_length=5, placeholder="20:00")

    async def on_submit(self, interaction: discord.Interaction):
        ev = self.conn.execute("SELECT day, league, status FROM evening WHERE id=?",
                              (self.evening_id,)).fetchone()
        if ev is None:
            return await interaction.response.send_message("That night no longer exists.",
                                                            ephemeral=True)
        text = _modal_text(self, "prompt")
        if len(text.split()) < 3:
            return await interaction.response.send_message(
                "That is too short to be a scenario - players need something to read.",
                ephemeral=True)
        V.set_scenario_prompt(self.conn, ev["day"], ev["league"], text,
                             deadline=_modal_text(self, "deadline") or None,
                             actor_id=interaction.user.id)
        posted = "It will post at the opening time." if ev["status"] == "scheduled" \
            else "This night has already posted - use 🔄 Refresh on its card."
        await interaction.response.send_message(
            f"✅ {ev['league'].upper()} prompt saved for {ev['day']}. {posted}",
            ephemeral=True)


# --------------------------------------------------------------------------- #
# evening / hub / board / season
# --------------------------------------------------------------------------- #

@persistent
class EveningView(HubView):
    def __init__(self, conn, evening_id: int = 0):
        super().__init__(conn, staff=True)
        self.evening_id = evening_id

    @discord.ui.button(label="🔒 Close answers", style=discord.ButtonStyle.secondary,
                       custom_id=cid("ev", 0, "close"))
    async def close(self, i, b):
        if not await self._authorized(i):
            return
        got = ints(b.custom_id, 2)             # hub:ev:<evening_id>:close
        if not got:
            return
        try:
            V.close_evening(self.conn, got[0], i.user.id, reason="staff")
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        await i.response.send_message(
            "🔒 Locked. Players can no longer answer or edit; late edits are flagged ×0.5.",
            ephemeral=True)

    @discord.ui.button(label="🏁 Finalise evening", style=discord.ButtonStyle.success,
                       custom_id=cid("ev", 0, "final"))
    async def final(self, i, b):
        if not await self._authorized(i):
            return
        got = ints(b.custom_id, 2)
        if not got:
            return
        try:
            res = V.finalize_l1(self.conn, got[0], i.user.id)
        except ValueError as e:
            return await i.response.send_message(f"⚠️ {e}", ephemeral=True)
        top = "\n".join(f"<@{p}> — {pts} pts" for p, pts in res["top"][:5]) or "nobody scored"
        await i.response.send_message(f"🏁 Tonight:\n{top}", ephemeral=True)


@persistent
class BoardView(HubView):
    @discord.ui.button(label="📚 League 1", style=discord.ButtonStyle.primary,
                       custom_id=cid("bd", "l1"))
    async def b_l1(self, i, b): await self.show(i, b, "l1")

    @discord.ui.button(label="⚔️ League 2", style=discord.ButtonStyle.primary,
                       custom_id=cid("bd", "l2"))
    async def b_l2(self, i, b): await self.show(i, b, "l2")

    @discord.ui.button(label="🔧 League 3", style=discord.ButtonStyle.primary,
                       custom_id=cid("bd", "l3"))
    async def b_l3(self, i, b): await self.show(i, b, "l3")

    @discord.ui.button(label="🏆 Season", style=discord.ButtonStyle.success,
                       custom_id=cid("bd", "season"))
    async def b_season(self, i, b): await self.show(i, b, None)

    @discord.ui.button(label="🧬 Lifetime", style=discord.ButtonStyle.secondary,
                       custom_id=cid("bd", "total"))
    async def b_total(self, i, b): await self.show(i, b, None)

    async def show(self, interaction, button, league):
        got = parse_cid(button.custom_id, 1)
        if got is None:
            return
        scope = "league" if got[0] in ("l1", "l2", "l3") else got[0]
        await interaction.response.edit_message(
            embed=board_embed(self.conn, scope, None if scope != "league" else got[0]),
            view=BoardView(self.conn))


@persistent
class HubPanelView(HubView):
    @discord.ui.button(label="📚 Tonight", style=discord.ButtonStyle.primary,
                       custom_id=cid("hb", "tonight"))
    async def tonight(self, i, b):
        rows = V.todays_evenings(self.conn)
        desc = "\n".join(
            f"{LEAGUE_META[r['league']][0]} **{r['league'].upper()}** — "
            f"{PHASE.get(r['status'])}" for r in rows) or "Nothing scheduled today."
        e = discord.Embed(title="🗓️ Tonight", description=desc, colour=0x2f3136)
        e.set_footer(text="16:00 IST · answers close after 60s · League 1 is graded in ~1 minute")
        await i.response.edit_message(embed=e, view=HubPanelView(self.conn))

    @discord.ui.button(label="🏆 Standings", style=discord.ButtonStyle.success,
                       custom_id=cid("hb", "board"))
    async def board(self, i, b):
        await i.response.edit_message(embed=standings_embed(self.conn, "season", None),
                                      view=BoardView(self.conn))

    @discord.ui.button(label="🏛️ Hall of Fame", style=discord.ButtonStyle.secondary,
                       custom_id=cid("hb", "hof"))
    async def hof(self, i, b):
        rows = self.conn.execute(
            "SELECT h.*, s.name sn FROM hall_of_fame h JOIN season s ON s.id=h.season_id "
            "ORDER BY h.season_id DESC, h.league, h.placement LIMIT 24").fetchall()
        e = discord.Embed(title="🏛️ Hall of Fame", colour=0xf1c40f)
        e.description = "\n".join(
            f"**{r['sn']}** · {LEAGUE_META[r['league']][0]} #{r['placement']} "
            f"<@{r['player_id']}> — {r['points']} pts" for r in rows) or "*Season 1 awaits.*"
        await i.response.edit_message(embed=e, view=HubPanelView(self.conn))

    @discord.ui.button(label="🛠️ Staff tools", style=discord.ButtonStyle.primary,
                       custom_id=cid("hb", "staff"))
    async def staff_tools(self, i, b):
        """Authoring and the checkout queue live here, so no staff member ever has
        to remember a slash command - and no id or date is ever typed."""
        e = discord.Embed(title="🛠️ Staff tools", colour=0x2f3136)
        e.description = ("Everything below is pickers and modals. You never type an "
                         "evening id, a date, or a channel id.")
        v = HubView(self.conn, staff=True)
        for label, mode, style in (
                ("📚 Author a question", "question", discord.ButtonStyle.primary),
                ("⚔️ Set a night's prompt", "prompt", discord.ButtonStyle.primary),
                ("🧾 Checkout queue", "checkout", discord.ButtonStyle.danger)):
            btn = discord.ui.Button(label=label, style=style, custom_id=cid("st", mode))
            btn.callback = _make_tool_callback(self.conn, mode)
            v.add_item(btn)
        await i.response.send_message(embed=e, view=v, ephemeral=True)

    @discord.ui.button(label="📊 My stats", style=discord.ButtonStyle.secondary,
                       custom_id=cid("hb", "me"))
    async def me(self, i, b):
        hist = V.points_history(self.conn, i.user.id)
        e = discord.Embed(title=f"📊 {i.user.display_name}", colour=0x2f3136)
        if not hist:
            e.description = "No points yet tonight. Three questions, sixty seconds each."
        else:
            e.description = f"**{sum(h['points'] for h in hist)}** points across your " \
                            f"last {len(hist)} awards."
            e.add_field(name="Trail", value="\n".join(
                f"{h['day']} · {h['league']} · +{h['points']} · {h['reason']}"
                for h in hist[:5]) or "—", inline=False)
            e.set_footer(text="Every row records who awarded it. Dispute in #appeals with the day.")
        await i.response.send_message(embed=e, ephemeral=True)


@persistent
class SeasonAdminView(HubView):
    def __init__(self, conn):
        super().__init__(conn, staff=True)

    @discord.ui.button(label="📋 Season end — DRY RUN", style=discord.ButtonStyle.secondary,
                       custom_id=cid("ss", "dry"))
    async def dry(self, i, b):
        if not await self._authorized(i):
            return
        # same resolver finalize_season uses, so this list describes the same
        # season APPLY will change
        sid = V.closing_season_id(self.conn)
        if sid is None:
            return await i.response.send_message(
                "⚠️ No season to preview - create one with `/season-create` first.",
                ephemeral=True)
        diff = V.role_diff(self.conn, sid)
        icon = {"add": "➕", "remove": "➖", "skip": "⏭️", "manual": "🖐"}
        lines = [f"{icon.get(d['action'], '·')} {d['why']}" for d in diff] or ["*nothing*"]
        e = discord.Embed(title=f"Season {sid} end — DRY RUN (nothing changed)",
                          colour=0xf1c40f)
        e.description = "\n".join(lines)
        e.set_footer(text="Read this list. Removals are permanent until re-added, so "
                          "verify names before APPLY.")
        await i.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🔥 APPLY season end", style=discord.ButtonStyle.danger,
                       custom_id=cid("ss", "apply"))
    async def apply(self, i, b):
        if not await self._authorized(i):
            return
        res = await V.finalize_season(self.conn, i.user.id, apply_roles=True, guild=i.guild)
        champ = ", ".join(
            f"{LEAGUE_META[l][0]} <@{rows[0]['player_id']}>"
            for l, rows in res["leagues"].items() if rows) or "—"
        await i.response.send_message(
            f"## 🏁 Season closed\nChampions: {champ}\n"
            f"Hall of Fame rows: {len(res['hall_of_fame'])} · roles touched: "
            f"{len(res['roles_applied'])}",
            view=SeasonAdminView(self.conn))


def _rebind(view: discord.ui.View, evening_id: int, names: dict[str, str]) -> None:
    """Swap placeholder ids for the real evening id, keeping every id unique and
    parseable. The registered template and the sent instance therefore share one
    shape, which is exactly why dispatch survives a restart."""
    for child in view.children:
        key = (child.custom_id or "").split(":")[-1]
        child.custom_id = cid(names.get(key, key), evening_id) if len(
            (child.custom_id or "").split(":")) == 3 else cid(
            names.get(key, key), evening_id, key)


def grading_view(conn, evening_id: int) -> GradingView:
    view = GradingView(conn, evening_id)
    for child in view.children:
        tail = (child.custom_id or "").split(":")[-1]
        child.custom_id = cid("gr", evening_id, tail)
    check_rows(view)
    return view


def evening_view(conn, evening_id: int) -> EveningView:
    view = EveningView(conn, evening_id)
    for child in view.children:
        tail = (child.custom_id or "").split(":")[-1]
        child.custom_id = cid("ev", evening_id, tail)
    return view


def submit_view(conn, evening_id: int) -> SubmitView:
    view = SubmitView(conn)
    view.children[0].custom_id = cid("sub", evening_id, 0)
    return view


# --------------------------------------------------------------------------- #
# embed builders
# --------------------------------------------------------------------------- #

def _ts(iso: str) -> int:
    return int(V.parse_iso(iso).timestamp())


def question_embed(conn, q, ev) -> discord.Embed:
    opts = [o for o in _options(conn, q["id"])]
    e = discord.Embed(title=f"Q{q['ordinal']} · {q['tier'].upper()}",
                      description=q["prompt"], colour=LEAGUE_META["l1"][2])
    if q["image_url"]:
        e.set_image(url=q["image_url"])
    listed = "\n".join(f"`{LETTERS[i]}` {o}" for i, o in enumerate(opts))
    if listed:
        e.add_field(name="Options", value=listed, inline=False)
    if q["correct_option"] is None:
        e.set_footer(text=f"⏱ closes <t:{_ts(q['answer_deadline'])}:R> · "
                          f"tier {q['tier']} · speed bonus fades over 3 min")
    else:
        awarded = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(points),0) p FROM award "
                               "WHERE question_id=?", (q["id"],)).fetchone()
        e.description += f"\n\n**Correct: {LETTERS[q['correct_option']]} " \
                         f"— {opts[q['correct_option']] if q['correct_option'] < len(opts) else ''}**"
        if q["explanation"]:
            e.description += f"\n_{q['explanation']}_"
        e.set_footer(text=f"✅ {awarded['c']} player(s) scored · {awarded['p']} points paid")
    return e


def _options(conn, question_id: int):
    row = conn.execute("SELECT options FROM question WHERE id=?", (question_id,)).fetchone()
    import json
    return json.loads(row["options"]) if row and row["options"] else []


def evening_embed(conn, ev) -> discord.Embed:
    icon, label, colour = LEAGUE_META[ev["league"]]
    e = discord.Embed(title=f"{icon} {label} — {ev['day']}", colour=colour)
    mult = f" · **×{ev['multiplier']}** Sunday" if float(ev["multiplier"]) != 1.0 else ""
    e.description = f"{PHASE.get(ev['status'], ev['status'])}{mult}\n" \
                    f"Closes <t:{_ts(ev['closes_at'])}:R>"
    if ev["league"] == "l1":
        qs = conn.execute("SELECT id, ordinal, tier, correct_option FROM question "
                          "WHERE evening_id=? ORDER BY ordinal", (ev["id"],)).fetchall()
        done = sum(1 for q in qs if q["correct_option"] is not None)
        e.add_field(name="Questions", value=f"{done}/{len(qs)} graded", inline=True)
        e.add_field(name="Players", value=str(conn.execute(
            "SELECT COUNT(DISTINCT player_id) c FROM entry WHERE evening_id=?",
            (ev["id"],)).fetchone()["c"]), inline=True)
    else:
        sub = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(points_in IS NULL),0) u FROM submission "
            "WHERE evening_id=?", (ev["id"],)).fetchone()
        e.add_field(name="Submissions", value=str(sub["n"]), inline=True)
        e.add_field(name="Awaiting points", value=str(sub["u"]), inline=True)
    e.set_footer(text="Staff enter points; the bot applies multipliers, caps and coins.")
    return e


def standings_embed(conn, scope: str, league: str | None) -> discord.Embed:
    rows = V.standings(conn, scope, league=league, limit=10)
    title = ("🏆 Season standings" if scope == "season"
             else f"{LEAGUE_META[league][0]} {LEAGUE_META[league][1]}")
    e = discord.Embed(title=title, colour=0x9b59b6 if scope == "season"
                      else LEAGUE_META[league][2])
    if not rows:
        e.description = "*Nothing graded yet — the table fills as staff post points.*"
    else:
        e.description = "\n".join(
            f"{ {1:'🥇',2:'🥈',3:'🥉'}.get(r['rank'], str(r['rank'])+'.') } "
            f"<@{r['player_id']}> — **{r['points']}** pts · {r['nights']} nights"
            for r in rows)
        e.set_footer(text="Provisional · coins follow these points · the Final decides "
                          "who is champion")
    return e


# --------------------------------------------------------------------------- #
# the two pinned panels: a board that updates itself, and a checkout queue
# --------------------------------------------------------------------------- #

def board_embed(conn, scope: str = "season", league: str | None = None) -> discord.Embed:
    """One card, three views: this month, a league table, and the lifetime total.

    The permanent leaderboard, so the three scopes are shown together rather than
    hidden behind buttons - a player should be able to read their standing without
    clicking anything, and the buttons are there to switch the MAIN view.
    """
    e = standings_embed(conn, scope, league)
    if scope != "total":
        life = V.standings(conn, "total", limit=3)
        if life:
            set_field(e, "🧬 Lifetime (all seasons)",
            "\n".join(f"{r['rank']}. <@{r['player_id']}> — **{r['points']}** pts"
                        for r in life) or "—")
    month = V.standings(conn, "season", limit=1)
    size = V.league_table_size(conn, league) if league else None
    foot = "Updates itself after every award · nobody has to refresh this card"
    if size:
        foot += f" · {size['excluded']} player(s) below the {size['floor']}-night floor"
    e.set_footer(text=foot)
    return e


# --------------------------------------------------------------------------- #
# Answering an interaction without ever raising
# --------------------------------------------------------------------------- #
async def reply(i, *, content=None, embed=None, view=None, file=None, ephemeral=True,
                clear_content=False):
    """Answer `i` however it currently can be answered, and never propagate a 404.

    One function instead of 72 hand-written `i.response.send_message(...)` sites, because
    a slash command's token dies on its own: the 3-second response window, a gateway
    reconnect, or a container restart between the command and the answer each make every
    reply method raise NotFound - including from inside the error handler that was meant
    to explain the problem. The branch order mirrors discord.py's own state machine
    (`is_done()` is False until something is sent, and a deferred reply counts as done).
    Returns "replied" or "dropped"; callers never have to handle either.

    clear_content is the one field the None-filter above cannot express. Omitting `content`
    leaves the message's text alone; sending an explicit null deletes it. A deferred
    component interaction (DEFERRED_UPDATE_MESSAGE) edits the message it was clicked on,
    so a caller that is replacing that message's text needs the explicit null.
    """
    kw = {k: v for k, v in (("content", content), ("embed", embed),
                            ("view", view), ("file", file)) if v is not None}
    if clear_content:
        kw["content"] = None
    if not kw:
        # edit_original_response() with no fields is a Discord 500, not a no-op. A caller
        # with nothing to say is a bug in the caller, so keep it loud but legal.
        kw = {"content": None}
    try:
        if i.response.is_done():
            await i.edit_original_response(**kw)     # deferred placeholder, or a re-answer
        else:
            await i.response.send_message(**kw, ephemeral=ephemeral)
        return "replied"
    except (discord.NotFound, discord.HTTPException) as exc:
        # NotFound is the expired token. A 400 ("interaction already responded to") or a
        # 403 is equally not worth a traceback: by now the database write has happened, so
        # the only thing left to do is inform - and inform without breaking if we cannot.
        if not isinstance(exc, discord.NotFound) and getattr(exc, "status", 0) not in (400, 403, 404):
            raise                                    # a real problem stays a real problem
    return "dropped"



_BOT = None


def bind_bot(bot) -> None:
    """One-line indirection so views can reach `get_channel` without every view
    carrying a client reference (views are built by install() before any exists)."""
    global _BOT
    _BOT = bot


def no_controls() -> discord.ui.View:
    """Public wrapper - main.py needs it for an empty pinned queue."""
    return _no_controls()


async def _refresh_boards(conn, interaction) -> None:
    """Refresh every pinned board. A failed edit must never fail the award that
    caused it: the points are already in SQLite, and the next award retries."""
    # The bot owns get_channel; a bare FakeInteraction in tests may not. Either way
    # a missing client is a skip, never a crash - the award already happened.
    bot = _BOT or getattr(interaction, "client", None)
    if bot is None or not hasattr(bot, "get_channel"):
        return
    panels = V.board_panels(conn)
    for row in panels:
        try:
            _, mid = row["key"].split(":", 1)
        except ValueError:
            continue
        try:
            channel = bot.get_channel(int(row["value"])) if bot else None
            if channel is None:
                channel = await bot.fetch_channel(int(row["value"]))
            msg = await channel.fetch_message(int(mid))
            await msg.edit(embed=board_embed(conn, "season", None), view=BoardView(conn))
        except Exception:
            continue                      # deleted / no access / offline: keep trying later


@persistent
class CheckoutView(HubView):
    """The pending-checkout channel. One CLEAR button per payout.

    The bot deliberately cannot pay anyone: it lists what is owed, a human sends
    the coins in the server economy, and pressing CLEAR is the only way a row
    leaves the queue. Second press is refused rather than double-paying.
    """

    def __init__(self, conn, page: int = 0):
        super().__init__(conn, staff=True)
        self.page = page
        batches = V.payout_batches(conn)
        rows = batches[page] if page < len(batches) else []
        for row in rows[:10]:
            btn = discord.ui.Button(
                # The PLAYER is named on the button, not the payout id: staff match
                # a row to a name they recognise, and never need to read an id.
                label=f"✓ {row['kind'].upper()} {row['amount']:,}"[:80],
                style=discord.ButtonStyle.success,
                custom_id=cid("co", row["id"]))
            btn.callback = _make_clear_callback(self, row["id"])
            self.add_item(btn)
        if not rows:
            # Its own id tail, NOT another hub:co:<int>: sharing the prefix means a
            # stale "nothing pending" button would be parsed as a payout id and hit
            # the CLEAR handler with id 0 - i.e. an error dialog where a no-op
            # belonged. Distinct tail, distinct handler.
            btn = discord.ui.Button(label="Nothing pending — refresh",
                                    style=discord.ButtonStyle.secondary,
                                    custom_id=cid("co", "empty"))
            btn.callback = self._refresh
            self.add_item(btn)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary,
                       custom_id=cid("co", "refresh"))
    async def refresh_button(self, i, b):
        await self._refresh(i)

    async def _refresh(self, interaction):
        """Re-render from SQLite. The queue changes as other staff clear rows, so
        a stale card must always be one tap from the truth."""
        if not await self._authorized(interaction):
            return
        rows = V.pending_payouts(self.conn)
        await interaction.response.edit_message(
            embed=checkout_embed(self.conn, min(self.page, max(0, len(V.payout_batches(self.conn)) - 1))),
            view=CheckoutView(self.conn, self.page) if rows else _no_controls())


def checkout_embed(conn, page: int = 0, note: str | None = None) -> discord.Embed:
    rows = V.pending_payouts(conn)
    batches = V.payout_batches(conn)
    batch = batches[page] if page < len(batches) else []
    e = discord.Embed(title="🧾 Pending checkouts", colour=0xe67e22)
    if not rows:
        e.description = "*Nothing owed. The queue empties as staff clear it.*"
        return e
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["amount"]
    e.description = "\n".join(
        f"**{r['player_id']}** · `{r['kind']}` **{r['amount']:,}** — "
        f"{r['points']} pts × {r['rate']} (season {r['season_id']})"
        f"{' · ✅ CLEARED' if r['status'] == 'cleared' else ''}" for r in batch)
    set_field(e, "Totals owed", " · ".join(f"{k} **{v:,}**" for k, v in sorted(by_kind.items())))
    e.set_footer(text=(f"page {page + 1}/{len(batches)} · the bot never pays anyone — "
                       "send it in-server, then press the row's CLEAR button")
                 + (f" · {note}" if note else ""))
    return e


def _make_tool_callback(conn, mode: str):
    async def _tool(interaction: discord.Interaction):
        view = interaction.message.view if interaction.message else None
        if isinstance(view, HubView) and not await view._authorized(interaction):
            return
        if mode == "checkout":
            e = checkout_embed(conn)
            return await interaction.response.edit_message(
                embed=e, view=CheckoutView(conn) if V.pending_payouts(conn) else _no_controls())
        title, blurb = NIGHT_LABELS[mode]
        e = discord.Embed(title=title, description=blurb, colour=0x2f3136)
        nights = V.authorable_nights(conn)
        set_field(e, "Nights open for content",
                "\n".join(f"{n['day']} · {n['league'].upper()} · "
                           f"{n['questions']}Q" for n in nights[:6]) or "—")
        await interaction.response.edit_message(embed=e, view=PickNightView(conn, mode))
    return _tool


def _make_clear_callback(view, payout_id: int):
    async def _clear(interaction: discord.Interaction):
        if not await view._authorized(interaction):
            return
        try:
            res = V.clear_payout(view.conn, payout_id, interaction.user.id)
        except ValueError as e:
            return await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        if res.get("already"):
            return await interaction.response.send_message(
                "⚠️ Already cleared by someone else — nothing was sent twice.",
                ephemeral=True)
        page = view.page
        batches = V.payout_batches(view.conn)
        if not batches:
            await interaction.response.edit_message(embed=checkout_embed(view.conn),
                                                    view=_no_controls())
            return
        page = min(page, len(batches) - 1)      # last row cleared: fall back a page
        await interaction.response.edit_message(embed=checkout_embed(view.conn, page),
                                                view=CheckoutView(view.conn, page))
        left = res.get("remaining")
        await interaction.followup.send(
            f"✅ Marked paid: <@{res['player_id']}> {res['kind']} {res['amount']:,}."
            + (f" {left} still pending." if left else " Queue is empty."),
            ephemeral=True)
        await _refresh_boards(view.conn, interaction)
    return _clear


def _make_refresh_callback(view):
    async def _refresh(interaction: discord.Interaction):
        if not await view._authorized(interaction):
            return
        await interaction.response.edit_message(embed=checkout_embed(view.conn),
                                                view=CheckoutView(view.conn))
    return _refresh


def _no_controls() -> discord.ui.View:
    """Empty but NOT `None`: `message.edit(view=None)` is rejected by discord.py,
    and leaving the old buttons live is exactly how a stale CLEAR button on an
    emptied queue becomes a double-send."""
    return discord.ui.View(timeout=None)


def hub_embed(conn) -> discord.Embed:
    today = V.todays_evenings(conn)
    e = discord.Embed(title="🏆 The Hub — Knowledge Season", colour=0x2f3136)
    e.description = "\n".join(
        f"{LEAGUE_META[t['league']][0]} **{t['league'].upper()}** — "
        f"{PHASE.get(t['status'])} · closes <t:{_ts(t['closes_at'])}:R>"
        for t in today) or "**No evening scheduled for today.**"
    # derived, never hard-coded: the hour in the card is the hour in the code
    e.set_footer(text=f"opens {V.NIGHT_OPEN_HOUR:02d}:{V.NIGHT_OPEN_MINUTE:02d} IST · "
                      "open 24h · every league pays the same, every night")
    return e
