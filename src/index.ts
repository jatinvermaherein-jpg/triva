import "dotenv/config";

import {
  ActionRowBuilder,
  AttachmentBuilder,
  ButtonBuilder,
  ButtonStyle,
  ChannelType,
  Client,
  EmbedBuilder,
  Events,
  GatewayIntentBits,
  MessageFlags,
  ModalBuilder,
  PermissionFlagsBits,
  REST,
  RoleSelectMenuBuilder,
  Routes,
  SlashCommandBuilder,
  StringSelectMenuBuilder,
  TextInputBuilder,
  TextInputStyle,
  ThreadAutoArchiveDuration,
  UserSelectMenuBuilder
} from "discord.js";

import { createClient } from "@supabase/supabase-js";
import { createHash, randomUUID } from "node:crypto";

import {
  COLORS,
  DAY,
  LABELS,
  LEAGUES,
  TITLES,
  chunks,
  compareStanding,
  imageMime,
  makeSlots,
  nextMondayIST,
  normalizeSupabaseUrl,
  validateSections,
  wordCount,
  type League
} from "./domain.js";

function env(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing environment variable: ${name}`);
  return value;
}

const TOKEN = env("DISCORD_TOKEN");
const APP_ID = env("DISCORD_APPLICATION_ID");
const GUILD_ID = env("DISCORD_GUILD_ID");

const BUCKET =
  process.env.SUPABASE_UPLOAD_BUCKET ?? "knowledge-uploads";

const SUPABASE_URL = normalizeSupabaseUrl(env("SUPABASE_URL"));

const supabase = createClient(
  SUPABASE_URL,
  env("SUPABASE_SERVICE_ROLE_KEY"),
  {
    auth: {
      persistSession: false,
      autoRefreshToken: false
    }
  }
);

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent
  ],
  allowedMentions: { parse: [] }
});

let config: any = null;
let workerRunning = false;
let setupRunning = false;
let shuttingDown = false;

const imageLocks = new Set<string>();

function dbErrorMessage(error: any): string {
  const parts = [error?.message ?? "Unknown database error"];
  if (error?.code) parts.push(`code=${error.code}`);
  if (error?.hint) parts.push(error.hint);
  if (error?.details) parts.push(error.details);

  const text = parts.filter(Boolean).join(" — ");

  if (
    error?.code === "PGRST125" ||
    /invalid path specified in request url/i.test(text)
  ) {
    return (
      `${text}. SUPABASE_URL must be the Project URL ` +
      `(https://YOUR_PROJECT.supabase.co), not /rest/v1 or a postgres:// URI. ` +
      `Using ${SUPABASE_URL}`
    );
  }

  return text;
}

function isMissingSchemaError(error: any): boolean {
  const text = `${error?.message ?? ""} ${error?.code ?? ""} ${error?.hint ?? ""}`;
  return (
    /PGRST205/.test(text) ||
    /42P01/.test(text) ||
    /could not find the table/i.test(text) ||
    /schema cache/i.test(text) ||
    /relation .* does not exist/i.test(text)
  );
}

async function db(query: PromiseLike<any>): Promise<any> {
  const { data, error } = await query;
  if (error) throw new Error(dbErrorMessage(error));
  return data;
}

async function rpc(name: string, args: Record<string, unknown>) {
  return db(supabase.rpc(name, args));
}

async function loadConfig() {
  try {
    config = await db(
      supabase.from("ks_config")
        .select("*")
        .eq("guild_id", GUILD_ID)
        .maybeSingle()
    );
  } catch (error: any) {
    if (isMissingSchemaError(error)) {
      console.error(
        "[config] ks_config is missing. Run sql/001_initial.sql " +
        "in the Supabase SQL editor before /setup."
      );
      config = null;
      return config;
    }
    throw error;
  }
  return config;
}

async function requireDatabase() {
  try {
    await db(
      supabase.from("ks_config").select("guild_id").limit(1)
    );
  } catch (error: any) {
    if (isMissingSchemaError(error)) {
      throw new Error(
        "The Knowledge Season tables are missing. Run sql/001_initial.sql " +
        "in the Supabase SQL editor, then try /setup again."
      );
    }
    throw error;
  }
}

async function guild() {
  return client.guilds.fetch(GUILD_ID);
}

async function member(userId: string) {
  return (await guild()).members.fetch(userId);
}

async function displayName(userId: string): Promise<string> {
  try {
    return (await member(userId)).displayName;
  } catch {
    return userId;
  }
}

async function isAdmin(userId: string): Promise<boolean> {
  return (await member(userId))
    .permissions.has(PermissionFlagsBits.Administrator);
}

async function requireStaff(i: any, adminOnly = false) {
  const m = await member(i.user.id);

  const admin = m.permissions.has(PermissionFlagsBits.Administrator);

  if (
    !admin &&
    (adminOnly || !config || !m.roles.cache.has(config.staff_role_id))
  ) {
    throw new Error(
      adminOnly
        ? "Administrator access required."
        : "Trusted staff access required."
    );
  }
}

async function requireParticipant(userId: string, challenge: any) {
  const m = await member(userId);

  if (m.roles.cache.has(config.blocked_role_id)) {
    throw new Error("Your participation is currently restricted.");
  }

  if (challenge.author_id === userId) {
    throw new Error("You cannot enter a challenge you created.");
  }
}

function row(...components: any[]): any {
  return new ActionRowBuilder().addComponents(...components);
}

function button(
  id: string,
  text: string,
  style = ButtonStyle.Secondary
) {
  return new ButtonBuilder()
    .setCustomId(id)
    .setLabel(text)
    .setStyle(style);
}

function embed(title: string, description: string) {
  return new EmbedBuilder()
    .setColor(0x5865f2)
    .setTitle(title.slice(0, 256))
    .setDescription(description.slice(0, 4096));
}

function timestamp(value: string | Date, format = "f") {
  return `<t:${Math.floor(new Date(value).getTime() / 1000)}:${format}>`;
}

function select(
  id: string,
  placeholder: string,
  options: { label: string; value: string; description?: string }[]
) {
  return new StringSelectMenuBuilder()
    .setCustomId(id)
    .setPlaceholder(placeholder)
    .addOptions(
      options.map(o => ({
        ...o,
        label: o.label.slice(0, 100),
        description: o.description?.slice(0, 100)
      }))
    );
}

function form(
  id: string,
  title: string,
  fields: {
    id: string;
    label: string;
    value?: string;
    paragraph?: boolean;
    required?: boolean;
    max?: number;
  }[]
) {
  const modal = new ModalBuilder()
    .setCustomId(id)
    .setTitle(title.slice(0, 45));

  modal.addComponents(
    fields.map(f => {
      const input = new TextInputBuilder()
        .setCustomId(f.id)
        .setLabel(f.label.slice(0, 45))
        .setStyle(
          f.paragraph
            ? TextInputStyle.Paragraph
            : TextInputStyle.Short
        )
        .setRequired(f.required !== false)
        .setMaxLength(f.max ?? 3900);

      if (f.value) input.setValue(f.value);

      return new ActionRowBuilder<TextInputBuilder>()
        .addComponents(input);
    })
  );

  return modal;
}

async function respond(i: any, payload: any) {
  const body = typeof payload === "string"
    ? { content: payload, embeds: [], components: [] }
    : payload;

  if (i.deferred || i.replied) {
    return i.editReply(body);
  }

  return i.reply({
    ...body,
    flags: MessageFlags.Ephemeral
  });
}

async function defer(i: any) {
  if (!i.deferred && !i.replied) {
    await i.deferReply({ flags: MessageFlags.Ephemeral });
  }
}

async function audit(
  actor: string,
  kind: string,
  data: Record<string, unknown>
) {
  await db(
    supabase.from("ks_events").insert({
      guild_id: GUILD_ID,
      actor_id: actor,
      kind,
      data
    })
  );
}

async function textChannel(id: string): Promise<any> {
  const channel = await client.channels.fetch(id);

  if (!channel || !channel.isTextBased() || !("send" in channel)) {
    throw new Error(`Configured channel ${id} is missing or inaccessible.`);
  }

  return channel;
}

/**
 * Durable message identity:
 * - Store the intended message before sending.
 * - On an uncertain retry, search messages since intent creation.
 * - Never use channel names for routing.
 *
 * Requires one bot replica and Read Message History.
 */
async function stableSend(
  key: string,
  channelId: string,
  payload: any
): Promise<any> {
  let artifact = await db(
    supabase.from("ks_artifacts")
      .select("*")
      .eq("key", key)
      .maybeSingle()
  );

  const channel = await textChannel(channelId);

  if (artifact?.message_id) {
    const existing = await channel.messages
      .fetch(artifact.message_id)
      .catch(() => null);

    if (existing) return existing;

    throw new Error(
      `Recorded message was deleted or is inaccessible: ${key}. ` +
      "Staff repair is required."
    );
  }

  if (!artifact) {
    artifact = await db(
      supabase.from("ks_artifacts")
        .insert({ key, channel_id: channelId })
        .select("*")
        .single()
    );
  }

  const marker = `KS • ${key}`;
  const cutoff = new Date(artifact.created_at).getTime() - 5000;

  let before: string | undefined;

  while (true) {
    const page = await channel.messages.fetch({
      limit: 100,
      ...(before ? { before } : {})
    });

    if (!page.size) break;

    const found = page.find((m: any) =>
      m.author.id === client.user!.id &&
      m.embeds.some((e: any) => e.footer?.text === marker)
    );

    if (found) {
      await db(
        supabase.from("ks_artifacts")
          .update({ message_id: found.id })
          .eq("key", key)
      );
      return found;
    }

    const oldest: any = page.last();
    if (!oldest || oldest.createdTimestamp < cutoff) break;
    before = oldest.id;
  }

  const embeds = (payload.embeds ?? []).map((e: any) =>
    EmbedBuilder.from(e)
  );

  if (!embeds.length) embeds.push(embed("Hub Knowledge", " "));
  embeds[0].setFooter({ text: marker });

  const hash = createHash("sha256").update(key).digest();
  const nonce = hash.readBigUInt64BE(0).toString();

  const sent = await channel.send({
    ...payload,
    embeds,
    nonce,
    enforceNonce: true,
    allowedMentions: payload.allowedMentions ?? { parse: [] }
  });

  await db(
    supabase.from("ks_artifacts")
      .update({ message_id: sent.id })
      .eq("key", key)
  );

  return sent;
}

