import { describe, expect, it } from "vitest";
import { RunStreamDecoder, StreamGap, StreamProtocolError } from "./sse";
const run = "00000000-0000-4000-8000-000000000001";
const timestamp = "2026-10-11T00:00:00Z";
const accepted = {
  spec_version: "1",
  run_id: run,
  seq: 1,
  type: "run.accepted",
  occurred_at: timestamp,
  data: { status: "queued" },
};
const delta = {
  spec_version: "1",
  run_id: run,
  seq: 2,
  type: "answer.delta",
  occurred_at: timestamp,
  data: { text: "Café नेपाल 😀", draft: true, sentence_index: 0 },
};
const frame = (event: typeof accepted | typeof delta, newline = "\n") =>
  `id: ${event.seq}${newline}event: ${event.type}${newline}data: ${JSON.stringify(event)}${newline}${newline}`;
const encode = (text: string) => new TextEncoder().encode(text);

describe("durable run SSE decoder", () => {
  it.each(["\n", "\r\n"])(
    "decodes every byte split with %j line endings",
    (newline) => {
      const bytes = encode(
        ": ping" +
          newline +
          newline +
          frame(accepted, newline) +
          frame(delta, newline),
      );
      for (let boundary = 0; boundary <= bytes.length; boundary++) {
        const decoder = new RunStreamDecoder(run);
        const result = [
          ...decoder.push(bytes.slice(0, boundary)),
          ...decoder.push(bytes.slice(boundary)),
        ];
        decoder.finish();
        expect(result).toEqual([accepted, delta]);
      }
    },
  );
  it("decodes a stream delivered one byte at a time", () => {
    const decoder = new RunStreamDecoder(run);
    const result = [...encode(frame(accepted) + frame(delta))].flatMap((byte) =>
      decoder.push(new Uint8Array([byte])),
    );
    expect(result).toEqual([accepted, delta]);
  });
  it("deduplicates committed IDs and reconnects without another POST", () => {
    const decoder = new RunStreamDecoder(run, 1);
    expect(
      decoder.push(encode(frame(accepted) + frame(delta) + frame(delta))),
    ).toEqual([delta]);
    expect(decoder.lastSequence).toBe(2);
  });
  it("rejects gaps without advancing the resume cursor", () => {
    const decoder = new RunStreamDecoder(run);
    expect(() => decoder.push(encode(frame(delta)))).toThrow(StreamGap);
    expect(decoder.lastSequence).toBe(0);
  });
  it("rolls back an undelivered batch when a later frame has a gap", () => {
    const decoder = new RunStreamDecoder(run);
    try {
      decoder.push(encode(frame(accepted) + frame({ ...delta, seq: 3 })));
    } catch (error) {
      expect(error).toBeInstanceOf(StreamGap);
      expect((error as StreamGap).after).toBe(0);
    }
    expect(decoder.lastSequence).toBe(0);
    expect(
      new RunStreamDecoder(run, decoder.lastSequence).push(
        encode(frame(accepted) + frame(delta)),
      ),
    ).toEqual([accepted, delta]);
  });
  it("does not commit a truncated frame", () => {
    const decoder = new RunStreamDecoder(run);
    expect(
      decoder.push(encode(frame(accepted) + frame(delta).slice(0, -2))),
    ).toEqual([accepted]);
    decoder.finish();
    expect(decoder.lastSequence).toBe(1);
  });
  it("rejects a foreign run, invalid shape and mismatched envelope", () => {
    for (const raw of [
      frame({ ...accepted, run_id: "00000000-0000-4000-8000-000000000002" }),
      frame({ ...accepted, data: { status: "wrong" } }),
      frame(accepted).replace("id: 1", "id: 2"),
    ]) {
      expect(() => new RunStreamDecoder(run).push(encode(raw))).toThrow(
        StreamProtocolError,
      );
    }
  });
  it("rejects malformed JSON and invalid UTF-8", () => {
    expect(() =>
      new RunStreamDecoder(run).push(encode("data: {bad}\n\n")),
    ).toThrow(StreamProtocolError);
    expect(() =>
      new RunStreamDecoder(run).push(new Uint8Array([0xff])),
    ).toThrow(StreamProtocolError);
  });
});
