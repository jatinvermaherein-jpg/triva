import test from "node:test";
import assert from "node:assert/strict";

import {
  DAY,
  compareStanding,
  makeSlots,
  nextMondayIST,
  normalizeSupabaseUrl,
  validateSections,
  wordCount
} from "../src/domain.js";

test("next Monday is calculated in IST", () => {
  const result = nextMondayIST(
    new Date("2026-07-05T12:00:00.000Z")
  );

  assert.equal(
    result.toISOString(),
    "2026-07-05T18:30:00.000Z"
  );
});

test("setup on Monday selects the following Monday", () => {
  const result = nextMondayIST(
    new Date("2026-07-06T10:00:00.000Z")
  );

  assert.equal(
    result.toISOString(),
    "2026-07-12T18:30:00.000Z"
  );
});

test("four weeks contain 24 scheduled dates", () => {
  const start = new Date("2026-07-05T18:30:00.000Z");
  const slots = makeSlots(start);

  assert.equal(slots.length, 24);

  for (const slot of slots) {
    assert.equal(
      new Date(slot.closes_at).getTime() -
      new Date(slot.opens_at).getTime(),
      DAY
    );
  }
});

test("first question opens Monday at 4 PM IST", () => {
  const slots = makeSlots(
    new Date("2026-07-05T18:30:00.000Z")
  );

  assert.equal(
    slots[0].opens_at,
    "2026-07-06T10:30:00.000Z"
  );
});

test("no Sunday openings", () => {
  const slots = makeSlots(
    new Date("2026-07-05T18:30:00.000Z")
  );

  for (const slot of slots) {
    const local = new Date(
      new Date(slot.opens_at).getTime() + 19_800_000
    );

    assert.notEqual(local.getUTCDay(), 0);
  }
});

test("word count uses whitespace boundaries", () => {
  assert.equal(wordCount(["one two", "three\nfour"]), 4);
  assert.equal(wordCount(["   "]), 0);
});

test("more than 2,000 words is rejected", () => {
  assert.throws(() => validateSections([
    Array(2001).fill("word").join(" ")
  ]));
});

test("Knowledge tie uses correct count, not histogram", () => {
  const a = {
    league: "knowledge",
    points: 20,
    correct_count: 4,
    histogram: [0],
    tiebreak: 0
  };

  const b = {
    league: "knowledge",
    points: 20,
    correct_count: 3,
    histogram: [10],
    tiebreak: 0
  };

  assert.ok(compareStanding(a, b) < 0);
});

test("Strategy tie uses highest-score histogram", () => {
  const a = {
    league: "strategy",
    points: 20,
    histogram: [2, 0, 0],
    tiebreak: 0
  };

  const b = {
    league: "strategy",
    points: 20,
    histogram: [1, 1, 0],
    tiebreak: 0
  };

  assert.ok(compareStanding(a, b) < 0);
});

test("exact ties remain ties", () => {
  const row = {
    league: "hangar",
    points: 10,
    histogram: [1, 0, 0],
    tiebreak: 0
  };

  assert.equal(compareStanding(row, row), 0);
});

test("SUPABASE_URL is the project origin, not the REST path", () => {
  assert.equal(
    normalizeSupabaseUrl("https://abc.supabase.co"),
    "https://abc.supabase.co"
  );
  assert.equal(
    normalizeSupabaseUrl("https://abc.supabase.co/"),
    "https://abc.supabase.co"
  );
  assert.equal(
    normalizeSupabaseUrl("https://abc.supabase.co/rest/v1"),
    "https://abc.supabase.co"
  );
  assert.equal(
    normalizeSupabaseUrl("https://abc.supabase.co/rest/v1/"),
    "https://abc.supabase.co"
  );
  assert.equal(
    normalizeSupabaseUrl(' "https://abc.supabase.co/rest/v1/" '),
    "https://abc.supabase.co"
  );
  assert.throws(
    () => normalizeSupabaseUrl("postgresql://postgres@localhost/postgres"),
    /https Project URL/
  );
});