async function context(challengeId: string) {
  return db(
    supabase.from("ks_context")
      .select("*")
      .eq("id", challengeId)
      .single()
  );
}

async function answerFor(challengeId: string, userId: string) {
  return db(
    supabase.from("ks_answers")
      .select("*")
      .eq("challenge_id", challengeId)
      .eq("user_id", userId)
      .maybeSingle()
  );
}

async function currentSeason() {
  const now = new Date().toISOString();

  const active = await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .lte("starts_at", now)
      .gt("ends_at", now)
      .order("number", { ascending: false })
      .limit(1)
      .maybeSingle()
  );

  if (active) return active;

  return db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .gt("starts_at", now)
      .order("number")
      .limit(1)
      .maybeSingle()
  );
}

async function createSeason(number: number, start: Date) {
  const season = await db(
    supabase.from("ks_seasons")
      .upsert({
        guild_id: GUILD_ID,
        number,
        starts_at: start.toISOString(),
        ends_at: new Date(start.getTime() + 28 * DAY).toISOString()
      }, {
        onConflict: "guild_id,number",
        ignoreDuplicates: true
      })
      .select("*")
      .maybeSingle()
  );

  const actual = season ?? await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .eq("number", number)
      .single()
  );

  await db(
    supabase.from("ks_slots").upsert(
      makeSlots(new Date(actual.starts_at)).map(slot => ({
        ...slot,
        season_id: actual.id
      })),
      {
        onConflict: "season_id,league,opens_at",
        ignoreDuplicates: true
      }
    )
  );

  return actual;
}

async function ensureSeasons() {
  let seasons = await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .order("number")
  );

  if (!seasons.length) {
    await createSeason(1, nextMondayIST());
    seasons = await db(
      supabase.from("ks_seasons")
        .select("*")
        .eq("guild_id", GUILD_ID)
        .order("number")
    );
  }

  // Reconcile partially created seasons after a restart.
  for (const season of seasons) {
    await createSeason(season.number, new Date(season.starts_at));
  }

  let last = seasons.at(-1);

  // Maintain an upcoming season for advance preparation.
  while (new Date(last.starts_at).getTime() <= Date.now()) {
    last = await createSeason(
      last.number + 1,
      new Date(last.ends_at)
    );
  }

  // Initial setup should also expose the following season.
  if (seasons.length === 1) {
    await createSeason(last.number + 1, new Date(last.ends_at));
  }
}

async function setupServer(staffRoleId: string) {
  await requireDatabase();

  const g = await guild();
  const me = await g.members.fetchMe();

  const required = [
    PermissionFlagsBits.ManageChannels,
    PermissionFlagsBits.ManageRoles,
    PermissionFlagsBits.ManageThreads,
    PermissionFlagsBits.CreatePrivateThreads,
    PermissionFlagsBits.CreatePublicThreads,
    PermissionFlagsBits.SendMessages,
    PermissionFlagsBits.SendMessagesInThreads,
    PermissionFlagsBits.ViewChannel,
    PermissionFlagsBits.ReadMessageHistory,
    PermissionFlagsBits.EmbedLinks,
    PermissionFlagsBits.AttachFiles
  ];

  const missing = required.filter(p => !me.permissions.has(p));

  if (missing.length) {
    throw new Error(
      "The bot is missing setup permissions. Check the installation " +
      "permissions and place its role above roles it manages."
    );
  }

  const staffRole = await g.roles.fetch(staffRoleId);
  if (!staffRole || staffRole.id === g.id) {
    throw new Error("Choose a dedicated staff role, not @everyone.");
  }

  const publicCategory = await g.channels.create({
    name: "HUB KNOWLEDGE",
    type: ChannelType.GuildCategory
  });

  const privateCategory = await g.channels.create({
    name: "KNOWLEDGE STAFF",
    type: ChannelType.GuildCategory,
    permissionOverwrites: [
      {
        id: g.id,
        deny: [PermissionFlagsBits.ViewChannel]
      },
      {
        id: staffRoleId,
        allow: [
          PermissionFlagsBits.ViewChannel,
          PermissionFlagsBits.SendMessages,
          PermissionFlagsBits.ReadMessageHistory
        ]
      },
      {
        id: me.id,
        allow: [
          PermissionFlagsBits.ViewChannel,
          PermissionFlagsBits.SendMessages,
          PermissionFlagsBits.ReadMessageHistory,
          PermissionFlagsBits.ManageChannels
        ]
      }
    ]
  });

  const channels: Record<string, string> = {
    publicCategory: publicCategory.id,
    privateCategory: privateCategory.id
  };

  const publicNames: Record<string, string> = {
    hub: "knowledge-hub",
    knowledge: "knowledge-challenges",
    strategy: "strategy-challenges",
    hangar: "hangar-challenges",
    strategyAnswers: "strategy-answers",
    hangarAnswers: "hangar-answers",
    leaderboards: "leaderboards",
    hall: "hall-of-fame",
    announcements: "announcements"
  };

  for (const [key, name] of Object.entries(publicNames)) {
    const channel = await g.channels.create({
      name,
      type: ChannelType.GuildText,
      parent: publicCategory.id,
      permissionOverwrites: [
        {
          id: g.id,
          allow: [
            PermissionFlagsBits.ViewChannel,
            PermissionFlagsBits.ReadMessageHistory,
            PermissionFlagsBits.SendMessagesInThreads,
            PermissionFlagsBits.AttachFiles
          ],
          deny: [
            PermissionFlagsBits.SendMessages,
            PermissionFlagsBits.CreatePublicThreads,
            PermissionFlagsBits.CreatePrivateThreads
          ]
        },
        {
          id: me.id,
          allow: required
        }
      ]
    });

    channels[key] = channel.id;
  }

  for (const [key, name] of Object.entries({
    staff: "staff-control",
    strategyReview: "strategy-review",
    hangarReview: "hangar-review",
    audit: "audit-logs"
  })) {
    const channel = await g.channels.create({
      name,
      type: ChannelType.GuildText,
      parent: privateCategory.id
    });
    channels[key] = channel.id;
  }

  const blockedRole = await g.roles.create({
    name: "Knowledge — Nonparticipating",
    permissions: [],
    mentionable: false,
    hoist: false
  });

  await db(
    supabase.from("ks_config").insert({
      guild_id: GUILD_ID,
      staff_role_id: staffRoleId,
      blocked_role_id: blockedRole.id,
      channels
    })
  );

  await loadConfig();
  await ensureSeasons();
  await installPanels();

  await audit("BOT", "setup.completed", { channels });
}

function memberPanel() {
  return {
    embeds: [
      embed(
        "🏆 Hub Knowledge Season",
        "Four-week seasons • Monday–Saturday • 4 PM IST\n\n" +
        "Knowledge answers are private and final.\n" +
        "Strategy and Hangar answers support saved drafts and edits " +
        "until closing.\n\n" +
        "Use the buttons below."
      )
    ],
    components: [
      row(
        button("open", "Open Challenges", ButtonStyle.Primary),
        button("mine", "My Drafts / Submissions"),
        button("summary", "My Season")
      ),
      row(
        button("boards", "Leaderboards"),
        button("hall", "Hall of Fame"),
        button("rules", "Rules & Rewards")
      )
    ]
  };
}

function staffPanel() {
  return {
    embeds: [
      embed(
        "🛠️ Knowledge Staff Control",
        "All scoring and moderation decisions remain staff-controlled.\n\n" +
        "Prepare question sets, mark them Ready, review closed answers, " +
        "and approve season results."
      )
    ],
    components: [
      row(
        button("calendar:0", "Challenge Calendar", ButtonStyle.Primary),
        button("reviews", "Review Queue"),
        button("seasons", "Seasons / Finalization")
      ),
      row(
        button("participants", "Participants"),
        button("rewards", "Rewards"),
        button("settings", "Settings / Diagnostics")
      )
    ]
  };
}

async function installPanels() {
  await stableSend(
    "panel-member",
    config.channels.hub,
    memberPanel()
  );

  await stableSend(
    "panel-staff",
    config.channels.staff,
    staffPanel()
  );

  await stableSend(
    "panel-leaderboards",
    config.channels.leaderboards,
    {
      embeds: [
        embed(
          "📊 League Leaderboards",
          "Each league has independent standings.\n" +
          "Use the button for current and historical seasons."
        )
      ],
      components: [row(button("boards", "View Leaderboards"))]
    }
  );
}

async function calendar(i: any, page: number) {
  const slots = await db(
    supabase.from("ks_slots")
      .select("*, ks_seasons!inner(guild_id, number)")
      .eq("ks_seasons.guild_id", GUILD_ID)
      .gte("closes_at", new Date(Date.now() - 7 * DAY).toISOString())
      .order("opens_at")
      .range(page * 20, page * 20 + 19)
  );

  if (!slots.length) return respond(i, "No more scheduled dates.");

  await respond(i, {
    embeds: [
      embed(
        "Challenge Calendar",
        "Select a date. Add multiple questions, then mark its set Ready.\n" +
        "A date is not automatically Ready just because one question exists."
      )
    ],
    components: [
      row(select(
        "slotPick",
        "Choose date / league",
        slots.map((s: any) => ({
          label:
            `S${s.ks_seasons.number} • ` +
            new Date(s.opens_at).toLocaleDateString("en-GB", {
              timeZone: "Asia/Kolkata"
            }) +
            ` • ${s.league}`,
          value: s.id,
          description: s.state
        }))
      )),
      row(
        button(`calendar:${Math.max(0, page - 1)}`, "Previous"),
        button(`calendar:${page + 1}`, "Next")
      )
    ]
  });
}

async function slotPanel(i: any, slotId: string) {
  const slot = await db(
    supabase.from("ks_slots").select("*").eq("id", slotId).single()
  );

  const questions = await db(
    supabase.from("ks_challenges")
      .select("id,title,state")
      .eq("slot_id", slotId)
      .order("created_at")
  );

  const rows: any[] = [
    row(
      button(`create:${slot.id}`, "Add Question", ButtonStyle.Primary),
      button(`ready:${slot.id}`, "Mark Ready", ButtonStyle.Success),
      button(`cancelSlot:${slot.id}`, "Cancel Date", ButtonStyle.Danger)
    ),
    row(
      button(`late:${slot.id}`, "Approve Late Posting"),
      button(`restore:${slot.id}`, "Restore Before Opening")
    )
  ];

  if (questions.length) {
    rows.push(row(select(
      "editPick",
      "Edit a question",
      questions.slice(0, 25).map((q: any) => ({
        label: q.title,
        value: q.id,
        description: q.state
      }))
    )));
  }

  await respond(i, {
    embeds: [
      embed(
        LABELS[slot.league as League],
        `Opens: ${timestamp(slot.opens_at)}\n` +
        `Closes: ${timestamp(slot.closes_at)}\n` +
        `State: **${slot.state}**\n` +
        `Questions: **${questions.length}**`
      )
    ],
    components: rows
  });
}

