import { describe, expect, it } from "vitest";
import { blockText, countHits, parseInline, parseMarkdown, sourceBlocks } from "./components/Markdown";

describe("parseMarkdown", () => {
  const md = [
    "---", 'title: "Widget Spec"', "source: \"Widget.pdf\"", "pages: 3", "---", "",
    "# Widget Spec", "", "## 5.1 Admin \\*Command\\* Overview", "",
    "The host **shall** set *these* fields; see `SQID` and SQ_ID.", "continued line.", "",
    "| Bits | Name | Notes |", "|:-----|-----:|:----:|", "| 31:16 | SQID | a<br>b |", "| 7:0 | OPC \\| x | &amp; |", "",
    "- one", "  - nested", "1. first", "", "> quoted", "", "```", "code *here*", "```", "<!-- image -->", "***",
  ].join("\n");
  const blocks = parseMarkdown(md);

  it("reads every block kind the converter writes", () => {
    expect(blocks.map((b) => b.t)).toEqual(["front", "h", "h", "p", "table", "list", "quote", "code", "image", "hr"]);
    expect(blocks[0]).toEqual({ t: "front", entries: [["title", "Widget Spec"], ["source", "Widget.pdf"], ["pages", "3"]] });
    expect(blockText(blocks[2])).toBe("5.1 Admin *Command* Overview");     // escapes stay literal
    expect(blockText(blocks[3])).toBe("The host  shall  set  these  fields; see  SQID  and SQ_ID. continued line.");
  });

  it("keeps table columns, alignment, escaped pipes and <br> in cells", () => {
    const t = blocks[4];
    if (t.t !== "table") throw new Error("not a table");
    expect(t.align).toEqual(["left", "right", "center"]);
    expect(t.rows).toHaveLength(2);
    expect(t.rows.every((r) => r.length === 3)).toBe(true);
    expect(t.rows[0][2]).toEqual([{ t: "text", v: "a" }, { t: "br" }, { t: "text", v: "b" }]);
    expect(t.rows[1][1]).toEqual([{ t: "text", v: "OPC | x" }]);
    expect(t.rows[1][2]).toEqual([{ t: "text", v: "&" }]);
  });

  it("nests list items by indent", () => {
    const l = blocks[5];
    if (l.t !== "list") throw new Error("not a list");
    expect(l.items.map((x) => [x.depth, x.marker])).toEqual([[0, "•"], [1, "•"], [0, "1."]]);
  });
});

describe("parseInline", () => {
  it("leaves unbalanced markers and mid-word underscores alone", () => {
    expect(parseInline("2 * 3 = 6")).toEqual([{ t: "text", v: "2 * 3 = 6" }]);
    expect(parseInline("CDW10_NSID and __x")).toEqual([{ t: "text", v: "CDW10_NSID and __x" }]);
    expect(parseInline("***both***")).toEqual([{ t: "b", c: [{ t: "i", c: [{ t: "text", v: "both" }] }] }]);
  });
  it("renders the built-in converter's <sup>, <sub> and <u>; any other tag is text", () => {
    expect(parseInline("NVM Express<sup>®</sup> x<sub>2</sub>")).toEqual([
      { t: "text", v: "NVM Express" }, { t: "sp", c: [{ t: "text", v: "®" }] }, { t: "text", v: " x" }, { t: "sb", c: [{ t: "text", v: "2" }] }]);
    expect(parseInline("<script>alert(1)</script>")).toEqual([{ t: "text", v: "<script>alert(1)</script>" }]);
  });
  it("never produces a script link", () => {
    expect(parseInline("[x](javascript:alert(1))")[0]).toMatchObject({ t: "a", href: "javascript:alert(1" });   // rendered without an href
  });
});

describe("search", () => {
  it("counts matches per text run, case-insensitively, in both views", () => {
    const md = "# Queue\n\nThe submission queue and the completion QUEUE.\n\n| a | b |\n|---|---|\n| queue | x |\n";
    expect(countHits(parseMarkdown(md), "queue")).toBe(4);
    expect(countHits(sourceBlocks(md, 2), "QUEUE")).toBe(4);
    expect(countHits(parseMarkdown(md), "")).toBe(0);
  });
});
