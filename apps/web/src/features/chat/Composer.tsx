import type { FormEvent } from "react";
import { ArrowUp, Square } from "lucide-react";
import { Link } from "react-router";
import type { Schema } from "../../lib/api";
import { ProblemBanner } from "../../components/ProblemBanner";

type Collection<T> = {
  data?: { items: T[] };
  error: unknown;
  refetch: () => unknown;
};
type Props = {
  workspaceId: string;
  canManageSources: boolean;
  query: string;
  setQuery: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  knowledge: string;
  setKnowledge: (value: string) => void;
  available: Schema["Model"][];
  models: Collection<Schema["Model"]>;
  sources: Collection<Schema["KnowledgeBase"]>;
  send: { error: unknown };
  stop: { error: unknown; isPending: boolean; mutate: () => void };
  isRunning: boolean;
  canSend: boolean;
  submit: (event: FormEvent) => void;
};
export function Composer({
  workspaceId,
  canManageSources,
  query,
  setQuery,
  model,
  setModel,
  knowledge,
  setKnowledge,
  available,
  models,
  sources,
  send,
  stop,
  isRunning,
  canSend,
  submit,
}: Props) {
  const sourceMessage = sources.error
    ? "Workspace sources could not be loaded."
    : !sources.data
      ? "Loading workspace sources…"
      : !sources.data.items.length
        ? canManageSources
          ? "Add a knowledge base and publish a document before asking a question."
          : "This workspace has no knowledge bases yet. An editor or administrator can add shared sources."
        : knowledge === "all" && sources.data.items.length > 10
          ? "Choose one knowledge base to keep this question within the source limit."
          : knowledge !== "all" &&
              !sources.data.items.some((item) => item.id === knowledge)
            ? "The selected knowledge base is no longer available. Choose another source."
            : null;
  const selectedModel = models.data?.items.find((item) => item.key === model);
  const modelMessage = models.error
    ? "Model availability could not be checked."
    : !models.data
      ? "Checking available models…"
      : !available.length
        ? "No model is ready to answer. Check availability again, or ask your workspace administrator."
        : model !== "auto" && !selectedModel?.available
          ? "The selected model is unavailable. Choose an available model to continue."
          : null;
  return (
    <div className="composer-area">
      <div className="composer-inner">
        <ProblemBanner error={send.error ?? stop.error} />
        {(sourceMessage || modelMessage) && (
          <div
            id="composer-readiness"
            className="composer-readiness"
            role="status"
            aria-live="polite"
          >
            {sourceMessage && <p>{sourceMessage}</p>}
            {modelMessage && <p>{modelMessage}</p>}
            <div className="readiness-actions">
              {!!sources.error && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void sources.refetch()}
                >
                  Retry sources
                </button>
              )}
              {!!sources.data && !sources.data.items.length && (
                <Link to={`/workspaces/${workspaceId}/knowledge`}>
                  Open workspace sources
                </Link>
              )}
              {modelMessage && !!models.data && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void models.refetch()}
                >
                  Check model availability
                </button>
              )}
              {!!models.error && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void models.refetch()}
                >
                  Retry models
                </button>
              )}
            </div>
          </div>
        )}
        <form className="composer" onSubmit={submit}>
          <label className="sr-only" htmlFor="question">
            Ask a question about your documents
          </label>
          <textarea
            id="question"
            placeholder="Ask a question about your documents…"
            value={query}
            maxLength={2000}
            rows={2}
            aria-describedby={
              sourceMessage || modelMessage
                ? "composer-readiness composer-help"
                : "composer-help"
            }
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                if (canSend) event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="composer-tools">
            <label>
              <span className="composer-control-label">Knowledge base</span>
              <select
                aria-label="Knowledge base"
                value={knowledge}
                onChange={(event) => setKnowledge(event.target.value)}
              >
                <option value="all">
                  {sources.data && sources.data.items.length > 10
                    ? "Choose a knowledge base"
                    : sources.data?.items.length
                      ? "All knowledge bases"
                      : "No sources available"}
                </option>
                {sources.data?.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="composer-control-label">Model</span>
              <select
                aria-label="Model"
                aria-describedby="model-guidance"
                value={model}
                onChange={(event) => setModel(event.target.value)}
              >
                <option value="auto">
                  {available.length
                    ? "Auto · available models"
                    : "Models unavailable"}
                </option>
                {models.data?.items.map((item) => (
                  <option
                    key={item.key}
                    value={item.key}
                    disabled={!item.available}
                  >
                    {item.display_name}
                    {item.available
                      ? ""
                      : ` · ${item.unavailable_reason ?? "unavailable"}`}
                  </option>
                ))}
              </select>
            </label>
            {isRunning ? (
              <button
                className="send-button stop"
                type="button"
                aria-label="Stop generating"
                onClick={() => stop.mutate()}
                disabled={stop.isPending}
              >
                <Square size={17} />
              </button>
            ) : (
              <button
                className="send-button primary"
                type="submit"
                aria-label="Send question"
                disabled={!canSend}
              >
                <ArrowUp size={19} />
              </button>
            )}
          </div>
        </form>
        <p id="model-guidance" className="model-guidance">
          {model === "auto"
            ? "Auto selects an available model allowed by workspace policy."
            : selectedModel?.privacy_class === "private"
              ? "Private model · generation uses the configured private endpoint."
              : selectedModel?.privacy_class === "hosted"
                ? "Hosted model · relevant source passages are sent to the configured provider."
                : "Choose an available model to answer your question."}
        </p>
        <div id="composer-help" className="composer-help">
          <span>
            Answers can be incomplete. Open citations to check the evidence.
          </span>
          <span>{query.length}/2000</span>
        </div>
      </div>
    </div>
  );
}