async function openChallenges(i: any, mine = false) {
  const now = new Date().toISOString();

  let questions = await db(
    supabase.from("ks_context")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .eq("state", "open")
      .gt("closes_at", now)
      .order("closes_at")
  );

  if (mine) {
    const answers = await db(
      supabase.from("ks_answers")
        .select("challenge_id")
        .eq("user_id", i.user.id)
    );

    const ids = new Set(answers.map((a: any) => a.challenge_id));
    questions = questions.filter((q: any) => ids.has(q.id));
  }

  if (!questions.length) {
    return respond(
      i,
      mine
        ? "You have no drafts/submissions in currently open challenges."
        : "No challenges are currently open."
    );
  }

  await respond(i, {
    embeds: [embed("Open Challenges", "Choose a question.")],
    components: [
      row(select(
        "challengePick",
        "Choose challenge",
        questions.slice(0, 25).map((q: any) => ({
          label: q.title,
          value: q.id,
          description: LABELS[q.league as League]
        }))
      ))
    ]
  });
}

async function workspace(i: any, challengeId: string) {
  const c = await context(challengeId);
  await requireParticipant(i.user.id, c);

  if (
    c.state !== "open" ||
    Date.now() >= new Date(c.closes_at).getTime()
  ) {
    return respond(i, "This challenge is closed.");
  }

  const a = await answerFor(c.id, i.user.id);

  if (c.league === "knowledge") {
    if (a?.submitted_at) {
      return respond(
        i,
        "Your final answer is already recorded. " +
        "Correctness and results are revealed after closing."
      );
    }

    return respond(i, {
      embeds: [
        embed(
          c.title,
          c.prompt + "\n\n" +
          c.options.map((o: string, n: number) =>
            `**${String.fromCharCode(65 + n)}.** ${o}`
          ).join("\n")
        )
      ],
      components: [
        row(select(
          `choose:${c.id}`,
          "Choose one answer",
          c.options.map((o: string, n: number) => ({
            label: `${String.fromCharCode(65 + n)}. ${o}`,
            value: String(n)
          }))
        ))
      ]
    });
  }

  const sections: string[] = a?.draft ?? [];
  const uploads = a ? await db(
    supabase.from("ks_uploads")
      .select("id,filename")
      .eq("answer_id", a.id)
      .eq("active", true)
  ) : [];

  const controls = [
    row(
      button(`sectionNew:${c.id}`, "Add Section", ButtonStyle.Primary),
      button(`preview:${c.id}`, "Preview Draft"),
      button(`submit:${c.id}`, a?.submitted_at
        ? "Update Submission"
        : "Submit Answer", ButtonStyle.Success)
    ),
    row(
      button(`uploadRoom:${c.id}`, "Add Images"),
      button(`images:${c.id}`, "Manage Images"),
      button(`submitted:${c.id}`, "View Submitted Version")
    )
  ];

  if (sections.length) {
    controls.push(row(select(
      `sectionPick:${c.id}`,
      "Edit a saved section",
      sections.map((s, n) => ({
        label: `Section ${n + 1}`,
        value: String(n),
        description: s.slice(0, 80) || "Empty"
      }))
    )));
  }

  await respond(i, {
    embeds: [
      embed(
        `${LABELS[c.league as League]} — Answer Workspace`,
        `**${c.title}**\n\n` +
        `Deadline: ${timestamp(c.closes_at)}\n` +
        `Saved draft: **${wordCount(sections)} / 2,000 words**\n` +
        `Images: **${uploads.length} / 5**\n` +
        `Submitted version: **${a?.version ?? 0}**\n\n` +
        "Saving a section does not submit it. Press Submit / Update " +
        "to make the draft your official answer.\n\n" +
        "Unsaved text inside a closed Discord form cannot be recovered."
      )
    ],
    components: controls
  });
}

async function showAnswer(
  i: any,
  challengeId: string,
  submitted: boolean
) {
  const a = await answerFor(challengeId, i.user.id);

  if (!a) return respond(i, "No saved answer.");

  const sections: string[] = submitted
    ? a.body?.sections ?? []
    : a.draft ?? [];

  const body = sections.join("\n\n");

  if (!body) {
    return respond(
      i,
      submitted ? "You have not submitted an answer." : "Draft is empty."
    );
  }

  await respond(i, {
    embeds: [
      embed(
        submitted ? "Submitted Answer" : "Saved Draft",
        body.slice(0, 3800)
      )
    ],
    files: [
      new AttachmentBuilder(Buffer.from(body, "utf8"), {
        name: submitted ? "submitted-answer.txt" : "draft-answer.txt"
      })
    ],
    components: [
      row(button(`work:${challengeId}`, "Back to Workspace"))
    ]
  });
}

async function createUploadRoom(i: any, challengeId: string) {
  const c = await context(challengeId);
  await requireParticipant(i.user.id, c);

  if (c.league === "knowledge") {
    throw new Error("Trivia does not accept image submissions.");
  }

  let a = await answerFor(c.id, i.user.id);

  if (!a) {
    await rpc("ks_answer_action", {
      p_actor: i.user.id,
      p_challenge: c.id,
      p_kind: "save",
      p_payload: { sections: [] }
    });
    a = await answerFor(c.id, i.user.id);
  }

  if (Date.now() >= new Date(c.closes_at).getTime()) {
    throw new Error("The submission deadline has passed.");
  }

  const existing = await db(
    supabase.from("ks_workspaces")
      .select("*")
      .eq("challenge_id", c.id)
      .eq("user_id", i.user.id)
      .maybeSingle()
  );

  let thread: any = existing
    ? await client.channels.fetch(existing.thread_id).catch(() => null)
    : null;

  if (!thread) {
    const parent: any = await textChannel(config.channels.hub);

    thread = await parent.threads.create({
      name: `Answer images • ${i.user.id.slice(-6)}`,
      type: ChannelType.PrivateThread,
      invitable: false,
      autoArchiveDuration: ThreadAutoArchiveDuration.OneDay,
      reason: "Private Knowledge Season image workspace"
    });

    await thread.members.add(i.user.id);

    await db(
      supabase.from("ks_workspaces").upsert({
        thread_id: thread.id,
        challenge_id: c.id,
        user_id: i.user.id
      }, {
        onConflict: "challenge_id,user_id"
      })
    );

    await thread.send({
      content:
        "Upload up to **5 images**, **10 MB each**.\n" +
        "Supported: PNG, JPEG, WEBP, GIF.\n\n" +
        "Images are added to your draft. Return to the answer workspace " +
        "and press **Submit / Update Submission** to include them.\n\n" +
        "This thread is private to you and moderators with thread access.",
      components: [
        row(button(`work:${c.id}`, "Open Answer Workspace"))
      ]
    });
  } else {
    if (thread.archived) await thread.setArchived(false);
    await thread.members.add(i.user.id);
  }

  await respond(i, `Upload your images here: <#${thread.id}>`);
}

async function standings(seasonId: string, league: League) {
  const rows = await db(
    supabase.from("ks_standings")
      .select("*")
      .eq("season_id", seasonId)
      .eq("league", league)
  );

  return rows.sort(compareStanding);
}

async function chooseSeason(i: any, action: string) {
  const seasons = await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .order("number", { ascending: false })
      .limit(25)
  );

  await respond(i, {
    embeds: [embed("Choose Season", "Select a season below.")],
    components: [
      row(select(
        `${action}Season`,
        "Season",
        seasons.map((s: any) => ({
          label: `Season ${s.number}`,
          value: s.id,
          description: s.state
        }))
      ))
    ]
  });
}

async function showBoards(i: any, seasonId: string) {
  const s = await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("id", seasonId)
      .single()
  );

  const embeds: EmbedBuilder[] = [];

  for (const league of LEAGUES) {
    const rows = await standings(seasonId, league);
    const lines: string[] = [];

    let position = 0;
    let previous: any = null;

    for (let n = 0; n < Math.min(rows.length, 10); n++) {
      const r = rows[n];

      if (!previous || compareStanding(previous, r) !== 0) {
        position = n + 1;
      }

      lines.push(
        `**${position}.** ${await displayName(r.user_id)} — **${r.points}**`
      );
      previous = r;
    }

    embeds.push(
      embed(
        `Season ${s.number} • ${LABELS[league]}`,
        lines.join("\n") || "No published scores yet."
      )
    );
  }

  await respond(i, {
    embeds,
    components: [
      row(button(`export:${seasonId}`, "Staff CSV Export"))
    ]
  });
}

async function personalSummary(i: any) {
  const s = await currentSeason();
  if (!s) return respond(i, "No season is configured.");

  const lines: string[] = [];

  for (const league of LEAGUES) {
    const rows = await standings(s.id, league);
    const index = rows.findIndex((r: any) => r.user_id === i.user.id);

    if (index < 0) {
      lines.push(`${LABELS[league]}: **0 published points**`);
      continue;
    }

    const rank = rows.filter((r: any) =>
      compareStanding(r, rows[index]) < 0
    ).length + 1;

    lines.push(
      `${LABELS[league]}: **${rows[index].points} points**, rank **${rank}**`
    );
  }

  await respond(i, {
    embeds: [
      embed(
        `My Season — Season ${s.number}`,
        lines.join("\n\n") +
        "\n\nOpen trivia scores remain hidden until closing."
      )
    ]
  });
}

async function reviewQueue(i: any) {
  const rows = await db(
    supabase.from("ks_answers")
      .select("id,challenge_id,user_id,score,disqualified")
      .not("submitted_at", "is", null)
      .order("submitted_at")
      .limit(500)
  );

  const eligible: any[] = [];

  for (const a of rows) {
    if (a.score !== null || a.disqualified) continue;

    const c = await context(a.challenge_id);

    if (
      c.league !== "knowledge" &&
      c.season_state === "open" &&
      c.state !== "cancelled" &&
      c.slot_state !== "cancelled" &&
      Date.now() >= new Date(c.closes_at).getTime()
    ) {
      eligible.push({ ...a, challenge: c });
    }

    if (eligible.length === 25) break;
  }

  if (!eligible.length) return respond(i, "No unscored closed answers.");

  await respond(i, {
    embeds: [embed("Review Queue", "Choose an answer to review.")],
    components: [
      row(select(
        "reviewPick",
        "Submission",
        await Promise.all(eligible.map(async a => ({
          label: `${await displayName(a.user_id)} • ${a.challenge.title}`,
          value: a.id,
          description: LABELS[a.challenge.league as League]
        })))
      ))
    ]
  });
}

