import { describe, expect, it } from "vitest";
import { GROUPS, NAV } from "./components/nav";

describe("navigation config", () => {
  it("has unique paths and labels", () => {
    expect(new Set(NAV.map((n) => n.to)).size).toBe(NAV.length);
    expect(new Set(NAV.map((n) => n.label)).size).toBe(NAV.length);
  });
  it("puts Settings and Activity in the always-visible system group", () => {
    expect(NAV.filter((n) => n.group === "system").map((n) => n.to).sort()).toEqual(["/activity", "/settings"]);
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
