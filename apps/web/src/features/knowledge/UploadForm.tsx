import { useEffect, useRef, useState, useId, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type Schema } from "../../lib/api";
import { useSession } from "../../app/session";
import { JobStatus } from "./JobStatus";
import { ProblemBanner } from "../../components/ProblemBanner";

export function UploadForm({
  workspace,
  target,
}: {
  workspace: string;
  target: { kb: string } | { document: string };
}) {
  const { session } = useSession();
  const cache = useQueryClient();
  const id = useId();
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState<Schema["UploadAccepted"] | null>(
    null,
  );
  const key = useRef(crypto.randomUUID());
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || pending) return;
    if (file.size < 1 || file.size > 20 * 1024 * 1024) {
      setError(new Error("Choose a nonempty file of at most 20 MiB."));
      return;
    }
    setError(null);
    setPending(true);
    setAccepted(null);
    controller.current = new AbortController();
    try {
      const result = await api.upload(
        workspace,
        target,
        file,
        key.current,
        controller.current.signal,
      );
      if (!mounted.current) return;
      setAccepted(result);
      await cache.invalidateQueries({
        queryKey: [session.user.id, workspace],
        predicate: (q) => q.queryKey[2] !== "job",
      });
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      if (mounted.current) setPending(false);
    }
  }
  return (
    <form
      className="upload-form"
      onSubmit={submit}
      aria-label={
        "document" in target ? "Upload new revision" : "Upload document"
      }
    >
      <label htmlFor={id}>
        {"document" in target ? "New revision file" : "Document file"}
      </label>
      <input
        id={id}
        type="file"
        accept=".pdf,.docx,.txt,.md,.html,.htm,.csv,.json"
        disabled={pending}
        onChange={(event) => {
          setFile(event.target.files?.[0] ?? null);
          key.current = crypto.randomUUID();
          setError(null);
          setAccepted(null);
        }}
        aria-describedby={`${id}-help`}
      />
      <p id={`${id}-help`} className="muted small">
        PDF, DOCX, text, Markdown, HTML, CSV or JSON · up to 20 MiB. Scanned
        PDFs need text extraction first.
      </p>
      <button className="primary" disabled={!file || pending || !!accepted}>
        {pending
          ? "Uploading…"
          : "document" in target
            ? "Upload new revision"
            : "Upload document"}
      </button>
      <ProblemBanner error={error} />
      {accepted && (
        <p role="status">
          File received. Revision status: {accepted.document.status}.{" "}
          <Link
            to={`/workspaces/${workspace}/documents/${accepted.document.id}?job=${accepted.job.job_id}`}
          >
            View revisions
          </Link>
        </p>
      )}
      {accepted && (
        <JobStatus workspace={workspace} job={accepted.job.job_id} canRetry />
      )}
    </form>
  );
}