async function reviewAnswer(i: any, answerId: string) {
  const a = await db(
    supabase.from("ks_answers").select("*").eq("id", answerId).single()
  );

  const c = await context(a.challenge_id);

  const body = c.league === "knowledge"
    ? `Selected: ${c.options[a.choice]}\nBase score: ${a.score}`
    : (a.body?.sections ?? []).join("\n\n");

  await respond(i, {
    embeds: [
      embed(
        `Review • ${await displayName(a.user_id)}`,
        `**${c.title}**\n` +
        `Version: ${a.version}\n` +
        `Score: ${a.score ?? "Not scored"}\n` +
        `Disqualified: ${a.disqualified ? "Yes" : "No"}\n\n` +
        body.slice(0, 3000)
      )
    ],
    files: [
      new AttachmentBuilder(Buffer.from(body, "utf8"), {
        name: "answer.txt"
      })
    ],
    components: [
      row(
        button(
          `score:${a.id}:${a.version}`,
          "Score / Adjust",
          ButtonStyle.Success
        ),
        button(`history:${a.id}`, "Version History")
      )
    ]
  });

  if (c.league !== "knowledge") {
    const uploads = await submissionUploads(a);
    for (const upload of uploads) {
      const { data, error } = await supabase.storage
        .from(BUCKET)
        .download(upload.path);

      if (error) throw new Error(error.message);

      await i.followUp({
        files: [
          new AttachmentBuilder(
            Buffer.from(await data.arrayBuffer()),
            { name: upload.filename }
          )
        ],
        flags: MessageFlags.Ephemeral
      });
    }
  }
}

async function submissionUploads(answer: any): Promise<any[]> {
  const ids: string[] = answer.body?.uploads ?? [];
  if (!ids.length) return [];

  return db(
    supabase.from("ks_uploads")
      .select("*")
      .in("id", ids)
  );
}

async function seasonControl(i: any, seasonId: string) {
  const s = await db(
    supabase.from("ks_seasons").select("*").eq("id", seasonId).single()
  );

  await respond(i, {
    embeds: [
      embed(
        `Season ${s.number}`,
        `Starts: ${timestamp(s.starts_at)}\n` +
        `Ends: ${timestamp(s.ends_at)}\n` +
        `State: **${s.state}**\n\n` +
        "Finalization requires all valid answers to be scored and " +
        "all top-10 ties to be resolved.\n\n" +
        "Finalized award records are frozen."
      )
    ],
    components: [
      row(
        button(`board:${s.id}`, "View Standings"),
        button(`tie:${s.id}`, "Record Tie-break"),
        button(`finalPreview:${s.id}`, "Finalization Preview")
      )
    ]
  });
}

async function finalizationData(seasonId: string) {
  const rows = await db(
    supabase.from("ks_standings")
      .select("*")
      .eq("season_id", seasonId)
      .gt("points", 0)
  );

  const absent: string[] = [];
  const names: Record<string, string> = {};

  for (const userId of [...new Set<string>(
    rows.map((r: any) => r.user_id)
  )]) {
    try {
      names[userId] = (await member(userId)).displayName;
    } catch (error: any) {
      if (Number(error.code) === 10007) {
        absent.push(userId);
      } else {
        throw new Error(
          `Could not verify membership for ${userId}. ` +
          "No winner will be skipped because of a temporary API error."
        );
      }
    }
  }

  return { rows, absent, names };
}

async function finalPreview(i: any, seasonId: string) {
  const s = await db(
    supabase.from("ks_seasons").select("*").eq("id", seasonId).single()
  );

  const data = await finalizationData(seasonId);
  const lines: string[] = [];

  for (const league of LEAGUES) {
    const eligible = data.rows
      .filter((r: any) =>
        r.league === league && !data.absent.includes(r.user_id)
      )
      .sort(compareStanding);

    lines.push(
      `**${LABELS[league]}**\n` +
      eligible.slice(0, 10).map((r: any, n: number) =>
        `${n + 1}. ${data.names[r.user_id]} — ${r.points}`
      ).join("\n")
    );
  }

  await respond(i, {
    embeds: [
      embed(
        `Approve Season ${s.number}?`,
        lines.join("\n\n") +
        `\n\nDeparted members excluded: ${data.absent.length}\n\n` +
        "Pressing Approve recalculates and validates the final standings " +
        "inside a database transaction."
      )
    ],
    components: [
      row(button(
        `finalize:${seasonId}`,
        "Approve Final Results",
        ButtonStyle.Danger
      ))
    ]
  });
}

async function rewardsPanel(i: any, seasonId: string) {
  const awards = await db(
    supabase.from("ks_awards")
      .select("*")
      .eq("season_id", seasonId)
      .order("league")
      .order("placement")
  );

  if (!awards.length) return respond(i, "No approved awards for this season.");

  const lines = awards.map((a: any) =>
    `${a.league} #${a.placement} • ${a.display_name} • ` +
    `${a.coins} ${config.currency_name} • **${a.reward_status}**`
  );

  await respond(i, {
    embeds: [embed("Manual Reward Ledger", lines.join("\n"))],
    components: [
      row(select(
        "rewardPick",
        "Select reward",
        awards.slice(0, 25).map((a: any) => ({
          label: `${a.league} #${a.placement} • ${a.display_name}`,
          value: a.id,
          description: `${a.coins} • ${a.reward_status}`
        }))
      )),
      ...(awards.length > 25 ? [
        row(select(
          "rewardPickMore",
          "Remaining rewards",
          awards.slice(25).map((a: any) => ({
            label: `${a.league} #${a.placement} • ${a.display_name}`,
            value: a.id,
            description: `${a.coins} • ${a.reward_status}`
          }))
        ))
      ] : [])
    ]
  });
}

async function exportSeason(i: any, seasonId: string) {
  const rows = await db(
    supabase.from("ks_standings")
      .select("*")
      .eq("season_id", seasonId)
  );

  function cell(value: unknown) {
    let text = String(value ?? "");
    if (/^[=+\-@]/.test(text)) text = "'" + text;
    return `"${text.replaceAll('"', '""')}"`;
  }

  const csv = [
    ["league", "user_id", "points", "correct_count", "tiebreak"],
    ...rows.map((r: any) => [
      r.league, r.user_id, r.points, r.correct_count, r.tiebreak
    ])
  ].map(r => r.map(cell).join(",")).join("\r\n");

  await respond(i, {
    content: "Season standings export.",
    files: [
      new AttachmentBuilder(Buffer.from(csv, "utf8"), {
        name: `season-${seasonId}.csv`
      })
    ],
    embeds: [],
    components: []
  });
}

async function publishChallenge(c: any) {
  const isTrivia = c.league === "knowledge";

  const message = await stableSend(
    `challenge-${c.id}`,
    config.channels[c.league],
    {
      embeds: [
        embed(
          c.title,
          c.prompt + "\n\n" +
          (isTrivia
            ? c.options.map((o: string, n: number) =>
              `**${String.fromCharCode(65 + n)}.** ${o}`
            ).join("\n") + "\n\n"
            : "") +
          `Closes: ${timestamp(c.closes_at)}\n` +
          (isTrivia
            ? `Correct: ${c.points} points • First correct: +1`
            : "Staff score: 0–10 • Maximum 2,000 words")
        )
      ],
      components: [
        row(button(
          `work:${c.id}`,
          isTrivia ? "Answer Privately" : "Start / Resume Answer",
          ButtonStyle.Primary
        ))
      ]
    }
  );

  await db(
    supabase.from("ks_challenges")
      .update({ state: "open", message_id: message.id })
      .eq("id", c.id)
  );
}

async function publishScoredAnswer(a: any, c: any) {
  const channelId = config.channels[`${c.league}Answers`];
  const parent: any = await textChannel(channelId);

  const header = await stableSend(
    `answer-thread-${c.id}`,
    channelId,
    {
      embeds: [
        embed(
          `Scored Answers • ${c.title}`,
          "Staff-scored submissions are published in this thread."
        )
      ]
    }
  );

  let thread: any = header.thread;

  if (!thread) {
    thread = await client.channels.fetch(header.id).catch(() => null);
  }

  if (!thread) {
    thread = await header.startThread({
      name: c.title.slice(0, 95),
      autoArchiveDuration: ThreadAutoArchiveDuration.OneWeek
    });
  }

  if (thread.archived) await thread.setArchived(false);

  const name = await displayName(a.user_id);
  const body = (a.body?.sections ?? []).join("\n\n");

  for (const [n, part] of chunks(body).entries()) {
    await stableSend(
      `published-${a.id}-v${a.version}-part${n}`,
      thread.id,
      {
        embeds: [
          embed(
            n === 0
              ? `${name} • ${a.score}/10`
              : `${name} • Continued`,
            part
          )
        ]
      }
    );
  }

  for (const upload of await submissionUploads(a)) {
    const { data, error } = await supabase.storage
      .from(BUCKET)
      .download(upload.path);

    if (error) throw new Error(error.message);

    await stableSend(
      `published-${a.id}-v${a.version}-image-${upload.id}`,
      thread.id,
      {
        embeds: [embed(`${name} • Image`, "Submission attachment")],
        files: [
          new AttachmentBuilder(
            Buffer.from(await data.arrayBuffer()),
            { name: upload.filename }
          )
        ]
      }
    );
  }

  // Remove ordinary discussion access once publication work finishes.
  await thread.setLocked(true);

  await db(
    supabase.from("ks_answers")
      .update({ published_version: a.version })
      .eq("id", a.id)
  );

  void parent;
}

