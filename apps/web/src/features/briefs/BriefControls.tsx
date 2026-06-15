import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

export function BriefControls({
  workspace,
  run,
  message,
  destination,
}: {
  workspace: string;
  run: string;
  message: Schema["Message"];
  destination: string;
}) {
  const { session } = useSession();
  const cache = useQueryClient();
  const [title, setTitle] = useState("Evidence brief");
  const key = [session.user.id, workspace, "tool-executions", run];
  const executions = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => api.executions(workspace, run, signal),
    refetchInterval: 3000,
    refetchOnWindowFocus: "always",
  });
  const propose = useMutation({
    mutationFn: () =>
      api.proposeBrief(
        workspace,
        run,
        { title, answer_message_id: message.id },
        crypto.randomUUID(),
      ),
    onSuccess: () => cache.invalidateQueries({ queryKey: key }),
  });
  const decide = useMutation({
    mutationFn: ({
      execution,
      decision,
    }: {
      execution: Schema["ToolExecution"];
      decision: "approve" | "reject";
    }) =>
      api.approveBrief(
        workspace,
        execution.id,
        { decision, expected_payload_sha256: execution.payload_sha256 },
        crypto.randomUUID(),
      ),
    onSuccess: () => cache.invalidateQueries({ queryKey: key }),
  });
  const items = executions.error
    ? []
    : (executions.data?.items.filter(
        (e) => e.proposal?.answer_message_id === message.id,
      ) ?? []);
  return (
    <section className="brief-controls" aria-label="Save answer as brief">
      <h3>Save a private brief</h3>
      <p>
        The answer and citations above are the complete content to be saved in{" "}
        <strong>{destination} / Your briefs</strong>. Only you can read it.
      </p>
      <label>
        Brief title
        <input
          value={title}
          maxLength={120}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <button
        type="button"
        disabled={
          !title.trim() ||
          propose.isPending ||
          !executions.data ||
          !!executions.error
        }
        onClick={() => propose.mutate()}
      >
        Preview save proposal
      </button>
      <ProblemBanner
        error={propose.error ?? decide.error ?? executions.error}
      />
      {items.map((execution) => (
        <div className="brief-proposal" key={execution.id}>
          <h4>{execution.proposal?.title}</h4>
          <p role="status">
            {execution.status === "succeeded" && execution.brief_id
              ? "Brief saved."
              : `Save status: ${execution.status}`}
          </p>
          {["proposed", "awaiting_approval"].includes(execution.status) && (
            <>
              <p>
                Approval expires{" "}
                {execution.expires_at
                  ? new Date(execution.expires_at).toLocaleTimeString()
                  : "soon"}
                . Saving copies the displayed answer with its citations.
              </p>
              <button
                type="button"
                className="primary"
                disabled={decide.isPending}
                onClick={() =>
                  decide.mutate({ execution, decision: "approve" })
                }
              >
                Approve save
              </button>{" "}
              <button
                type="button"
                disabled={decide.isPending}
                onClick={() => decide.mutate({ execution, decision: "reject" })}
              >
                Reject
              </button>
            </>
          )}
          {execution.status === "approved" && (
            <p>Approval recorded. Waiting for the save to finish.</p>
          )}
          {execution.status === "succeeded" && execution.brief_id && (
            <Link to={`/workspaces/${workspace}/briefs`}>Open your briefs</Link>
          )}
        </div>
      ))}
    </section>
  );
}
