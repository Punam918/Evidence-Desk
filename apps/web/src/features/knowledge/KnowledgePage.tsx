import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { BookOpen, FileText, Trash2 } from "lucide-react";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

import { UploadForm } from "./UploadForm";

export default function KnowledgePage() {
  const { workspaceId = "" } = useParams();
  return <Knowledge key={workspaceId} workspace={workspaceId} />;
}
function Knowledge({ workspace }: { workspace: string }) {
  const { session } = useSession();
  const user = session.user.id;
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("kb");
  const [name, setName] = useState("");
  const [deleting, setDeleting] = useState<Schema["KnowledgeBase"] | null>(
    null,
  );
  const [notice, setNotice] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const current = useQuery({
    queryKey: [user, workspace, "workspace"],
    queryFn: ({ signal }) => api.workspace(workspace, signal),
  });
  const knowledge = useInfiniteQuery({
    queryKey: [user, workspace, "knowledge-pages"],
    queryFn: ({ signal, pageParam }) =>
      api.knowledge(workspace, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const collections = knowledge.data?.pages.flatMap((page) => page.items);
  const selected = collections?.find((item) => item.id === selectedId);
  const documents = useInfiniteQuery({
    queryKey: [user, workspace, "documents", selectedId],
    queryFn: ({ signal, pageParam }) =>
      api.documents(workspace, selectedId!, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !!selected,
  });
  const canCreate =
    current.data && ["editor", "admin", "owner"].includes(current.data.role);
  const canDelete =
    current.data && ["admin", "owner"].includes(current.data.role);
  const create = useMutation({
    mutationFn: (value: string) => api.createKnowledge(workspace, value),
    onSuccess: async (value) => {
      setName("");
      await cache.invalidateQueries({
        queryKey: [user, workspace],
      });
      setParams({ kb: value.id });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteKnowledge(workspace, id),
    onSuccess: async () => {
      setDeleting(null);
      setParams({});
      setNotice(
        "Knowledge base removed. Its documents are no longer accessible.",
      );
      await cache.invalidateQueries({ queryKey: [user, workspace] });
    },
  });
  useEffect(() => {
    if (deleting) dialog.current?.showModal();
    else dialog.current?.close();
  }, [deleting]);
  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }
  return (
    <main className="knowledge-page">
      <header className="knowledge-header">
        <Link className="button" to={`/workspaces/${workspace}`}>
          Back to chat
        </Link>
      </header>
      <p className="eyebrow">{current.data?.name ?? "WORKSPACE"} / SOURCES</p>
      <div className="section-title">
        <div>
          <h1>Your knowledge bases</h1>
          <p className="muted">
            Organize documents into collections. Workspace members share access
            to these sources.
          </p>
        </div>
        <BookOpen size={32} />
      </div>
      <ProblemBanner
        error={current.error ?? knowledge.error}
        retry={() => void knowledge.refetch()}
      />
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <div className="knowledge-layout">
        <aside className="knowledge-collections" aria-label="Knowledge bases">
          {knowledge.isPending && <p role="status">Loading knowledge bases…</p>}
          {collections?.length === 0 && (
            <div className="empty-collection">
              <BookOpen size={24} />
              <h2>No knowledge bases yet</h2>
              <p>
                {canCreate
                  ? "Create a collection for your documents."
                  : "An editor or administrator can add shared knowledge here."}
              </p>
            </div>
          )}
          <label className="mobile-collection-select">
            Knowledge base
            <select
              value={selectedId ?? ""}
              onChange={(event) =>
                setParams(event.target.value ? { kb: event.target.value } : {})
              }
            >
              <option value="">Choose a knowledge base</option>
              {collections?.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.document_count} documents
                </option>
              ))}
            </select>
          </label>
          <div className="collection-options">
            {collections?.map((item) => (
              <button
                key={item.id}
                className={`collection-button ${selected?.id === item.id ? "selected" : ""}`}
                onClick={() => setParams({ kb: item.id })}
                aria-pressed={selected?.id === item.id}
              >
                <span>{item.name}</span>
                <small>
                  {item.document_count}{" "}
                  {item.document_count === 1 ? "document" : "documents"}
                </small>
              </button>
            ))}
          </div>
          {knowledge.hasNextPage && (
            <button
              onClick={() => void knowledge.fetchNextPage()}
              disabled={knowledge.isFetchingNextPage}
            >
              Load more knowledge bases
            </button>
          )}
        </aside>
        <section className="document-list" aria-labelledby="documents-title">
          <div className="section-title">
            <h2 id="documents-title">
              {selected?.name ?? "Choose a knowledge base"}
            </h2>
            {selected && canDelete && (
              <button
                className="danger-button"
                onClick={() => setDeleting(selected)}
                aria-label={`Delete ${selected.name}`}
              >
                <Trash2 size={16} /> Delete
              </button>
            )}
          </div>
          {!selected && (
            <p className="muted">
              Select a collection to view its documents and revision history.
            </p>
          )}
          <ProblemBanner
            error={documents.error}
            retry={() => void documents.refetch()}
          />
          {selected && documents.isPending && (
            <p role="status">Loading documents…</p>
          )}
          {selected && documents.data?.pages[0].items.length === 0 && (
            <div className="document-empty">
              <FileText size={30} />
              <h3>No documents in this collection</h3>
              <p>Documents will appear here after they are added.</p>
            </div>
          )}
          {documents.data?.pages
            .flatMap((page) => page.items)
            .map((document) => (
              <Link
                className="document-row"
                key={document.id}
                to={`/workspaces/${workspace}/documents/${document.id}`}
              >
                <FileText size={22} />
                <div>
                  <h3>{document.title}</h3>
                  <small>
                    {document.active_revision_id
                      ? "Published version available"
                      : "No published version yet"}
                  </small>
                </div>
                <span className="status-badge">
                  Latest: {document.status.replaceAll("_", " ")}
                </span>
              </Link>
            ))}
          {documents.hasNextPage && (
            <button
              onClick={() => void documents.fetchNextPage()}
              disabled={documents.isFetchingNextPage}
            >
              Load more documents
            </button>
          )}
          {selected && canCreate && (
            <section
              id="knowledge-upload"
              className="knowledge-upload"
              aria-label="Add a source document"
            >
              <h3>Add a document</h3>
              <UploadForm
                key={selected.id}
                workspace={workspace}
                target={{ kb: selected.id }}
              />
            </section>
          )}
          {canCreate && (
            <details
              className="collection-management"
              open={collections?.length === 0}
            >
              <summary>New knowledge base</summary>
              <form onSubmit={submit} className="collection-form">
                <label htmlFor="kb-name">New knowledge base</label>
                <input
                  id="kb-name"
                  maxLength={120}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Collection name"
                  required
                />
                <button
                  className="primary"
                  disabled={!name.trim() || create.isPending}
                >
                  {create.isPending ? "Creating…" : "Create knowledge base"}
                </button>
                <ProblemBanner error={create.error} />
              </form>
            </details>
          )}
        </section>
      </div>
      <dialog
        ref={dialog}
        className="source-dialog"
        aria-labelledby="delete-kb-title"
        onCancel={() => !remove.isPending && setDeleting(null)}
        onClose={() => setDeleting(null)}
      >
        <h2 id="delete-kb-title">Delete {deleting?.name}?</h2>
        <p>
          This removes access to every document in the knowledge base. Answers
          citing these documents will become unavailable.
        </p>
        <ProblemBanner error={remove.error} />
        <div className="dialog-actions">
          <button onClick={() => setDeleting(null)} disabled={remove.isPending}>
            Keep knowledge base
          </button>
          <button
            className="danger-button"
            disabled={remove.isPending || !deleting}
            onClick={() => deleting && remove.mutate(deleting.id)}
          >
            {remove.isPending ? "Removing…" : "Delete knowledge base"}
          </button>
        </div>
      </dialog>
    </main>
  );
}
