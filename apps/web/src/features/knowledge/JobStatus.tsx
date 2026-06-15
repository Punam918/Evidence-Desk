import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

const active = ["queued", "running", "retry_wait"];
const reasons: Record<string, string> = {
  ocr_required:
    "This PDF needs OCR before it can be uploaded as searchable text.",
  partial_coverage:
    "Some pages have too little extractable text. Upload a complete text version.",
  encrypted_pdf: "Upload an unencrypted copy of this PDF.",
  parse_failed:
    "This file could not be extracted. Check the file or upload a new revision.",
  parser_timeout: "Extraction exceeded its time limit. Try a smaller document.",
  parser_resource_failure:
    "Extraction exceeded available resources or the parser failed.",
  storage_unavailable: "Document storage is temporarily unavailable.",
};

export function JobStatus({
  workspace,
  job,
  canRetry,
}: {
  workspace: string;
  job: string;
  canRetry: boolean;
}) {
  const { session } = useSession();
  const cache = useQueryClient();
  const retryKey = useRef(crypto.randomUUID());
  const query = useQuery({
    queryKey: [session.user.id, workspace, "job", job],
    queryFn: ({ signal }) => api.job(workspace, job, signal),
    retry: false,
    refetchInterval: (query) => {
      if (
        query.state.error instanceof ApiError &&
        [401, 403, 404].includes(query.state.error.status)
      )
        return false;
      if (query.state.data && !active.includes(query.state.data.status))
        return false;
      return query.state.dataUpdateCount < 5 ? 2000 : 5000;
    },
  });
  const retry = useMutation({
    mutationFn: () => api.retryJob(workspace, job, retryKey.current),
    onSuccess: async () => {
      retryKey.current = crypto.randomUUID();
      await query.refetch();
    },
  });
  const terminal =
    query.data && !active.includes(query.data.status)
      ? query.data.status
      : null;
  useEffect(() => {
    if (terminal)
      void cache.invalidateQueries({
        queryKey: [session.user.id, workspace],
        predicate: (q) => q.queryKey[2] !== "job",
      });
  }, [terminal, job, cache, session.user.id, workspace]);
  return (
    <section className="job-status" aria-label="Document processing">
      <ProblemBanner error={query.error} retry={() => void query.refetch()} />
      {query.data && (
        <>
          <p aria-live="polite">
            Processing:{" "}
            {query.data.status === "running"
              ? query.data.stage
              : query.data.status.replaceAll("_", " ")}
          </p>
          <progress
            aria-label="Document processing progress"
            value={query.data.progress}
            max={100}
          />
          {query.data.error_code && (
            <p>
              {reasons[query.data.error_code] ??
                "Processing did not complete. Review the source and try again."}
            </p>
          )}
          {canRetry &&
            query.data.kind === "ingest" &&
            query.data.status === "dead" && (
              <button
                type="button"
                disabled={retry.isPending}
                onClick={() => retry.mutate()}
              >
                {retry.isPending ? "Requesting retry…" : "Retry processing"}
              </button>
            )}
          <ProblemBanner error={retry.error} />
        </>
      )}
    </section>
  );
}
