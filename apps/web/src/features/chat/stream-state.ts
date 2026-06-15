import type { RunEvent } from "@evidencedesk/api-client/events";
import type { Schema } from "../../lib/api";
export type StreamState = {
  runId: string;
  seq: number;
  state: "queued" | "draft" | "completed" | "failed" | "cancelled" | "refused";
  phase: string;
  text: string;
  messageId?: string;
  answer?: Schema["Answer"];
  citations: Schema["Citation"][];
  error?: string;
};
export function initialStream(runId: string): StreamState {
  return {
    runId,
    seq: 0,
    state: "queued",
    phase: "Queued",
    text: "",
    citations: [],
  };
}
export function reduceStream(state: StreamState, event: RunEvent): StreamState {
  if (
    event.run_id !== state.runId ||
    event.seq <= state.seq ||
    ["completed", "failed", "cancelled", "refused"].includes(state.state)
  )
    return state;
  if (event.seq !== state.seq + 1)
    throw new Error("A stream event is missing.");
  const next = { ...state, seq: event.seq };
  switch (event.type) {
    case "run.accepted":
      return next;
    case "run.phase":
      return { ...next, phase: event.data.phase };
    case "answer.delta":
      return { ...next, state: "draft", text: state.text + event.data.text };
    case "answer.completed":
      return {
        ...next,
        state: "completed",
        phase: "Complete",
        text: "",
        messageId: event.data.message_id,
        answer: event.data.answer,
        citations: event.data.citations,
      };
    case "run.failed":
      return {
        ...next,
        state: "failed",
        text: "",
        error:
          "This answer could not be completed. You can send a new request.",
      };
    case "run.cancelled":
      return { ...next, state: "cancelled", text: "", phase: "Stopped" };
    case "run.refused":
      return {
        ...next,
        state: "refused",
        text: "",
        phase: "The model could not answer this request.",
      };
  }
}
