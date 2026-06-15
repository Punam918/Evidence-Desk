import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";
import { RevisionSelect, type Selection } from "./RevisionSelect";

export default function ComparePage() {
  const { workspaceId = "" } = useParams();
  return <Compare key={workspaceId} id={workspaceId} />;
}
function Compare({ id }: { id: string }) {
  const { session } = useSession();
  const navigate = useNavigate();
  const [left, setLeft] = useState<Selection | null>(null);
  const [right, setRight] = useState<Selection | null>(null);
  const [focus, setFocus] = useState("");
  const [model, setModel] = useState<Schema["RunInput"]["model_key"]>("auto");
  const attempt = useRef<{
    body: Schema["RunInput"];
    key: string;
    conversation?: Schema["Conversation"];
  } | null>(null);
  const models = useQuery({
    queryKey: [session.user.id, id, "models"],
    queryFn: ({ signal }) => api.models(id, signal),
  });
  const send = useMutation({
    mutationFn: async () => {
      if (!left || !right) throw new Error("Choose two revisions.");
      attempt.current ??= {
        key: crypto.randomUUID(),
        body: {
          query: focus.trim(),
          kb_ids: [...new Set([left.kb, right.kb])],
          model_key: model,
          intent: "compare",
          revision_ids: [left.revision, right.revision],
          expected_conversation_version: 1,
        },
      };
      const current = attempt.current;
      current.conversation ??= await api.createConversation(
        id,
        "Compare: " + current.body.query.slice(0, 95),
      );
      return api.start(id, current.conversation.id, current.body, current.key);
    },
    onSuccess: (result) =>
      navigate(
        `/workspaces/${id}/conversations/${result.conversation_id}?run=${result.id}`,
      ),
  });
  const locked = send.isPending || !!attempt.current;
  const canSend =
    left &&
    right &&
    left.revision !== right.revision &&
    focus.trim() &&
    models.data?.items.some(
      (m) => m.available && (model === "auto" || m.key === model),
    );
  return (
    <main className="knowledge-page">
      <header className="knowledge-header">
        <Link to={`/workspaces/${id}`}>Back to conversations</Link>
      </header>
      <p className="eyebrow">GROUNDED COMPARISON</p>
      <h1>Compare two revisions</h1>
      <p>
        Select two published versions and describe what you want to compare.
        Each side keeps its own evidence and citations.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canSend && !send.isPending) send.mutate();
        }}
      >
        <div className="compare-grid">
          <RevisionSelect
            workspace={id}
            side="Left"
            disabled={locked}
            onChange={setLeft}
          />
          <RevisionSelect
            workspace={id}
            side="Right"
            disabled={locked}
            onChange={setRight}
          />
        </div>
        {left && right && left.revision === right.revision && (
          <p role="alert">Choose two different revisions.</p>
        )}
        <label className="field">
          Comparison focus
          <textarea
            required
            maxLength={2000}
            value={focus}
            disabled={locked}
            onChange={(e) => setFocus(e.target.value)}
            placeholder="What changed in the retention policy?"
          />
        </label>
        <label className="field">
          Model
          <select
            value={model}
            disabled={locked}
            onChange={(e) =>
              setModel(e.target.value as Schema["RunInput"]["model_key"])
            }
          >
            <option value="auto">Auto · policy eligible</option>
            {models.data?.items.map((m) => (
              <option key={m.key} value={m.key} disabled={!m.available}>
                {m.display_name}
                {m.available ? "" : " · unavailable"}
              </option>
            ))}
          </select>
        </label>
        <ProblemBanner error={send.error ?? models.error} />
        <button
          className="primary"
          type="submit"
          disabled={!canSend || send.isPending}
        >
          {send.isPending
            ? "Starting comparison…"
            : send.isError
              ? "Retry this comparison"
              : "Compare revisions"}
        </button>
      </form>
    </main>
  );
}
