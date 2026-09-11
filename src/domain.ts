export type League = "knowledge" | "strategy" | "hangar";

export const LEAGUES: League[] = [
  "knowledge",
  "strategy",
  "hangar"
];

export const LABELS: Record<League, string> = {
  knowledge: "🧠 Trivia Challenge",
  strategy: "⚔️ Strategy Challenge",
  hangar: "🔧 Hangar Review"
};

export const TITLES: Record<League, string> = {
  knowledge: "🧠 Mech Arena Trivia Champion",
  strategy: "⚔️ Strategy Master",
  hangar: "🔧 Best Advisor"
};

export const COLORS: Record<League, number> = {
  knowledge: 0x3498db,
  strategy: 0xe74c3c,
  hangar: 0x2ecc71
};

export const PAYOUTS = [
  3000, 2000, 1000, 800, 700,
  600, 500, 400, 300, 200
];

export const DAY = 86_400_000;
export const IST_OFFSET = 19_800_000;

export function nextMondayIST(now = new Date()): Date {
  const local = new Date(now.getTime() + IST_OFFSET);
  const midnight = Date.UTC(
    local.getUTCFullYear(),
    local.getUTCMonth(),
    local.getUTCDate()
  );

  const day = local.getUTCDay();
  const advance = ((8 - day) % 7) || 7;

  return new Date(midnight + advance * DAY - IST_OFFSET);
}

export function makeSlots(start: Date) {
  const schedule: League[] = [
    "knowledge",
    "strategy",
    "hangar",
    "knowledge",
    "strategy",
    "hangar"
  ];

  const slots: {
    league: League;
    opens_at: string;
    closes_at: string;
  }[] = [];

  for (let day = 0; day < 28; day++) {
    const weekday = day % 7;
    if (weekday === 6) continue;

    const opens = start.getTime() + day * DAY + 16 * 3_600_000;

    slots.push({
      league: schedule[weekday],
      opens_at: new Date(opens).toISOString(),
      closes_at: new Date(opens + DAY).toISOString()
    });
  }

  return slots;
}

export function wordCount(sections: string[]): number {
  const text = sections.join(" ").trim();
  return text ? text.split(/\s+/u).length : 0;
}

export function validateSections(sections: string[]): void {
  if (sections.length > 10) {
    throw new Error("Maximum 10 answer sections.");
  }

  if (sections.some(s => s.length > 3900)) {
    throw new Error("Each section may contain at most 3,900 characters.");
  }

  if (
    wordCount(sections) > 2000 ||
    sections.join("").length > 20000
  ) {
    throw new Error("Maximum 2,000 words and 20,000 characters.");
  }
}

export function compareStanding(a: any, b: any): number {
  if (a.points !== b.points) return b.points - a.points;

  const av: number[] = a.league === "knowledge"
    ? [a.correct_count]
    : a.histogram;

  const bv: number[] = b.league === "knowledge"
    ? [b.correct_count]
    : b.histogram;

  for (let n = 0; n < Math.max(av.length, bv.length); n++) {
    const diff = (bv[n] ?? 0) - (av[n] ?? 0);
    if (diff) return diff;
  }

  return (b.tiebreak ?? 0) - (a.tiebreak ?? 0);
}

export function chunks(text: string, size = 1750): string[] {
  const output: string[] = [];
  for (let i = 0; i < text.length; i += size) {
    output.push(text.slice(i, i + size));
  }
  return output.length ? output : ["—"];
}

/**
 * People often paste the REST endpoint, a trailing slash, or a postgres URI
 * into SUPABASE_URL. PostgREST then sees an extra path segment and returns
 * PGRST125 "Invalid path specified in request URL".
 */
export function normalizeSupabaseUrl(raw: string): string {
  const value = raw.trim().replace(/^['"]+|['"]+$/g, "");

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(
      "SUPABASE_URL is not a valid URL. Use the Project URL from " +
      "Supabase Settings → API, e.g. https://YOUR_PROJECT.supabase.co"
    );
  }

  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error(
      "SUPABASE_URL must be the https Project URL from Supabase " +
      "Settings → API, not a postgres:// connection string."
    );
  }

  url.pathname = url.pathname
    .replace(/\/+$/g, "")
    .replace(/\/(rest|auth|storage|functions)\/v1$/i, "");
  url.search = "";
  url.hash = "";

  return `${url.origin}${url.pathname}`.replace(/\/+$/g, "");
}

export function imageMime(buffer: Buffer): string | null {
  if (
    buffer.length >= 8 &&
    buffer.subarray(0, 8).equals(
      Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
    )
  ) return "image/png";

  if (
    buffer.length >= 3 &&
    buffer[0] === 0xff &&
    buffer[1] === 0xd8 &&
    buffer[2] === 0xff
  ) return "image/jpeg";

  if (
    buffer.length >= 12 &&
    buffer.toString("ascii", 0, 4) === "RIFF" &&
    buffer.toString("ascii", 8, 12) === "WEBP"
  ) return "image/webp";

  if (
    buffer.length >= 6 &&
    ["GIF87a", "GIF89a"].includes(buffer.toString("ascii", 0, 6))
  ) return "image/gif";

  return null;
}