async function workerSlots() {
  const now = Date.now();

  const slots = await db(
    supabase.from("ks_slots")
      .select("*, ks_seasons!inner(guild_id,number)")
      .eq("ks_seasons.guild_id", GUILD_ID)
      .neq("state", "cancelled")
      .neq("state", "posted")
      .lte("opens_at", new Date(now + DAY).toISOString())
  );

  for (const slot of slots) {
    const opens = new Date(slot.opens_at).getTime();
    const closes = new Date(slot.closes_at).getTime();
    const remaining = opens - now;

    if (remaining > 0 && slot.state === "draft") {
      const hours = remaining / 3_600_000;

      const stage = hours <= 1 ? 1
        : hours <= 2 ? 2
        : hours <= 3 ? 3
        : hours <= 4 ? 4
        : 24;

      await stableSend(
        `reminder-${slot.id}-${stage}`,
        config.channels.staff,
        {
          content: `<@&${config.staff_role_id}>`,
          allowedMentions: { roles: [config.staff_role_id] },
          embeds: [
            embed(
              "Challenge Preparation Reminder",
              `${LABELS[slot.league as League]}\n` +
              `Posting: ${timestamp(slot.opens_at)}\n\n` +
              "The question set is not marked Ready."
            )
          ],
          components: [
            row(button(`slot:${slot.id}`, "Prepare This Date"))
          ]
        }
      );
    }

    if (remaining > 0) continue;

    const onTime = now <= opens + 120_000;

    if (
      slot.state === "ready" &&
      now < closes &&
      (onTime || slot.late_approved)
    ) {
      const questions = await db(
        supabase.from("ks_context")
          .select("*")
          .eq("slot_id", slot.id)
          .neq("state", "cancelled")
          .order("created_at")
      );

      if (!questions.length) continue;

      for (const c of questions) {
        if (c.state === "scheduled") await publishChallenge(c);
      }

      await db(
        supabase.from("ks_slots")
          .update({ state: "posted" })
          .eq("id", slot.id)
      );

      continue;
    }

    if (slot.state !== "missed") {
      await db(
        supabase.from("ks_slots")
          .update({ state: "missed" })
          .eq("id", slot.id)
      );

      await stableSend(
        `missed-${slot.id}`,
        config.channels.staff,
        {
          content: `<@&${config.staff_role_id}>`,
          allowedMentions: { roles: [config.staff_role_id] },
          embeds: [
            embed(
              "Posting Requires Staff Action",
              `${LABELS[slot.league as League]}\n` +
              `Original opening: ${timestamp(slot.opens_at)}\n` +
              `Original closing: ${timestamp(slot.closes_at)}\n\n` +
              "No deadline was extended automatically."
            )
          ],
          components: [
            row(button(`slot:${slot.id}`, "Open Recovery Controls"))
          ]
        }
      );
    }
  }
}

async function workerCloseChallenges() {
  const rows = await db(
    supabase.from("ks_context")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .eq("state", "open")
      .lte("closes_at", new Date().toISOString())
  );

  for (const c of rows) {
    if (c.message_id) {
      const channel = await textChannel(config.channels[c.league]);
      const message = await channel.messages.fetch(c.message_id);

      await message.edit({
        components: [
          row(
            button(`work:${c.id}`, "Closed")
              .setDisabled(true)
          )
        ]
      });
    }

    await db(
      supabase.from("ks_challenges")
        .update({ state: "closed" })
        .eq("id", c.id)
    );

    if (c.league === "knowledge") {
      await stableSend(
        `trivia-result-${c.id}`,
        config.channels.knowledge,
        {
          embeds: [
            embed(
              `Answer Revealed • ${c.title}`,
              `Correct answer: **${c.options[c.correct_index]}**\n\n` +
              "Scores and the first-correct bonus are now included " +
              "in the Knowledge leaderboard."
            )
          ]
        }
      );
    }

    const rooms = await db(
      supabase.from("ks_workspaces")
        .select("*")
        .eq("challenge_id", c.id)
    );

    for (const room of rooms) {
      const thread: any = await client.channels
        .fetch(room.thread_id)
        .catch(() => null);

      if (thread) {
        await thread.setLocked(true);
        await thread.setArchived(true);
      }
    }
  }
}

async function workerReviews() {
  const answers = await db(
    supabase.from("ks_answers")
      .select("*")
      .not("submitted_at", "is", null)
      .order("submitted_at")
      .limit(1000)
  );

  for (const a of answers) {
    const c = await context(a.challenge_id);

    if (
      c.league === "knowledge" ||
      c.state === "cancelled" ||
      c.slot_state === "cancelled" ||
      Date.now() < new Date(c.closes_at).getTime()
    ) continue;

    if (!a.review_message_id) {
      const body = (a.body?.sections ?? []).join("\n\n");

      const message = await stableSend(
        `review-${a.id}`,
        config.channels[`${c.league}Review`],
        {
          embeds: [
            embed(
              `${await displayName(a.user_id)} • ${c.title}`,
              `Version ${a.version}\n\n${body.slice(0, 3000)}`
            )
          ],
          files: [
            new AttachmentBuilder(Buffer.from(body, "utf8"), {
              name: "answer.txt"
            })
          ],
          components: [
            row(button(`review:${a.id}`, "Review / Score"))
          ]
        }
      );

      await db(
        supabase.from("ks_answers")
          .update({ review_message_id: message.id })
          .eq("id", a.id)
      );
    }

    if (
      a.score !== null &&
      !a.disqualified &&
      a.published_version !== a.version
    ) {
      await publishScoredAnswer(a, c);
    }
  }
}

async function workerAudit() {
  const events = await db(
    supabase.from("ks_events")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .eq("delivered", false)
      .order("id")
      .limit(30)
  );

  for (const event of events) {
    await stableSend(
      `audit-${event.id}`,
      config.channels.audit,
      {
        embeds: [
          embed(
            event.kind,
            `Actor: ${event.actor_id}\n` +
            `Time: ${timestamp(event.created_at)}\n\n` +
            JSON.stringify(event.data, null, 2).slice(0, 3200)
          )
        ]
      }
    );

    await db(
      supabase.from("ks_events")
        .update({ delivered: true })
        .eq("id", event.id)
    );
  }
}

async function workerFinalization() {
  const seasons = await db(
    supabase.from("ks_seasons")
      .select("*")
      .eq("guild_id", GUILD_ID)
      .eq("state", "finalizing")
  );

  const g = await guild();

  for (const s of seasons) {
    const champions = await db(
      supabase.from("ks_awards")
        .select("*")
        .eq("season_id", s.id)
        .eq("placement", 1)
    );

    for (const award of champions) {
      if (award.role_done) continue;

      const league = award.league as League;
      let role = award.role_id
        ? await g.roles.fetch(award.role_id)
        : null;

      if (award.role_id && !role) {
        throw new Error(
          `Season ${s.number} champion role was deleted. Staff repair required.`
        );
      }

      if (!role) {
        const initialName = `${TITLES[league]} — Season ${s.number}`;

        // Reconcile a crash between role creation and DB recording.
        const allRoles = await g.roles.fetch();
        const candidates = allRoles.filter(r => r.name === initialName);

        if (candidates.size > 1) {
          throw new Error(
            `Ambiguous champion roles for ${initialName}. Staff repair required.`
          );
        }

        role = candidates.first() ?? await g.roles.create({
          name: initialName,
          color: COLORS[league],
          hoist: true,
          mentionable: false,
          permissions: [],
          reason: `Approved Season ${s.number} champion`
        });

        await db(
          supabase.from("ks_awards")
            .update({ role_id: role.id })
            .eq("id", award.id)
        );
      }

      try {
        await (await member(award.user_id)).roles.add(role.id);
      } catch (error: any) {
        if (Number(error.code) !== 10007) throw error;

        // Award was already finalized: preserve historical winner.
        await audit("BOT", "award.member_left_after_approval", {
          award_id: award.id,
          user_id: award.user_id
        });
      }

      await db(
        supabase.from("ks_awards")
          .update({ role_done: true })
          .eq("id", award.id)
      );
    }

    const lines = LEAGUES.map(league => {
      const a = champions.find((x: any) => x.league === league);
      return `${TITLES[league]} — **${a?.display_name ?? "No eligible champion"}**`;
    });

    await stableSend(
      `hall-season-${s.id}`,
      config.channels.hall,
      {
        embeds: [
          embed(`🏆 Season ${s.number} — Hall of Fame`, lines.join("\n\n"))
        ]
      }
    );

    await stableSend(
      `announcement-season-${s.id}`,
      config.channels.announcements,
      {
        embeds: [
          embed(
            `Season ${s.number} Champions`,
            lines.join("\n\n") +
            "\n\nCoin rewards are approved and distributed manually by staff."
          )
        ]
      }
    );

    await db(
      supabase.from("ks_seasons")
        .update({
          state: "finalized",
          finalized_at: new Date().toISOString()
        })
        .eq("id", s.id)
    );
  }
}

async function workerBoards() {
  const season = await currentSeason();
  if (!season) return;

  for (const league of LEAGUES) {
    const rows = await standings(season.id, league);
    const lines: string[] = [];
    let rank = 0;

    for (let n = 0; n < Math.min(rows.length, 10); n++) {
      if (n === 0 || compareStanding(rows[n - 1], rows[n]) !== 0) {
        rank = n + 1;
      }

      lines.push(
        `**${rank}.** ${await displayName(rows[n].user_id)} — ` +
        `**${rows[n].points}**`
      );
    }

    const message = await stableSend(
      `live-board-${season.id}-${league}`,
      config.channels.leaderboards,
      {
        embeds: [
          embed(
            `Season ${season.number} • ${LABELS[league]}`,
            lines.join("\n") || "No published scores yet."
          )
        ]
      }
    );

    const next = embed(
      `Season ${season.number} • ${LABELS[league]}`,
      lines.join("\n") || "No published scores yet."
    ).setFooter({
      text: `KS • live-board-${season.id}-${league}`
    });

    if (message.embeds[0]?.description !== next.data.description) {
      await message.edit({ embeds: [next] });
    }
  }
}

let lastBoards = 0;

async function tick() {
  if (workerRunning || shuttingDown || !config) return;
  workerRunning = true;

  try {
    await ensureSeasons();

    // One failing subsystem should not stop the others.
    const jobs = [
      workerSlots,
      workerCloseChallenges,
      workerReviews,
      workerFinalization,
      workerAudit
    ];

    for (const job of jobs) {
      try {
        await job();
      } catch (error) {
        console.error(`[worker:${job.name}]`, error);
      }
    }

    if (Date.now() - lastBoards > 120_000) {
      try {
        await workerBoards();
        lastBoards = Date.now();
      } catch (error) {
        console.error("[worker:boards]", error);
      }
    }
  } finally {
    workerRunning = false;
  }
}

