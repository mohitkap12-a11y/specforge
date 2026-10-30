import { describe, expect, it } from "vitest";
import { queueText } from "./components/jobs";

describe("queueText", () => {
  it("says where a queued job stands and what runs before it", () => {
    expect(queueText({ state: "queued", ahead: 2, ahead_of: { label: "Convert Base.pdf to Markdown", user: "ann" } }))
      .toBe("2 ahead of it in the queue, behind Convert Base.pdf to Markdown (ann)");
    expect(queueText({ state: "queued", ahead: 1, ahead_of: { label: "Parse nvme-base 2.4", user: null } })).toBe("1 ahead of it in the queue, behind Parse nvme-base 2.4");
    expect(queueText({ state: "queued", ahead: 0, ahead_of: null })).toBe("Next in line");
  });
  it("is empty once the job has started", () => {
    expect(queueText({ state: "running", ahead: 0, ahead_of: null })).toBe("");
  });
});
