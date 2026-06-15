import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

export function SourceDetails({
  workspaceId,
  source,
}: {
  workspaceId: string;
  source: Schema["Citation"];
}) {
  const { session } = useSession();
  const [downloadError, setDownloadError] = useState<unknown>();
  const [downloading, setDownloading] = useState(false);
  const download = useRef<AbortController | null>(null);
  useEffect(() => () => download.current?.abort(), []);
  const passage = useQuery({
    queryKey: [session.user.id, workspaceId, "chunk", source.chunk_id],
    queryFn: ({ signal }) => api.chunk(workspaceId, source.chunk_id, signal),
    retry: false,
    gcTime: 0,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: 5000,
  });
  const data = passage.data;
  const valid =
    data &&
    data.document_id === source.document_id &&
    data.revision_id === source.revision_id &&
    data.id === source.chunk_id;
  async function saveOriginal() {
    const controller = new AbortController();
    download.current = controller;
    setDownloading(true);
    setDownloadError(undefined);
    try {
      const blob = await api.original(
        workspaceId,
        source.document_id,
        source.revision_id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download =
        source.title.replace(/[\\/\u0000-\u001f\u007f]/g, "_") || "document";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (!controller.signal.aborted) setDownloadError(error);
    } finally {
      if (!controller.signal.aborted) setDownloading(false);
    }
  }
  if (passage.isPending) return <p role="status">Loading source passage…</p>;
  if (passage.error || !valid)
    return (
      <div role="status">
        <p>
          Source unavailable. It may have been removed or your access may have
          changed.
        </p>
        <button onClick={() => void passage.refetch()}>Check again</button>
      </div>
    );
  return (
    <>
      <dl>
        <dt>Evidence</dt>
        <dd>{source.evidence_id}</dd>
        <dt>Exact revision</dt>
        <dd>{data.revision_id}</dd>
        {data.page_start !== null && (
          <>
            <dt>Pages</dt>
            <dd>
              {data.page_start}
              {data.page_end !== null && data.page_end !== data.page_start
                ? `–${data.page_end}`
                : ""}
            </dd>
          </>
        )}
        {data.heading_path.length > 0 && (
          <>
            <dt>Section</dt>
            <dd>{data.heading_path.join(" / ")}</dd>
          </>
        )}
      </dl>
      <h3>Source passage</h3>
      <pre className="source-passage">{data.text}</pre>
      <p>This is the cited revision, even if a newer version exists.</p>
      <button
        className="primary-button"
        disabled={downloading}
        onClick={() => void saveOriginal()}
      >
        {downloading ? "Preparing attachment…" : "Download original revision"}
      </button>
      <ProblemBanner error={downloadError} />
    </>
  );
}
