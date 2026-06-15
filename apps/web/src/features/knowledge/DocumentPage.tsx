import {
  useInfiniteQuery,
  useQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router";
import { api } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";
import { JobStatus } from "./JobStatus";
import { UploadForm } from "./UploadForm";

export default function DocumentPage() {
  const { workspaceId = "", documentId = "" } = useParams();
  const { session } = useSession();
  const [search] = useSearchParams();
  const job = search.get("job");
  const cache = useQueryClient();
  const workspace = useQuery({
    queryKey: [session.user.id, workspaceId, "workspace"],
    queryFn: ({ signal }) => api.workspace(workspaceId, signal),
  });
  const canEdit =
    workspace.data &&
    ["editor", "admin", "owner"].includes(workspace.data.role);
  const reprocess = useMutation({
    mutationFn: ({ version, key }: { version: number; key: string }) =>
      api.reprocess(workspaceId, documentId, version, key),
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: [session.user.id, workspaceId] }),
  });
  const document = useQuery({
    queryKey: [session.user.id, workspaceId, "document", documentId],
    queryFn: ({ signal }) => api.document(workspaceId, documentId, signal),
  });
  const revisions = useInfiniteQuery({
    queryKey: [session.user.id, workspaceId, "revisions", documentId],
    queryFn: ({ signal, pageParam }) =>
      api.revisions(workspaceId, documentId, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: !!document.data,
  });
  return (
    <main className="knowledge-page">
      <header className="knowledge-header">
        <Link
          className="button"
          to={`/workspaces/${workspaceId}/knowledge${document.data ? `?kb=${document.data.kb_id}` : ""}`}
        >
          Back to sources
        </Link>
      </header>
      <p className="eyebrow">DOCUMENT / REVISION HISTORY</p>
      <h1>{document.data?.title ?? "Document"}</h1>
      <ProblemBanner
        error={document.error ?? revisions.error}
        retry={() => void document.refetch()}
      />
      {document.isPending && <p role="status">Loading document…</p>}
      {document.data && (
        <>
          <p className="muted">
            {document.data.active_revision_id
              ? "Answers use the published version. A newer upload does not replace it until processing succeeds."
              : "This document becomes searchable after a revision is published."}
          </p>
          {canEdit && (
            <>
              <UploadForm
                key={`${workspaceId}:${documentId}`}
                workspace={workspaceId}
                target={{ document: documentId }}
              />
              <button
                disabled={
                  reprocess.isPending ||
                  !["published", "superseded", "failed", "cancelled"].includes(
                    document.data.status,
                  )
                }
                onClick={() =>
                  reprocess.mutate({
                    version: document.data.version,
                    key: crypto.randomUUID(),
                  })
                }
              >
                {reprocess.isPending ? "Requesting…" : "Reprocess current file"}
              </button>
              <ProblemBanner error={reprocess.error} />
              {reprocess.isSuccess && (
                <p role="status">
                  Revision request accepted. Current status:{" "}
                  {reprocess.data.document.status}.
                </p>
              )}
            </>
          )}
          {job && (
            <JobStatus workspace={workspaceId} job={job} canRetry={!!canEdit} />
          )}
          <h2>Revisions</h2>
          <ol className="revision-timeline">
            {revisions.data?.pages
              .flatMap((page) => page.items)
              .map((revision) => (
                <li key={revision.id}>
                  <div className="section-title">
                    <h3>Revision {revision.revision_no}</h3>
                    <span className="status-badge">
                      {revision.status.replaceAll("_", " ")}
                    </span>
                  </div>
                  {revision.id === document.data.active_revision_id && (
                    <span className="published-label">Used for answers</span>
                  )}
                  <p className="muted">
                    {new Date(revision.created_at).toLocaleString()} ·{" "}
                    {revision.chunk_count} searchable passages
                  </p>
                  {revision.warnings.length > 0 && (
                    <div className="limitations">
                      <strong>Processing notes</strong>
                      {revision.warnings.map((warning, i) => (
                        <p key={i}>{warning}</p>
                      ))}
                    </div>
                  )}
                  <details>
                    <summary>Revision identifiers</summary>
                    <dl>
                      <dt>Revision</dt>
                      <dd>{revision.id}</dd>
                      <dt>Content checksum</dt>
                      <dd>{revision.content_sha256}</dd>
                    </dl>
                  </details>
                </li>
              ))}
          </ol>
          {revisions.hasNextPage && (
            <button
              onClick={() => void revisions.fetchNextPage()}
              disabled={revisions.isFetchingNextPage}
            >
              Load earlier revisions
            </button>
          )}
        </>
      )}
    </main>
  );
}
