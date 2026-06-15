import { useState, useEffect, type FormEvent } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { BookOpen, ShieldCheck } from "lucide-react";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";
import { AnswerView } from "../../components/AnswerView";
import { FeedbackControls } from "../reports/FeedbackControls";
import { BriefControls } from "../briefs/BriefControls";
import { Composer } from "./Composer";
import { useRunStream } from "./use-run-stream";

export default function ChatWorkspace() {
  const { workspaceId = "" } = useParams();
  return <Workspace key={workspaceId} id={workspaceId} />;
}
function Workspace({ id }: { id: string }) {
  const { conversationId } = useParams();
  const [params] = useSearchParams();
  const runId = params.get("run");
  const { session } = useSession();
  const location = useLocation();
  const user = session.user.id;
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [model, setModel] = useState("auto");
  const [knowledge, setKnowledge] = useState("all");
  useEffect(() => setQuery(""), [location.state?.newChatKey]);
  const workspace = useQuery({
    queryKey: [user, id, "workspace"],
    queryFn: ({ signal }) => api.workspace(id, signal),
  });
  const conversation = useQuery({
    queryKey: [user, id, "conversation", conversationId],
    queryFn: ({ signal }) => api.conversation(id, conversationId!, signal),
    enabled: !!conversationId,
  });
  const messages = useInfiniteQuery({
    queryKey: [user, id, "messages", conversationId],
    queryFn: ({ signal, pageParam }) =>
      api.messages(id, conversationId!, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !!conversationId,
  });
  const models = useQuery({
    queryKey: [user, id, "models"],
    queryFn: ({ signal }) => api.models(id, signal),
  });
  const sources = useQuery({
    queryKey: [user, id, "knowledge"],
    queryFn: ({ signal }) => api.knowledge(id, signal),
  });
  const run = useQuery({
    queryKey: [user, id, "run", runId],
    queryFn: ({ signal }) => api.run(id, runId!, signal),
    enabled: !!runId,
  });
  const active =
    run.data && run.data.conversation_id === conversationId ? run.data : null;
  const comparison = active?.comparison ?? [];
  const stream = useRunStream(id, user, active);
  const isRunning =
    !!active &&
    (stream.state
      ? ["queued", "draft"].includes(stream.state.state)
      : ["queued", "running"].includes(active.status));
  const available = models.data?.items.filter((item) => item.available) ?? [];
  const canManageSources =
    !!workspace.data &&
    ["owner", "admin", "editor"].includes(workspace.data.role);
  const chosenSources =
    knowledge === "all"
      ? sources.data && sources.data.items.length <= 10
        ? sources.data.items.map((item) => item.id)
        : []
      : sources.data?.items.some((item) => item.id === knowledge)
        ? [knowledge]
        : [];
  const send = useMutation({
    mutationFn: async (text: string) => {
      const selected = conversationId
        ? conversation.data
        : await api.createConversation(id, text.slice(0, 100));
      if (!selected) throw new Error("Reload the conversation before sending.");
      const value: Schema["RunInput"] = {
        query: text,
        kb_ids: chosenSources,
        model_key: model as Schema["RunInput"]["model_key"],
        intent: "ask",
        revision_ids: [],
        expected_conversation_version: selected.version,
      };
      const result = await api.start(
        id,
        selected.id,
        value,
        crypto.randomUUID(),
      );
      return result;
    },
    onSuccess: (result) => {
      setQuery("");
      navigate(
        `/workspaces/${id}/conversations/${result.conversation_id}?run=${result.id}`,
      );
    },
  });
  const stop = useMutation({ mutationFn: () => api.cancel(id, active!.id) });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (canSend) send.mutate(query.trim());
  }
  const saved = messages.data?.pages.flatMap((page) => page.items) ?? [];
  const draft =
    stream.state &&
    !saved.some((message) => message.id === stream.state?.messageId)
      ? stream.state
      : null;
  const canSend =
    !!query.trim() &&
    !sources.error &&
    !models.error &&
    query.length <= 2000 &&
    chosenSources.length > 0 &&
    available.length > 0 &&
    (model === "auto" || available.some((item) => item.key === model)) &&
    !isRunning &&
    !send.isPending &&
    (!conversationId || !!conversation.data);
  return (
    <main className="chat-panel">
      <header className="conversation-heading">
        <p className="eyebrow">PRIVATE CONVERSATION</p>
        <h1>{conversation.data?.title ?? "New conversation"}</h1>
      </header>
      {active?.intent === "compare" && comparison.length === 2 && (
        <section
          className="comparison-context"
          aria-label="Saved comparison context"
        >
          <p className="eyebrow">SAVED COMPARISON</p>
          <div>
            {comparison.map((side) => (
              <span key={side.side}>
                <strong>{side.side}</strong>{" "}
                {side.available
                  ? `${side.title} · version ${side.revision_no}`
                  : "Source is no longer available"}
              </span>
            ))}
          </div>
        </section>
      )}
      <section id="conversation" className="conversation" tabIndex={-1}>
        <ProblemBanner
          error={
            workspace.error ?? conversation.error ?? messages.error ?? run.error
          }
        />
        {!conversationId && (
          <div className="chat-empty">
            <div className="empty-mark">
              <BookOpen size={30} strokeWidth={1.5} />
            </div>
            <p className="eyebrow">START WITH A QUESTION</p>
            <h2>
              Find the answer.
              <br />
              <span>See the evidence.</span>
            </h2>
            <p>
              Ask across your organization’s knowledge bases. Check the cited
              sources and see when there is not enough evidence.
            </p>
            <div className="empty-principles">
              <span>
                <BookOpen size={17} /> Shared knowledge
              </span>
              <span>
                <ShieldCheck size={17} /> Your private chat
              </span>
            </div>
            <Link className="empty-browse" to={`/workspaces/${id}/knowledge`}>
              {sources.data?.items.length === 0 && canManageSources
                ? "Create a knowledge base →"
                : "Browse workspace sources →"}
            </Link>
          </div>
        )}
        {messages.isPending && conversationId && (
          <p role="status">Loading conversation…</p>
        )}
        {saved.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <div className="message-label">
              {message.role === "user" ? "You" : "EvidenceDesk"}
              <span>
                {message.state !== "final"
                  ? message.state
                  : message.role === "assistant"
                    ? "Answer ready"
                    : ""}
              </span>
            </div>
            {message.source_status === "unavailable" ? (
              <p className="muted">
                This answer is unavailable because a source was removed.
              </p>
            ) : message.answer ? (
              <AnswerView
                workspaceId={id}
                answer={message.answer}
                citations={message.citations}
              />
            ) : (
              <p className="plain-text">{message.text}</p>
            )}
            {message.role === "assistant" &&
              message.state === "final" &&
              message.source_status === "available" &&
              message.answer && (
                <FeedbackControls workspaceId={id} messageId={message.id} />
              )}
            {message.role === "assistant" && message.run_id && (
              <Link
                className="run-context-link"
                to={`/workspaces/${id}/conversations/${conversationId}?run=${message.run_id}`}
              >
                View saved run context
              </Link>
            )}
            {message.run_id &&
              message.state === "final" &&
              message.answer &&
              message.source_status === "available" &&
              workspace.data &&
              ["owner", "admin", "editor"].includes(workspace.data.role) && (
                <BriefControls
                  workspace={id}
                  run={message.run_id}
                  message={message}
                  destination={workspace.data.name}
                />
              )}
          </article>
        ))}
        {messages.hasNextPage && (
          <button
            onClick={() => void messages.fetchNextPage()}
            disabled={messages.isFetchingNextPage}
          >
            Load more messages
          </button>
        )}
        {draft && (
          <article className="message assistant">
            <div className="message-label">
              EvidenceDesk{" "}
              <span>
                {draft.state === "draft"
                  ? "Draft · checking sources"
                  : draft.phase}
              </span>
            </div>
            {draft.text && (
              <p className="plain-text draft-text">{draft.text}</p>
            )}
            {draft.answer && (
              <AnswerView
                workspaceId={id}
                answer={draft.answer}
                citations={draft.citations}
              />
            )}
            {draft.error && (
              <p role="alert" className="error-text">
                {draft.error}
              </p>
            )}
          </article>
        )}
        <p className="stream-status" role="status" aria-live="polite">
          {isRunning
            ? `${stream.state?.phase ?? "Queued"} · ${stream.connection}`
            : stream.connection}
        </p>
      </section>
      <Composer
        workspaceId={id}
        canManageSources={canManageSources}
        query={query}
        setQuery={setQuery}
        model={model}
        setModel={setModel}
        knowledge={knowledge}
        setKnowledge={setKnowledge}
        available={available}
        models={models}
        sources={sources}
        send={send}
        stop={stop}
        isRunning={isRunning}
        canSend={canSend}
        submit={submit}
      />
    </main>
  );
}
