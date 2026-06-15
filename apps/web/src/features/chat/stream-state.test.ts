import { describe, expect, it } from "vitest";
import type { RunEvent } from "@evidencedesk/api-client/events";
import { initialStream, reduceStream } from "./stream-state";
const run = "00000000-0000-4000-8000-000000000001";
const envelope = {
  spec_version: "1" as const,
  run_id: run,
  occurred_at: "2026-10-11T00:00:00Z",
};
const accepted: RunEvent = {
  ...envelope,
  seq: 1,
  type: "run.accepted",
  data: { status: "queued" },
};
const draft: RunEvent = {
  ...envelope,
  seq: 2,
  type: "answer.delta",
  data: { text: "<script>private</script>", draft: true, sentence_index: 0 },
};
describe("run presentation reducer", () => {
  it("deduplicates replay and rejects missing events", () => {
    const state = reduceStream(initialStream(run), accepted);
    expect(reduceStream(state, accepted)).toBe(state);
    expect(() => reduceStream(state, { ...draft, seq: 3 })).toThrow();
    expect(reduceStream(state, { ...draft, run_id: "other" })).toBe(state);
  });
  it.each(["run.failed", "run.cancelled", "run.refused"] as const)(
    "discards every draft on %s",
    (type) => {
      const state = reduceStream(
        reduceStream(initialStream(run), accepted),
        draft,
      );
      const data =
        type === "run.failed"
          ? { code: "interrupted", retryable: false, discard_draft: true }
          : type === "run.cancelled"
            ? { reason: "user" }
            : { reason: "provider_refusal" };
      const failed = reduceStream(state, {
        ...envelope,
        seq: 3,
        type,
        data,
      } as RunEvent);
      expect(failed.text).toBe("");
      expect(reduceStream(failed, { ...draft, seq: 4 })).toBe(failed);
    },
  );
  it("replaces draft with the validated terminal answer", () => {
    const state = reduceStream(
      reduceStream(initialStream(run), accepted),
      draft,
    );
    const answer = {
      status: "abstained" as const,
      sentences: [],
      limitations: ["Insufficient evidence."],
    };
    const final = reduceStream(state, {
      ...envelope,
      seq: 3,
      type: "answer.completed",
      data: { message_id: run, answer, citations: [] },
    });
    expect(final.text).toBe("");
    expect(final.answer).toEqual(answer);
    expect(final.state).toBe("completed");
  });
});
