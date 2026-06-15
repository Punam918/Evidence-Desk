import type { RunEvent } from "@evidencedesk/api-client/events";
import validateEvent from "@evidencedesk/api-client/validate-event";

const terminal = new Set([
  "answer.completed",
  "run.failed",
  "run.cancelled",
  "run.refused",
]);
const maxFrameChars = 1024 * 1024;

export class StreamProtocolError extends Error {}
export class StreamGap extends StreamProtocolError {
  constructor(public readonly after: number) {
    super("Event sequence gap; reconnect from the last committed event.");
  }
}

/** Incremental UTF-8/SSE decoder. Reconnects consume events; they never resubmit a run. */
export class RunStreamDecoder {
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });
  private buffer = "";
  private data: string[] = [];
  private dataChars = 0;
  private eventId = "";
  private eventType = "";
  private ended = false;
  public lastSequence: number;

  constructor(
    private readonly runId: string,
    after = 0,
  ) {
    this.lastSequence = after;
  }

  push(bytes: Uint8Array): RunEvent[] {
    try {
      this.buffer += this.decoder.decode(bytes, { stream: true });
    } catch {
      throw new StreamProtocolError("Invalid UTF-8 event stream.");
    }
    const committed = this.lastSequence;
    const wasEnded = this.ended;
    const events: RunEvent[] = [];
    try {
      while (true) {
        const newline = this.buffer.search(/[\r\n]/);
        if (newline < 0) break;
        const char = this.buffer[newline];
        if (char === "\r" && newline === this.buffer.length - 1) break;
        const line = this.buffer.slice(0, newline);
        const width =
          char === "\r" && this.buffer[newline + 1] === "\n" ? 2 : 1;
        this.buffer = this.buffer.slice(newline + width);
        const event = this.line(line);
        if (event) events.push(event);
      }
      if (this.buffer.length + this.dataChars > maxFrameChars)
        throw new StreamProtocolError("Event frame is too large.");
      return events;
    } catch (error) {
      // A failed batch returns no events: do not skip its valid prefix on reconnect.
      this.lastSequence = committed;
      this.ended = wasEnded;
      if (error instanceof StreamGap) throw new StreamGap(committed);
      throw error;
    }
  }

  /** A partial frame at EOF is discarded. The caller reconnects from lastSequence. */
  finish(): void {
    try {
      this.decoder.decode();
    } catch {
      throw new StreamGap(this.lastSequence);
    }
    this.buffer = "";
    this.reset();
  }

  private reset(): void {
    this.data = [];
    this.dataChars = 0;
    this.eventId = "";
    this.eventType = "";
  }

  private line(line: string): RunEvent | null {
    if (line === "") {
      if (!this.data.length) {
        this.reset();
        return null;
      }
      const id = this.eventId;
      const type = this.eventType;
      const encoded = this.data.join("\n");
      this.reset();
      let value: unknown;
      try {
        value = JSON.parse(encoded);
      } catch {
        throw new StreamProtocolError("Malformed event JSON.");
      }
      if (
        !validateEvent(value) ||
        !/^[0-9]+$/.test(id) ||
        !Number.isSafeInteger(value.seq) ||
        Number(id) !== value.seq ||
        type !== value.type ||
        value.run_id !== this.runId
      ) {
        throw new StreamProtocolError("Event does not match the run protocol.");
      }
      if (value.seq <= this.lastSequence) return null;
      if (this.ended)
        throw new StreamProtocolError(
          "Received an event after the terminal event.",
        );
      if (value.seq !== this.lastSequence + 1)
        throw new StreamGap(this.lastSequence);
      this.lastSequence = value.seq;
      this.ended = terminal.has(value.type);
      return value;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") this.eventId = value;
    if (field === "event") this.eventType = value;
    if (field === "data") {
      this.data.push(value);
      this.dataChars += value.length;
    }
    if (this.dataChars > maxFrameChars)
      throw new StreamProtocolError("Event frame is too large.");
    return null;
  }
}

export async function* readRunStream(
  response: Response,
  runId: string,
  after = 0,
): AsyncGenerator<RunEvent> {
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get("content-type")?.startsWith("text/event-stream")
  ) {
    throw new StreamProtocolError("Event stream is unavailable.");
  }
  const reader = response.body.getReader();
  const decoder = new RunStreamDecoder(runId, after);
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        decoder.finish();
        return;
      }
      for (const event of decoder.push(value)) yield event;
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}
