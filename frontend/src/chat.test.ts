import { describe, expect, it } from "vitest";
import { splitFences } from "./components/chat/code";

describe("splitFences", () => {
  it("splits prose and fenced code with the language", () => {
    const s = splitFences("Intro\n\n```json\n{\"a\": 1}\n```\nAfter");
    expect(s).toEqual([{ kind: "text", text: "Intro\n" }, { kind: "code", lang: "json", code: "{\"a\": 1}", open: false }, { kind: "text", text: "After" }]);
  });
  it("treats a fence still being streamed as an open code block", () => {
    const s = splitFences("Here:\n```python\ndef f():\n    return 1");
    expect(s[1]).toEqual({ kind: "code", lang: "python", code: "def f():\n    return 1", open: true });
  });
  it("keeps a shorter fence inside a longer one", () => {
    const s = splitFences("````md\n```c\nint x;\n```\n````");
    expect(s).toHaveLength(1);
    expect(s[0]).toMatchObject({ kind: "code", lang: "md", code: "```c\nint x;\n```", open: false });
  });
  it("leaves plain text alone", () => expect(splitFences("a `b` c")).toEqual([{ kind: "text", text: "a `b` c" }]));
});