client.on(Events.MessageCreate, async message => {
  if (
    message.author.bot ||
    message.guildId !== GUILD_ID ||
    !message.channel.isThread() ||
    !message.attachments.size ||
    !config
  ) return;

  const workspace = await db(
    supabase.from("ks_workspaces")
      .select("*")
      .eq("thread_id", message.channelId)
      .maybeSingle()
  ).catch(() => null);

  if (!workspace || workspace.user_id !== message.author.id) return;

  const lockKey = `${workspace.challenge_id}:${message.author.id}`;

  if (imageLocks.has(lockKey)) {
    await message.reply("Please wait for the previous image upload to finish.");
    return;
  }

  imageLocks.add(lockKey);

  try {
    const c = await context(workspace.challenge_id);
    await requireParticipant(message.author.id, c);

    if (
      c.state !== "open" ||
      Date.now() >= new Date(c.closes_at).getTime()
    ) throw new Error("This challenge is closed.");

    const answer = await answerFor(c.id, message.author.id);
    if (!answer) throw new Error("Open your draft workspace first.");

    const existing = await db(
      supabase.from("ks_uploads")
        .select("id")
        .eq("answer_id", answer.id)
        .eq("active", true)
    );

    if (existing.length + message.attachments.size > 5) {
      throw new Error("Maximum five active images per answer.");
    }

    for (const attachment of message.attachments.values()) {
      if (attachment.size > 10 * 1024 * 1024) {
        throw new Error("Each image must be 10 MB or smaller.");
      }

      const url = new URL(attachment.url);

      if (
        url.protocol !== "https:" ||
        !["cdn.discordapp.com", "media.discordapp.net"].includes(url.hostname)
      ) {
        throw new Error("Unsupported attachment host.");
      }

      const response = await fetch(url, {
        signal: AbortSignal.timeout(30_000),
        redirect: "error"
      });

      if (!response.ok) throw new Error("Could not download the attachment.");

      const buffer = Buffer.from(await response.arrayBuffer());

      if (buffer.length > 10 * 1024 * 1024) {
        throw new Error("Downloaded image exceeds 10 MB.");
      }

      const mime = imageMime(buffer);
      if (!mime) throw new Error("Use PNG, JPEG, WEBP, or GIF images.");

      const extension: Record<string, string> = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif"
      };

      const id = randomUUID();
      const filename = `${id}.${extension[mime]}`;
      const path = `${GUILD_ID}/${answer.id}/${filename}`;

      const { error } = await supabase.storage.from(BUCKET).upload(
        path,
        buffer,
        { contentType: mime, upsert: false }
      );

      if (error) throw new Error(error.message);

      try {
        // Check the deadline again after the network transfer.
        if (Date.now() >= new Date(c.closes_at).getTime()) {
          throw new Error("The deadline passed during upload.");
        }

        await db(
          supabase.from("ks_uploads").insert({
            id,
            answer_id: answer.id,
            user_id: message.author.id,
            path,
            filename,
            mime,
            bytes: buffer.length
          })
        );
      } catch (error) {
        await supabase.storage.from(BUCKET).remove([path]);
        throw error;
      }
    }

    await message.reply(
      "Images saved to your draft. Press **Submit / Update Submission** " +
      "in your answer workspace to include them."
    );
  } catch (error: any) {
    console.error("[upload]", error);
    await message.reply(`Upload failed: ${error.message}`);
  } finally {
    imageLocks.delete(lockKey);
  }
});

