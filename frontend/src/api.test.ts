import { describe, expect, it } from "vitest";
import { errorText, qs } from "./api";

describe("errorText", () => {
  it("passes strings through", () => expect(errorText("nope")).toBe("nope"));
  it("flattens FastAPI validation arrays", () => expect(errorText([{ msg: "a" }, { msg: "b" }])).toBe("a; b"));
  it("joins predicate errors with the message", () => expect(errorText({ message: "Only valid predicates", errors: ["x", "y"] })).toBe("Only valid predicates; x; y"));
  it("has a fallback", () => expect(errorText(undefined)).toBe("request failed"));
});

describe("qs", () => {
  it("drops empty values and encodes the rest", () => expect(qs({ a: "x y", b: "", c: undefined, d: 0 })).toBe("?a=x+y&d=0"));
  it("is empty for no params", () => expect(qs({})).toBe(""));
});

import { parseOpcode, setIn } from "./util";

describe("parseOpcode", () => {
  it("accepts decimal, 0x and h forms", () => { expect(parseOpcode("6")).toBe(6); expect(parseOpcode("0x06")).toBe(6); expect(parseOpcode("0Ah")).toBe(10); });
  it("treats empty as no filter", () => expect(parseOpcode("  ")).toBeUndefined());
  it("rejects junk and out-of-range", () => { expect(parseOpcode("zz")).toBeNull(); expect(parseOpcode("256")).toBeNull(); expect(parseOpcode("-1")).toBeNull(); });
});

describe("setIn", () => {
  it("sets nested values immutably", () => {
    const a = { x: { y: 1 }, z: 2 };
    const b = setIn(a, ["x", "w"], 5);
    expect(b).toEqual({ x: { y: 1, w: 5 }, z: 2 });
    expect(a).toEqual({ x: { y: 1 }, z: 2 });
  });
  it("creates missing branches", () => expect(setIn({}, ["a", "b"], 1)).toEqual({ a: { b: 1 } }));
});

import { conversionSummary } from "./util";

describe("conversionSummary", () => {
  it("pluralises and drops zero counts", () => expect(conversionSummary({ words: 1204, headings: 1, tables: 3, list_items: 0, code_blocks: 0, pages: 2 })).toBe("2 pages · 1,204 words · 1 heading · 3 tables"));
});
