import { describe, expect, it } from "vitest";
import { GROUPS, NAV, navFor } from "./components/nav";

describe("navigation config", () => {
  it("has unique paths and labels", () => {
    expect(new Set(NAV.map((n) => n.to)).size).toBe(NAV.length);
    expect(new Set(NAV.map((n) => n.label)).size).toBe(NAV.length);
  });
  it("puts Settings, Activity and Users in the always-visible system group", () => {
    expect(NAV.filter((n) => n.group === "system").map((n) => n.to).sort()).toEqual(["/activity", "/settings", "/users"]);
  });
  it("shows user management to admins only", () => {
    expect(navFor(true).map((n) => n.to)).toContain("/users");
    expect(navFor(false).map((n) => n.to)).not.toContain("/users");
    expect(navFor(false).length).toBe(NAV.length - 1);
  });
  it("every non-system group is rendered", () => {
    const rendered = new Set(GROUPS.map((g) => g.key));
    for (const n of NAV) if (n.group !== "system") expect(rendered.has(n.group)).toBe(true);
  });
});

import { trimRows } from "./util";

describe("trimRows", () => {
  const rows = Array.from({ length: 10 }, (_, i) => i);
  it("trims from the top while following the tail", () => expect(trimRows(rows, true, 4, 8)).toEqual([6, 7, 8, 9]));
  it("keeps history while the user reads it, up to the hard cap", () => {
    expect(trimRows(rows, false, 4, 20)).toEqual(rows);
    expect(trimRows(rows, false, 4, 8)).toEqual([2, 3, 4, 5, 6, 7, 8, 9]);
  });
  it("leaves short lists alone", () => expect(trimRows([1, 2], true, 4, 8)).toEqual([1, 2]));
});

import { bufferPaused } from "./util";

describe("bufferPaused", () => {
  it("accumulates under the cap", () => expect(bufferPaused([1, 2], [3], 5)).toEqual({ buf: [1, 2, 3], overflowed: false }));
  it("keeps the newest events and reports the overflow", () => expect(bufferPaused([1, 2, 3], [4, 5], 4)).toEqual({ buf: [2, 3, 4, 5], overflowed: true }));
});

describe("tool separation", () => {
  it("keeps the converter, comparison and coverage tools in their own groups", () => {
    const of = (g: string) => NAV.filter((n) => n.group === g).map((n) => n.to);
    expect(of("convert")).toEqual(["/convert"]);
    expect(of("compare")).toEqual(["/compare"]);
    expect(of("coverage")).toEqual(expect.arrayContaining(["/workflow", "/specs", "/catalog", "/cmddb", "/coverage", "/reports"]));
    expect(of("coverage")).not.toContain("/compare");
  });
  it("titles every tool group", () => expect(GROUPS.filter((g) => g.key !== "home").every((g) => g.title)).toBe(true));
});
