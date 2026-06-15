import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

export type Selection = { kb: string; revision: string };
export function RevisionSelect({
  workspace,
  side,
  disabled,
  onChange,
}: {
  workspace: string;
  side: string;
  disabled: boolean;
  onChange: (selection: Selection | null) => void;
}) {
  const { session } = useSession();
  const [kb, setKb] = useState("");
  const [document, setDocument] = useState("");
  const [revision, setRevision] = useState("");
  const knowledge = useInfiniteQuery({
    queryKey: [session.user.id, workspace, "compare-knowledge"],
    queryFn: ({ signal, pageParam }) =>
      api.knowledge(workspace, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
  });
  const documents = useInfiniteQuery({
    queryKey: [session.user.id, workspace, "documents", kb],
    queryFn: ({ signal, pageParam }) =>
      api.documents(workspace, kb, signal, pageParam),
    enabled: !!kb,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
  });
  const revisions = useInfiniteQuery({
    queryKey: [session.user.id, workspace, "revisions", document],
    queryFn: ({ signal, pageParam }) =>
      api.revisions(workspace, document, signal, pageParam),
    enabled: !!document,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
  });
  return (
    <fieldset disabled={disabled} className="revision-picker">
      <legend>{side} revision</legend>
      <label>
        Knowledge base
        <select
          aria-label={`${side} knowledge base`}
          value={kb}
          onChange={(e) => {
            setKb(e.target.value);
            setDocument("");
            setRevision("");
            onChange(null);
          }}
        >
          <option value="">Choose knowledge base</option>
          {knowledge.data?.pages
            .flatMap((p) => p.items)
            .map((k) => (
              <option key={k.id} value={k.id}>
                {k.name}
              </option>
            ))}
        </select>
      </label>
      {knowledge.hasNextPage && (
        <button type="button" onClick={() => void knowledge.fetchNextPage()}>
          More knowledge bases
        </button>
      )}
      <label>
        Document
        <select
          aria-label={`${side} document`}
          value={document}
          disabled={!kb}
          onChange={(e) => {
            setDocument(e.target.value);
            setRevision("");
            onChange(null);
          }}
        >
          <option value="">Choose document</option>
          {documents.data?.pages
            .flatMap((p) => p.items)
            .map((d) => (
              <option key={d.id} value={d.id}>
                {d.title}
              </option>
            ))}
        </select>
      </label>
      {documents.hasNextPage && (
        <button type="button" onClick={() => void documents.fetchNextPage()}>
          More documents
        </button>
      )}
      <label>
        Version
        <select
          aria-label={`${side} version`}
          value={revision}
          disabled={!document}
          onChange={(e) => {
            setRevision(e.target.value);
            onChange(e.target.value ? { kb, revision: e.target.value } : null);
          }}
        >
          <option value="">Choose published version</option>
          {revisions.data?.pages
            .flatMap((p) => p.items)
            .filter((r) => ["published", "superseded"].includes(r.status))
            .map((r) => (
              <option key={r.id} value={r.id}>
                Version {r.revision_no} · {r.status}
              </option>
            ))}
        </select>
      </label>
      {revisions.hasNextPage && (
        <button type="button" onClick={() => void revisions.fetchNextPage()}>
          More versions
        </button>
      )}
      <ProblemBanner
        error={knowledge.error ?? documents.error ?? revisions.error}
      />
    </fieldset>
  );
}
