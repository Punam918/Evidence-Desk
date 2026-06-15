import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "../../lib/api";
import { useSession } from "../../app/session";
import { AnswerView } from "../../components/AnswerView";
import { ProblemBanner } from "../../components/ProblemBanner";

export default function BriefsPage() {
  const { workspaceId = "" } = useParams();
  const { session } = useSession();
  const cache = useQueryClient();
  const key = [session.user.id, workspaceId, "briefs"];
  const briefs = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ signal, pageParam }) =>
      api.briefs(workspaceId, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
    refetchInterval: 5000,
    refetchOnWindowFocus: "always",
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBrief(workspaceId, id),
    onSuccess: () => cache.invalidateQueries({ queryKey: key }),
  });
  const items = briefs.error
    ? []
    : (briefs.data?.pages.flatMap((p) => p.items) ?? []);
  return (
    <main className="knowledge-page">
      <header className="knowledge-header">
        <Link to={`/workspaces/${workspaceId}`}>Back to conversations</Link>
      </header>
      <p className="eyebrow">PRIVATE / SAVED ANSWERS</p>
      <h1>Your briefs</h1>
      <p>
        Saved answers retain their original citations. Removed sources make
        their content unavailable.
      </p>
      <ProblemBanner
        error={briefs.error ?? remove.error}
        retry={() => void briefs.refetch()}
      />
      {briefs.isPending && <p role="status">Loading briefs…</p>}
      {!briefs.isPending && !briefs.error && !items.length && (
        <p>No saved briefs yet. Open a completed answer to propose one.</p>
      )}
      {items.map((b) => (
        <article className="saved-brief" key={b.id}>
          <h2>{b.title}</h2>
          {b.answer && b.source_status === "available" ? (
            <AnswerView
              workspaceId={workspaceId}
              answer={b.answer}
              citations={b.citations}
            />
          ) : (
            <p>This brief is unavailable because its sources were removed.</p>
          )}
          <button
            disabled={remove.isPending}
            onClick={() => remove.mutate(b.id)}
          >
            Delete brief
          </button>
        </article>
      ))}
      {briefs.hasNextPage && (
        <button
          disabled={briefs.isFetchingNextPage}
          onClick={() => void briefs.fetchNextPage()}
        >
          More briefs
        </button>
      )}
    </main>
  );
}
