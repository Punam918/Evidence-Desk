import { useRef, useState } from "react";
import { Link, useParams } from "react-router";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";
import { api, type Schema } from "../../lib/api";

const scopes: Record<Schema["Evaluation"]["evidence_scope"], string> = {
  draft_diagnostic: "Draft diagnostic — not benchmark evidence",
  reviewed_benchmark: "Reviewed benchmark — administrator supplied",
  operational: "Operational measurement — administrator supplied",
  legacy_unclassified: "Legacy report — evidence scope unknown",
};
const number = (value: number | null) =>
  value === null
    ? "Not measured"
    : new Intl.NumberFormat("en-US", { maximumSignificantDigits: 5 }).format(
        value,
      );

export default function EvaluationsPage() {
  const { workspaceId = "" } = useParams();
  return <Evaluations key={workspaceId} workspaceId={workspaceId} />;
}

function Evaluations({ workspaceId }: { workspaceId: string }) {
  const { session } = useSession();
  const cache = useQueryClient();
  const key = [session.user.id, workspaceId, "evaluations"];
  const [file, setFile] = useState<{
    name: string;
    body: unknown;
    key: string;
  } | null>(null);
  const [invalid, setInvalid] = useState("");
  const reading = useRef(0);
  const reports = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ signal, pageParam }) =>
      api.evaluations(workspaceId, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
    refetchInterval: 5000,
    refetchOnWindowFocus: "always",
  });
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a report first.");
      return api.importEvaluation(workspaceId, file.body, file.key);
    },
    onSuccess: () => {
      setFile(null);
      return cache.invalidateQueries({ queryKey: key });
    },
  });
  async function choose(selected?: File) {
    const sequence = ++reading.current;
    setFile(null);
    setInvalid("");
    upload.reset();
    if (!selected) return;
    if (selected.size > 131072) {
      setInvalid("Choose an aggregate JSON report smaller than 128 KiB.");
      return;
    }
    try {
      const body: unknown = JSON.parse(await selected.text());
      if (sequence !== reading.current) return;
      if (!body || typeof body !== "object" || Array.isArray(body))
        throw new Error("Invalid object");
      setFile({ name: selected.name, body, key: crypto.randomUUID() });
    } catch {
      if (sequence === reading.current)
        setInvalid("This file is not a JSON report object.");
    }
  }
  const items = reports.error
    ? []
    : (reports.data?.pages.flatMap((p) => p.items) ?? []);
  return (
    <main className="knowledge-page evaluation-page">
      <header className="knowledge-header">
        <Link to={`/workspaces/${workspaceId}`}>Back to conversations</Link>
      </header>
      <p className="eyebrow">ADMIN / EVALUATION REPORTS</p>
      <h1>Evaluation evidence</h1>
      <p>
        Inspect recorded measurements, sample counts and uncertainty. Imported
        reports are administrator supplied; importing a report does not validate
        its labels or promote a model.
      </p>
      <ProblemBanner
        error={reports.error ?? upload.error}
        retry={() => void reports.refetch()}
      />
      {!reports.error && !reports.isPending && (
        <section className="saved-brief" aria-labelledby="import-title">
          <h2 id="import-title">Import an aggregate report</h2>
          <p>
            Use the evaluation report JSON format. Include only sanitized
            aggregate measurements and public version or hardware labels. Keep
            case records, prompts, answers and secrets in your private
            evaluation artifacts.
          </p>
          <label>
            Report file
            <input
              type="file"
              accept=".json,application/json"
              disabled={upload.isPending}
              onChange={(e) => void choose(e.target.files?.[0])}
            />
          </label>
          {file && <p>Ready to import: {file.name}</p>}
          {invalid && <p role="alert">{invalid}</p>}
          <button
            disabled={!file || upload.isPending}
            onClick={() => upload.mutate()}
          >
            {upload.isPending ? "Importing…" : "Import report"}
          </button>
          {upload.isSuccess && <p role="status">Report imported.</p>}
        </section>
      )}
      {reports.isPending && <p role="status">Loading reports…</p>}
      {!reports.isPending && !reports.error && !items.length && (
        <p>No evaluation reports imported. Quality is not measured yet.</p>
      )}
      {items.map((r) => (
        <article className="saved-brief" key={r.id}>
          <p className="evaluation-scope">{scopes[r.evidence_scope]}</p>
          <h2>
            {r.dataset_version} · {r.variant}
          </h2>
          <p>
            {r.model_key} · {r.status} · {r.case_count} declared cases
          </p>
          <div
            className="evaluation-table"
            role="region"
            aria-label={`${r.variant} measurements`}
            tabIndex={0}
          >
            <table>
              <caption>
                Recorded metrics and supplied confidence intervals
              </caption>
              <thead>
                <tr>
                  <th scope="col">Metric</th>
                  <th scope="col">Value</th>
                  <th scope="col">n</th>
                  <th scope="col">Interval</th>
                </tr>
              </thead>
              <tbody>
                {r.metrics.map((m) => (
                  <tr key={m.name}>
                    <th scope="row">{m.name}</th>
                    <td>{number(m.value)}</td>
                    <td>{m.n}</td>
                    <td>
                      {m.ci_low === null || m.ci_high === null
                        ? "Not supplied"
                        : `${number(m.ci_low)} to ${number(m.ci_high)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!r.metrics.length && <p>No metrics supplied.</p>}
          <details>
            <summary>Provenance</summary>
            <dl className="evaluation-provenance">
              <dt>Hardware</dt>
              <dd>{r.hardware}</dd>
              <dt>Dataset SHA256</dt>
              <dd>{r.dataset_sha256}</dd>
              <dt>Configuration SHA256</dt>
              <dd>{r.config_sha256}</dd>
              <dt>Source snapshot SHA256</dt>
              <dd>{r.source_snapshot_sha256 ?? "Unknown"}</dd>
              <dt>Git commit</dt>
              <dd>{r.git_sha ?? "Not recorded"}</dd>
              <dt>Artifact SHA256</dt>
              <dd>{r.artifact_sha256}</dd>
              <dt>Imported</dt>
              <dd>{new Date(r.created_at).toLocaleString()}</dd>
            </dl>
          </details>
        </article>
      ))}
      {reports.hasNextPage && (
        <button
          disabled={reports.isFetchingNextPage}
          onClick={() => void reports.fetchNextPage()}
        >
          More reports
        </button>
      )}
    </main>
  );
}