client.on(Events.InteractionCreate, async (i: any) => {
  if (
    !i.isChatInputCommand() &&
    !i.isButton() &&
    !i.isStringSelectMenu() &&
    !i.isRoleSelectMenu() &&
    !i.isUserSelectMenu() &&
    !i.isModalSubmit()
  ) return;

  try {
    if (i.guildId !== GUILD_ID) {
      throw new Error("This bot operates only in its configured server.");
    }

    if (i.isChatInputCommand()) {
      await requireStaff(i, true);

      if (i.commandName === "setup") {
        if (config) {
          return respond(
            i,
            "Setup already exists. Use /panels to recover panel messages. " +
            "Renaming channels and roles does not require setup again."
          );
        }

        return respond(i, {
          embeds: [
            embed(
              "Knowledge Season Setup",
              "Choose your trusted staff role.\n\n" +
              "The bot will create the channel layout, a Nonparticipating " +
              "role, and the panels. Season 1 starts automatically on " +
              "the next Monday at 00:00 IST. First challenges open at 4 PM.\n\n" +
              "Do not run setup while another setup is in progress."
            )
          ],
          components: [
            row(
              new RoleSelectMenuBuilder()
                .setCustomId("setupRole")
                .setPlaceholder("Choose trusted staff role")
                .setMinValues(1)
                .setMaxValues(1)
            )
          ]
        });
      }

      if (i.commandName === "panels") {
        if (!config) throw new Error("Run /setup first.");
        await defer(i);

        // Explicit admin recovery of root panels only.
        await db(
          supabase.from("ks_artifacts")
            .delete()
            .in("key", [
              "panel-member",
              "panel-staff",
              "panel-leaderboards"
            ])
        );

        await installPanels();
        return respond(
          i,
          "New root panels posted. Older panels, if still present, " +
          "remain usable and can be deleted manually."
        );
      }
    }

    const [action, id, extra] = i.customId.split(":");

    if (action === "setupRole") {
      await requireStaff(i, true);
      if (config) throw new Error("Setup already completed.");
      if (setupRunning) throw new Error("Setup is already running.");

      setupRunning = true;
      await defer(i);

      try {
        await setupServer(i.values[0]);
        return respond(
          i,
          "Setup complete. Rename channels/categories/roles freely. " +
          "The bot tracks their IDs."
        );
      } finally {
        setupRunning = false;
      }
    }

    if (!config) throw new Error("An administrator must run /setup first.");

    const staffActions = new Set([
      "calendar", "slotPick", "slot",
      "create", "createForm", "editPick", "editForm",
      "ready", "cancelSlot", "cancelForm", "late", "restore",
      "reviews", "reviewPick", "review", "score", "scoreForm", "history",
      "seasons", "controlSeason", "tie", "tieForm",
      "participants", "participantPick", "block", "unblock",
      "rewards", "rewardSeason", "rewardPick", "rewardPickMore",
      "rewardSet", "export"
    ]);

    const adminActions = new Set([
      "finalPreview", "finalize", "settings", "currencyForm"
    ]);

    if (staffActions.has(action)) await requireStaff(i);
    if (adminActions.has(action)) await requireStaff(i, true);

    // Forms must be shown before deferring the interaction.
    if (action === "create") {
      const slot = await db(
        supabase.from("ks_slots").select("*").eq("id", id).single()
      );

      if (Date.now() >= new Date(slot.opens_at).getTime()) {
        throw new Error("New questions must be created before opening.");
      }

      return i.showModal(form(
        `createForm:${id}`,
        "Create Challenge",
        [
          { id: "title", label: "Title", max: 150 },
          { id: "prompt", label: "Question / scenario", paragraph: true, max: 2500 },
          ...(slot.league === "knowledge" ? [
            { id: "points", label: "Correct-answer points: 1–10", max: 2 },
            {
              id: "options",
              label: "2–5 lines; prefix correct option with *",
              paragraph: true,
              max: 750
            }
          ] : [])
        ]
      ));
    }

    if (action === "editPick") {
      const c = await context(i.values[0]);

      return i.showModal(form(
        `editForm:${c.id}`,
        "Edit Challenge Wording",
        [
          { id: "title", label: "Title", value: c.title, max: 150 },
          {
            id: "prompt",
            label: "Prompt / wording correction",
            value: c.prompt,
            paragraph: true,
            max: 2500
          },
          {
            id: "reason",
            label: "Reason for this correction",
            paragraph: true,
            max: 500
          }
        ]
      ));
    }

    if (action === "cancelSlot") {
      return i.showModal(form(
        `cancelForm:${id}`,
        "Cancel Scheduled Date",
        [{ id: "reason", label: "Cancellation reason", paragraph: true, max: 500 }]
      ));
    }

    if (action === "sectionNew" || action === "sectionPick") {
      const c = await context(id);
      await requireParticipant(i.user.id, c);

      const a = await answerFor(id, i.user.id);
      const sections: string[] = a?.draft ?? [];

      const index = action === "sectionNew"
        ? sections.length
        : Number(i.values[0]);

      if (index >= 10) throw new Error("Maximum 10 sections.");

      return i.showModal(form(
        `sectionForm:${id}:${index}`,
        `Answer Section ${index + 1}`,
        [{
          id: "text",
          label: "Save text; empty removes this section",
          value: sections[index] ?? "",
          paragraph: true,
          required: false,
          max: 3900
        }]
      ));
    }

    if (action === "score") {
      const a = await db(
        supabase.from("ks_answers")
          .select("*")
          .eq("id", id)
          .single()
      );

      return i.showModal(form(
        `scoreForm:${id}:${extra}`,
        "Score / Adjust Answer",
        [
          {
            id: "score",
            label: "Whole-number score: 0–10",
            value: String(a.score ?? ""),
            max: 2
          },
          {
            id: "dq",
            label: "Disqualified? yes / no",
            value: a.disqualified ? "yes" : "no",
            max: 3
          },
          {
            id: "reason",
            label: "Feedback / reason (required)",
            paragraph: true,
            max: 1000
          }
        ]
      ));
    }

    if (action === "tie") {
      return i.showModal(form(
        `tieForm:${id}`,
        "Record Staff Tie-break Result",
        [
          { id: "league", label: "knowledge / strategy / hangar", max: 12 },
          { id: "user", label: "Participant Discord user ID", max: 22 },
          { id: "value", label: "Tie-break value; higher wins", max: 8 },
          {
            id: "reason",
            label: "Describe the actual tie-break result",
            paragraph: true,
            max: 1000
          }
        ]
      ));
    }

    if (action === "settings") {
      return i.showModal(form(
        "currencyForm",
        "Server Settings",
        [{
          id: "currency",
          label: "Currency display name",
          value: config.currency_name,
          max: 50
        }]
      ));
    }

    await defer(i);

    if (action === "open") return openChallenges(i);
    if (action === "mine") return openChallenges(i, true);
    if (action === "summary") return personalSummary(i);

    if (action === "rules") {
      return respond(i, {
        embeds: [
          embed(
            "Rules & Rewards",
            "• Four-week seasons; Monday–Saturday openings at 4 PM IST.\n" +
            "• Every question lasts 24 hours; Saturday closes Sunday.\n" +
            "• Trivia: private MCQ, one final answer, 1–10 points and +1 first correct.\n" +
            "• Other leagues: saved drafts, edits until deadline, staff scores 0–10.\n" +
            "• Maximum 2,000 words and 20,000 characters per long answer.\n" +
            "• No copying, manipulation, harassment, or exploit abuse.\n" +
            "• Staff decisions and penalties are logged.\n" +
            "• Challenge authors cannot enter their own challenges.\n\n" +
            `**${config.currency_name} per league**\n` +
            "1st 3,000 • 2nd 2,000 • 3rd 1,000 • 4th 800 • 5th 700\n" +
            "6th 600 • 7th 500 • 8th 400 • 9th 300 • 10th 200\n\n" +
            "Rewards are paid manually. Champion roles are permanent, " +
            "coloured, and displayed separately."
          )
        ],
        components: []
      });
    }

    if (action === "boards") return chooseSeason(i, "board");
    if (action === "boardSeason") return showBoards(i, i.values[0]);
    if (action === "board") return showBoards(i, id);

    if (action === "hall") {
      return respond(
        i,
        `Permanent Hall of Fame: <#${config.channels.hall}>`
      );
    }

    if (action === "calendar") return calendar(i, Number(id));
    if (action === "slotPick") return slotPanel(i, i.values[0]);
    if (action === "slot") return slotPanel(i, id);

    if (action === "createForm") {
      const slot = await db(
        supabase.from("ks_slots").select("*").eq("id", id).single()
      );

      if (
        Date.now() >= new Date(slot.opens_at).getTime() ||
        slot.state === "cancelled"
      ) throw new Error("This date no longer accepts new questions.");

      const title = i.fields.getTextInputValue("title").trim();
      const prompt = i.fields.getTextInputValue("prompt").trim();

      let options: string[] | null = null;
      let correctIndex: number | null = null;
      let points = 10;

      if (slot.league === "knowledge") {
        points = Number(i.fields.getTextInputValue("points"));

        if (!Number.isInteger(points) || points < 1 || points > 10) {
          throw new Error("Points must be a whole number from 1 to 10.");
        }

        const lines = i.fields.getTextInputValue("options")
          .split("\n")
          .map((x: string) => x.trim())
          .filter(Boolean);

        if (
          lines.length < 2 ||
          lines.length > 5 ||
          lines.filter((x: string) => x.startsWith("*")).length !== 1
        ) {
          throw new Error(
            "Provide 2–5 options on separate lines, with exactly one " +
            "correct option prefixed by *."
          );
        }

        correctIndex = lines.findIndex((x: string) => x.startsWith("*"));
        const parsed: string[] = lines.map((x: string) =>
          x.startsWith("*") ? x.slice(1).trim() : x
        );

        if (parsed.some(x => !x || x.length > 140)) {
          throw new Error("Options must contain 1–140 characters.");
        }

        options = parsed;
      }

      const question = await db(
        supabase.from("ks_challenges").insert({
          slot_id: id,
          title,
          prompt,
          points,
          options,
          correct_index: correctIndex,
          author_id: i.user.id
        }).select("*").single()
      );

      // Adding content returns the set to draft for explicit review.
      await db(
        supabase.from("ks_slots").update({ state: "draft" }).eq("id", id)
      );

      await audit(i.user.id, "challenge.created", {
        challenge_id: question.id,
        slot_id: id
      });

      return slotPanel(i, id);
    }

    if (action === "editForm") {
      const c = await context(id);

      if (
        c.season_state !== "open" ||
        Date.now() >= new Date(c.closes_at).getTime()
      ) throw new Error("Closed challenge wording is locked in this version.");

      const title = i.fields.getTextInputValue("title").trim();
      const prompt = i.fields.getTextInputValue("prompt").trim();
      const reason = i.fields.getTextInputValue("reason").trim();

      await db(
        supabase.from("ks_challenges")
          .update({ title, prompt })
          .eq("id", id)
      );

      await audit(i.user.id, "challenge.wording_corrected", {
        challenge_id: id,
        old_title: c.title,
        old_prompt: c.prompt,
        new_title: title,
        new_prompt: prompt,
        reason
      });

      if (c.message_id) {
        const channel = await textChannel(config.channels[c.league]);
        const message = await channel.messages.fetch(c.message_id);

        const description = prompt + "\n\n" +
          (c.league === "knowledge"
            ? c.options.map((o: string, n: number) =>
              `**${String.fromCharCode(65 + n)}.** ${o}`
            ).join("\n") + "\n\n"
            : "") +
          `Closes: ${timestamp(c.closes_at)}\n\n` +
          `**Staff correction:** ${reason}`;

        await message.edit({
          embeds: [
            embed(title, description)
              .setFooter({ text: `KS • challenge-${id}` })
          ]
        });

        await channel.send({
          embeds: [
            embed(
              "Challenge Wording Corrected",
              `**${title}**\n${reason}\n\n` +
              "Deadline and scoring rules are unchanged."
            )
          ]
        });
      }

      return respond(i, "Correction saved and logged.");
    }

    if (action === "ready") {
      const slot = await db(
        supabase.from("ks_slots").select("*").eq("id", id).single()
      );

      if (Date.now() >= new Date(slot.opens_at).getTime()) {
        throw new Error("Use Approve Late Posting after the opening time.");
      }

      const questions = await db(
        supabase.from("ks_challenges")
          .select("id")
          .eq("slot_id", id)
          .neq("state", "cancelled")
      );

      if (!questions.length) throw new Error("Add at least one question.");

      await db(
        supabase.from("ks_slots").update({ state: "ready" }).eq("id", id)
      );

      await audit(i.user.id, "slot.ready", { slot_id: id });
      return slotPanel(i, id);
    }

    if (action === "cancelForm") {
      const slot = await db(
        supabase.from("ks_slots")
          .select("*,ks_seasons!inner(state)")
          .eq("id", id)
          .single()
      );

      if (slot.ks_seasons.state !== "open") {
        throw new Error("Finalized seasons cannot be cancelled.");
      }

      if (Date.now() >= new Date(slot.opens_at).getTime()) {
        throw new Error(
          "This v1 cancellation control is for future dates only. " +
          "Use answer disqualification for individual completed entries."
        );
      }

      const reason = i.fields.getTextInputValue("reason").trim();

      await db(
        supabase.from("ks_slots")
          .update({ state: "cancelled" })
          .eq("id", id)
      );

      await audit(i.user.id, "slot.cancelled", {
        slot_id: id,
        reason
      });

      return slotPanel(i, id);
    }

    if (action === "restore") {
      const slot = await db(
        supabase.from("ks_slots").select("*").eq("id", id).single()
      );

      if (Date.now() >= new Date(slot.opens_at).getTime()) {
        throw new Error("Only future dates can be restored.");
      }

      await db(
        supabase.from("ks_slots")
          .update({ state: "draft", late_approved: false })
          .eq("id", id)
      );

      await audit(i.user.id, "slot.restored", { slot_id: id });
      return slotPanel(i, id);
    }

    if (action === "late") {
      const slot = await db(
        supabase.from("ks_slots").select("*").eq("id", id).single()
      );

      const now = Date.now();

      if (
        now < new Date(slot.opens_at).getTime() ||
        now >= new Date(slot.closes_at).getTime()
      ) {
        throw new Error("Late posting is allowed only inside the original window.");
      }

      if (slot.state === "cancelled") {
        throw new Error("This slot was cancelled.");
      }

      await db(
        supabase.from("ks_slots")
          .update({ state: "ready", late_approved: true })
          .eq("id", id)
      );

      await audit(i.user.id, "slot.late_post_approved", { slot_id: id });

      return respond(
        i,
        "Late posting approved. The original closing time is unchanged."
      );
    }

    if (action === "challengePick") {
      return workspace(i, i.values[0]);
    }

    if (action === "work") return workspace(i, id);

    if (action === "choose") {
      const c = await context(id);
      const chosen = Number(i.values[0]);

      return respond(i, {
        embeds: [
          embed(
            "Confirm Final Trivia Answer",
            `**${c.options[chosen]}**\n\n` +
            "This answer cannot be edited after confirmation."
          )
        ],
        components: [
          row(
            button(
              `confirm:${id}:${chosen}`,
              "Confirm Final Answer",
              ButtonStyle.Danger
            ),
            button(`work:${id}`, "Choose Again")
          )
        ]
      });
    }

    if (action === "confirm") {
      const c = await context(id);
      await requireParticipant(i.user.id, c);

      await rpc("ks_answer_action", {
        p_actor: i.user.id,
        p_challenge: id,
        p_kind: "trivia",
        p_payload: { choice: Number(extra) }
      });

      return respond(
        i,
        "Your final answer is recorded. Correctness and points remain " +
        "hidden until this question closes."
      );
    }

    if (action === "sectionForm") {
      const c = await context(id);
      await requireParticipant(i.user.id, c);

      const a = await answerFor(id, i.user.id);
      const sections: string[] = [...(a?.draft ?? [])];
      const index = Number(extra);
      const text = i.fields.getTextInputValue("text");

      if (index > sections.length) {
        throw new Error("Draft changed. Reopen the workspace.");
      }

      if (!text.trim()) {
        sections.splice(index, 1);
      } else {
        sections[index] = text;
      }

      validateSections(sections);

      await rpc("ks_answer_action", {
        p_actor: i.user.id,
        p_challenge: id,
        p_kind: "save",
        p_payload: { sections }
      });

      return workspace(i, id);
    }

    if (action === "preview") return showAnswer(i, id, false);
    if (action === "submitted") return showAnswer(i, id, true);

    if (action === "submit") {
      const c = await context(id);
      await requireParticipant(i.user.id, c);

      await rpc("ks_answer_action", {
        p_actor: i.user.id,
        p_challenge: id,
        p_kind: "submit",
        p_payload: {}
      });

      return workspace(i, id);
    }

    if (action === "uploadRoom") return createUploadRoom(i, id);

    if (action === "images") {
      const a = await answerFor(id, i.user.id);
      if (!a) return respond(i, "No images saved.");

      const uploads = await db(
        supabase.from("ks_uploads")
          .select("*")
          .eq("answer_id", a.id)
          .eq("active", true)
      );

      if (!uploads.length) return respond(i, "No active draft images.");

      return respond(i, {
        embeds: [
          embed(
            "Manage Draft Images",
            "Select an image to remove from your draft. " +
            "Already submitted versions remain unchanged until you update."
          )
        ],
        components: [
          row(select(
            `removeImage:${id}`,
            "Remove a draft image",
            uploads.map((u: any, n: number) => ({
              label: `Image ${n + 1} • ${u.filename}`,
              value: u.id
            }))
          ))
        ]
      });
    }

    if (action === "removeImage") {
      const c = await context(id);
      await requireParticipant(i.user.id, c);

      if (Date.now() >= new Date(c.closes_at).getTime()) {
        throw new Error("Deadline has passed.");
      }

      const a = await answerFor(id, i.user.id);
      if (!a) throw new Error("Answer not found.");

      await db(
        supabase.from("ks_uploads")
          .update({ active: false })
          .eq("id", i.values[0])
          .eq("answer_id", a.id)
          .eq("user_id", i.user.id)
      );

      return workspace(i, id);
    }

    if (action === "reviews") return reviewQueue(i);
    if (action === "reviewPick") return reviewAnswer(i, i.values[0]);
    if (action === "review") return reviewAnswer(i, id);

    if (action === "scoreForm") {
      const score = Number(i.fields.getTextInputValue("score"));
      const dqText = i.fields.getTextInputValue("dq").trim().toLowerCase();
      const reason = i.fields.getTextInputValue("reason").trim();

      if (!Number.isInteger(score) || score < 0 || score > 10) {
        throw new Error("Use a whole-number score from 0 to 10.");
      }

      if (!["yes", "no"].includes(dqText)) {
        throw new Error("Disqualified must be yes or no.");
      }

      await rpc("ks_score_answer", {
        p_actor: i.user.id,
        p_answer: id,
        p_expected_version: Number(extra),
        p_score: score,
        p_disqualified: dqText === "yes",
        p_reason: reason
      });

      await audit(i.user.id, "public_score_notice.required", {
        answer_id: id,
        score,
        disqualified: dqText === "yes",
        reason
      });

      const a = await db(
        supabase.from("ks_answers")
          .select("*")
          .eq("id", id)
          .single()
      );

      const c = await context(a.challenge_id);

      if (a.published_version !== null && c.league !== "knowledge") {
        await stableSend(
          `score-change-${a.id}-${new Date(a.scored_at).getTime()}`,
          config.channels[`${c.league}Answers`],
          {
            embeds: [
              embed(
                `Score Update • ${await displayName(a.user_id)}`,
                `Challenge: **${c.title}**\n` +
                `Current score: **${score}/10**\n` +
                `Disqualified: **${dqText}**\n\n${reason}`
              )
            ]
          }
        );
      }

      return respond(i, "Score saved. Leaderboards will update automatically.");
    }

    if (action === "history") {
      const versions = await db(
        supabase.from("ks_answer_versions")
          .select("*")
          .eq("answer_id", id)
          .order("version")
      );

      return respond(i, {
        content: "Immutable submitted-version history.",
        files: [
          new AttachmentBuilder(
            Buffer.from(JSON.stringify(versions, null, 2), "utf8"),
            { name: "submission-history.json" }
          )
        ],
        embeds: [],
        components: []
      });
    }

    if (action === "seasons") return chooseSeason(i, "control");
    if (action === "controlSeason") return seasonControl(i, i.values[0]);
    if (action === "finalPreview") return finalPreview(i, id);

    if (action === "tieForm") {
      const league = i.fields.getTextInputValue("league").trim().toLowerCase();
      const userId = i.fields.getTextInputValue("user").trim();
      const value = Number(i.fields.getTextInputValue("value"));
      const reason = i.fields.getTextInputValue("reason").trim();

      if (!LEAGUES.includes(league as League)) {
        throw new Error("Invalid league.");
      }

      if (!/^\d{17,22}$/.test(userId) || !Number.isSafeInteger(value)) {
        throw new Error("Invalid user ID or tie-break value.");
      }

      await rpc("ks_set_tiebreak", {
        p_actor: i.user.id,
        p_season: id,
        p_league: league,
        p_user: userId,
        p_value: value,
        p_reason: reason
      });

      return respond(i, "Staff-decided tie-break result recorded.");
    }

    if (action === "finalize") {
      const data = await finalizationData(id);

      await rpc("ks_finalize", {
        p_actor: i.user.id,
        p_season: id,
        p_absent: data.absent,
        p_names: data.names
      });

      return respond(
        i,
        "Final results approved and locked. The worker will award " +
        "permanent roles, publish the Hall of Fame, and create announcements."
      );
    }

    if (action === "participants") {
      return respond(i, {
        embeds: [embed("Participants", "Select a member to manage eligibility.")],
        components: [
          row(
            new UserSelectMenuBuilder()
              .setCustomId("participantPick")
              .setPlaceholder("Choose member")
              .setMinValues(1)
              .setMaxValues(1)
          )
        ]
      });
    }

    if (action === "participantPick") {
      const userId = i.values[0];

      return respond(i, {
        embeds: [
          embed(
            `Participation • ${await displayName(userId)}`,
            "Blocking prevents new submissions and updates. " +
            "Existing scores are not automatically removed."
          )
        ],
        components: [
          row(
            button(`block:${userId}`, "Block Participation", ButtonStyle.Danger),
            button(`unblock:${userId}`, "Restore Participation", ButtonStyle.Success)
          )
        ]
      });
    }

    if (action === "block") {
      await db(
        supabase.from("ks_exclusions").upsert({
          guild_id: GUILD_ID,
          user_id: id,
          reason: "Staff participation restriction",
          actor_id: i.user.id
        })
      );

      await (await member(id)).roles.add(config.blocked_role_id);

      await audit(i.user.id, "participant.blocked", { user_id: id });
      return respond(i, "Participation blocked. Existing scores retained.");
    }

    if (action === "unblock") {
      await (await member(id)).roles.remove(config.blocked_role_id);

      await db(
        supabase.from("ks_exclusions")
          .delete()
          .eq("guild_id", GUILD_ID)
          .eq("user_id", id)
      );

      await audit(i.user.id, "participant.unblocked", { user_id: id });
      return respond(i, "Participation restored.");
    }

    if (action === "rewards") return chooseSeason(i, "reward");
    if (action === "rewardSeason") return rewardsPanel(i, i.values[0]);

    if (action === "rewardPick" || action === "rewardPickMore") {
      const award = await db(
        supabase.from("ks_awards")
          .select("*")
          .eq("id", i.values[0])
          .single()
      );

      return respond(i, {
        embeds: [
          embed(
            "Manual Reward",
            `${award.display_name}\n` +
            `${award.league} #${award.placement}\n` +
            `**${award.coins} ${config.currency_name}**\n` +
            `Status: **${award.reward_status}**\n\n` +
            "These controls only record status. They do not transfer coins."
          )
        ],
        components: [
          row(
            button(`rewardSet:${award.id}:approved`, "Approve"),
            button(`rewardSet:${award.id}:paid`, "Mark Manually Paid", ButtonStyle.Success)
          )
        ]
      });
    }

    if (action === "rewardSet") {
      if (!["approved", "paid"].includes(extra)) {
        throw new Error("Invalid reward status.");
      }

      const award = await db(
        supabase.from("ks_awards")
          .select("*")
          .eq("id", id)
          .single()
      );

      if (award.reward_status === "paid") {
        throw new Error("This reward is already marked paid.");
      }

      if (extra === "paid" && award.reward_status !== "approved") {
        throw new Error("Approve the reward before marking it paid.");
      }

      await db(
        supabase.from("ks_awards")
          .update({
            reward_status: extra,
            reward_actor_id: i.user.id,
            reward_updated_at: new Date().toISOString()
          })
          .eq("id", id)
          .eq("reward_status", award.reward_status)
      );

      await audit(i.user.id, `reward.${extra}`, {
        award_id: id,
        coins: award.coins,
        user_id: award.user_id
      });

      return respond(i, `Reward status recorded: ${extra}. No coins were transferred.`);
    }

    if (action === "currencyForm") {
      const currency = i.fields.getTextInputValue("currency").trim();
      if (!currency) throw new Error("Currency name is required.");

      await db(
        supabase.from("ks_config")
          .update({ currency_name: currency })
          .eq("guild_id", GUILD_ID)
      );

      await loadConfig();
      await audit(i.user.id, "settings.currency_name", { currency });
      return respond(i, "Currency display name updated.");
    }

    if (action === "export") return exportSeason(i, id);

    throw new Error("Unknown or outdated panel action. Reopen the main panel.");
  } catch (error: any) {
    console.error("[interaction]", error);

    const message = String(error.message ?? "Unexpected error");

    await respond(
      i,
      `⚠️ ${message.includes("duplicate key")
        ? "This action was already recorded. Trivia allows only one final answer."
        : message}`
    ).catch(console.error);
  }
});

