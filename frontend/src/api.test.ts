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
