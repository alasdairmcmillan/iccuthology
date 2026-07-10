import { describe, expect, it } from "vitest";
import { parseSeedfile, SEEDFILE_USER_RE } from "../src/seedfile";

// Mirrors src/phishpred/personal.py parse_seedfile and its two-digit-year
// convention (00-69 -> 2000s, 70-99 -> 1900s).
describe("parseSeedfile", () => {
  it("parses M/D/YY and M/D/YYYY lines, zero-padded, sorted, deduped", () => {
    const text = [
      "7/10/26",
      "12/31/1995",
      "8/6/2024",
      "7/10/26", // duplicate
      "not a date line",
    ].join("\n");
    expect(parseSeedfile(text)).toEqual(["1995-12-31", "2024-08-06", "2026-07-10"]);
  });

  it("maps two-digit years across the 1970 pivot", () => {
    expect(parseSeedfile("11/22/97")).toEqual(["1997-11-22"]);
    expect(parseSeedfile("1/1/69")).toEqual(["2069-01-01"]);
    expect(parseSeedfile("1/1/70")).toEqual(["1970-01-01"]);
  });

  it("returns empty for text with no dates", () => {
    expect(parseSeedfile("hello world")).toEqual([]);
  });
});

describe("SEEDFILE_USER_RE", () => {
  it("accepts plain usernames and rejects path/query characters", () => {
    expect(SEEDFILE_USER_RE.test("steeze")).toBe(true);
    expect(SEEDFILE_USER_RE.test("a.b-c_9")).toBe(true);
    expect(SEEDFILE_USER_RE.test("a/b")).toBe(false);
    expect(SEEDFILE_USER_RE.test("a?b=c")).toBe(false);
    expect(SEEDFILE_USER_RE.test("")).toBe(false);
  });
});