client.on(Events.Error, error => {
  console.error("[discord]", error);
});

client.once(Events.ClientReady, async () => {
  console.log(`Logged in as ${client.user!.tag}`);
  console.log(`Supabase host: ${new URL(SUPABASE_URL).host}`);

  const commands = [
    new SlashCommandBuilder()
      .setName("setup")
      .setDescription("First-time Knowledge Season setup")
      .setDefaultMemberPermissions(PermissionFlagsBits.Administrator),
    new SlashCommandBuilder()
      .setName("panels")
      .setDescription("Recover Knowledge Season root panels")
      .setDefaultMemberPermissions(PermissionFlagsBits.Administrator)
  ];

  const rest = new REST({ version: "10" }).setToken(TOKEN);

  try {
    await rest.put(
      Routes.applicationGuildCommands(APP_ID, GUILD_ID),
      { body: commands.map(c => c.toJSON()) }
    );
  } catch (error) {
    console.error("[ready] Failed to register slash commands:", error);
  }

  try {
    await loadConfig();

    if (config) await ensureSeasons();

    await tick();
  } catch (error) {
    console.error("[ready] Database startup failed:", error);
    console.error(
      "The bot will stay online so /setup can run. " +
      "Confirm SUPABASE_URL is the Project URL " +
      "(https://YOUR_PROJECT.supabase.co) and that sql/001_initial.sql " +
      "has been applied."
    );
  }

  setInterval(
    () => void tick(),
    Number(process.env.WORKER_INTERVAL_MS ?? 30_000)
  ).unref();
});

async function shutdown(signal: string) {
  if (shuttingDown) return;
  shuttingDown = true;

  console.log(`Received ${signal}; stopping.`);

  const deadline = Date.now() + 15_000;

  while (workerRunning && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 250));
  }

  client.destroy();
  process.exit(0);
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));

process.on("unhandledRejection", error => {
  console.error("[unhandledRejection]", error);
});

await client.login(TOKEN);
