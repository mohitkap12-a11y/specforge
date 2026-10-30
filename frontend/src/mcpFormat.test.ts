import { describe, expect, it } from "vitest";
import { approxTokens, callText, delta, fmtChars, fmtDuration, fmtMs, niceScale } from "./mcpFormat";
import { mcpQuery } from "./api";
import { issueReport } from "./pages/McpAdmin";
import type { McpIssueDetail } from "./types";

describe("MCP metrics formatting", () => {
  it("formats latency, sizes and durations", () => {
    expect(fmtMs(212.4)).toBe("212 ms");
    expect(fmtMs(1420)).toBe("1.42 s");
    expect(fmtMs(21400)).toBe("21 s");
    expect(fmtMs(null)).toBe("–");
    expect(fmtChars(2900)).toBe("2.9 k");
    expect(fmtChars(53_100_000)).toBe("53.1 M");
    expect(approxTokens(4000)).toBe(1000);
    expect(fmtDuration(250)).toBe("4 m 10 s");
  });
  it("compares with the previous period, knowing when lower is better", () => {
    expect(delta(123, 100)).toEqual({ text: "▲ 23%", tone: "up" });
    expect(delta(1.3, 0.9, { points: true, lowerIsBetter: true })).toEqual({ text: "▲ 0.4 pt", tone: "down" });
    expect(delta(1240, 1420, { lowerIsBetter: true })?.tone).toBe("up");
    expect(delta(5, 0)?.text).toBe("▲ new");
    expect(delta(9673, 1)?.text).toBe("▲ >999%");
    expect(delta(null, 3)).toBeNull();
  });
  it("picks round axis maxima", () => {
    expect(niceScale(1060)).toEqual({ max: 1250, step: 250 });
    expect(niceScale(7)).toEqual({ max: 8, step: 2 });
    expect(niceScale(0)).toEqual({ max: 4, step: 1 });
  });
  it("shows a call the way an agent made it", () => {
    expect(callText("get_section", { section: "99.9", spec: "base", depth: 1 })).toBe('get_section(section="99.9", spec="base", depth=1)');
  });
  it("leaves empty filters out of the query string", () => {
    expect(mcpQuery({ range: "7d", tool: "get_table", q: "", slow: true }, { tz: 60 })).toBe("?range=7d&tool=get_table&slow=1&tz=60");
  });
  it("writes a pasteable report for one group of misses", () => {
    const d: McpIssueDetail = {
      fingerprint: "abc", tool: "get_section", outcome: "miss", reason: "section not found", count: 3, first: 0, last: 60, status: "investigating",
      note: "renumbered in 2.4", updated: null, updated_by: null, per_day: [], variants: [{ args: { section: "99.9" }, count: 3 }],
      errors: [{ error: "nvme-base 2.4 has no section 99.9", count: 3 }], agents: [{ agent: "claude-code 2.1", count: 3 }],
      clients: [{ ip: "10.0.0.1", count: 3 }], p50_ms: 12, recovered: 1, followed: 3, next: [],
    };
    const md = issueReport(d);
    expect(md).toContain("### MCP miss: get_section — section not found");
    expect(md).toContain('`get_section(section="99.9")` × 3');
    expect(md).toContain("status: investigating — renumbered in 2.4");
    expect(md).toContain("nvme-base 2.4 has no section 99.9");
  });
});
