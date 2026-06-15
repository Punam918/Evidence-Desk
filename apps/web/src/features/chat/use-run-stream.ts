import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { readRunStream, StreamGap, StreamProtocolError } from "../../lib/sse";
import { workspacePath, type Schema } from "../../lib/api";
import { initialStream, reduceStream, type StreamState } from "./stream-state";
export function useRunStream(
  workspace: string,
  user: string,
  run: Schema["Run"] | null,
) {
  const cache = useQueryClient();
  const [state, setState] = useState<StreamState | null>(null);
  const [connection, setConnection] = useState("");
  useEffect(() => {
    if (!run) {
      setState(null);
      return;
    }
    const activeRun = run;
    const controller = new AbortController();
    let current = initialStream(activeRun.id);
    setState(current);
    async function consume() {
      for (let retry = 0; retry < 6 && !controller.signal.aborted; retry++) {
        try {
          setConnection(retry ? "Reconnecting…" : "Connected");
          const response = await fetch(
            `${workspacePath(workspace)}/runs/${encodeURIComponent(activeRun.id)}/events`,
            {
              headers: current.seq
                ? { "Last-Event-ID": String(current.seq) }
                : {},
              signal: controller.signal,
              credentials: "same-origin",
              redirect: "error",
              cache: "no-store",
            },
          );
          if (response.status === 401) {
            window.dispatchEvent(new Event("ed:session-expired"));
            return;
          }
          if ([403, 404, 409, 410].includes(response.status)) {
            setState({
              ...current,
              state: "failed",
              text: "",
              answer: undefined,
              citations: [],
              error:
                "This stream is no longer available. Refresh the conversation to check its saved state.",
            });
            await cache.invalidateQueries({
              queryKey: [
                user,
                workspace,
                "messages",
                activeRun.conversation_id,
              ],
            });
            return;
          }
          for await (const event of readRunStream(
            response,
            activeRun.id,
            current.seq,
          )) {
            if (controller.signal.aborted) return;
            current = reduceStream(current, event);
            setState(current);
            if (
              ["completed", "failed", "cancelled", "refused"].includes(
                current.state,
              )
            ) {
              setConnection("");
              await cache.invalidateQueries({ queryKey: [user, workspace] });
              return;
            }
          }
        } catch (error) {
          if (controller.signal.aborted) return;
          if (
            error instanceof StreamProtocolError &&
            !(error instanceof StreamGap)
          ) {
            setState({
              ...current,
              state: "failed",
              text: "",
              error:
                "The answer stream was invalid. Refresh to check the saved conversation.",
            });
            return;
          }
        }
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, Math.min(5000, 500 * 2 ** retry));
          controller.signal.addEventListener(
            "abort",
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
        });
      }
      if (!controller.signal.aborted)
        setConnection("Connection lost. Refresh to check the saved answer.");
    }
    void consume();
    return () => controller.abort();
  }, [run?.id, workspace, user, cache]);
  return {
    state: state?.runId === run?.id ? state : null,
    connection: state?.runId === run?.id ? connection : "",
  };
}
