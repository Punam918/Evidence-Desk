import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useSession } from "../../app/session";
import { ProblemBanner } from "../../components/ProblemBanner";

const usd = (micro: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  }).format(micro / 1_000_000);
export default function UsagePage() {
  const { workspaceId = "" } = useParams();
  return <Usage key={workspaceId} workspaceId={workspaceId} />;
}
function Usage({ workspaceId }: { workspaceId: string }) {
  const { session } = useSession();
  const today = new Date().toISOString().slice(0, 10);
  const [start, setStart] = useState(today.slice(0, 8) + "01");
  const [end, setEnd] = useState(today);
  const [window, setWindow] = useState({ start, end });
  const [invalid, setInvalid] = useState("");
  const usage = useQuery({
    queryKey: [session.user.id, workspaceId, "usage", window],
    queryFn: ({ signal }) =>
      api.usage(workspaceId, window.start, window.end, signal),
  });
  function apply(event: FormEvent) {
    event.preventDefault();
    const days =
      (Date.parse(end + "T00:00:00Z") - Date.parse(start + "T00:00:00Z")) /
      86400000;
    if (!Number.isFinite(days) || days < 0 || days >= 90) {
      setInvalid("Choose an inclusive UTC date range of 1–90 days.");
      return;
    }
    setInvalid("");
    setWindow({ start, end });
  }
  const data = usage.error ? undefined : usage.data;
  const total =
    data?.buckets.reduce((sum, b) => sum + b.cost_micro_usd, 0) ?? 0;
  const estimates =
    data?.buckets.reduce((sum, b) => sum + b.estimated_attempts, 0) ?? 0;
  return (
    <main className="workspace-home usage-page">
      <Link to={`/workspaces/${workspaceId}`}>← Back to chat</Link>
      <p className="eyebrow">USAGE AND COSTS</p>
      <h1>Model usage</h1>
      <p>
        Admins and owners see workspace totals. Other members see only their own
        attempts. No conversations or prompts are shown here.
      </p>
      <form className="usage-filters" onSubmit={apply}>
        <label>
          Start date (UTC)
          <input
            type="date"
            required
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <label>
          End date (UTC)
          <input
            type="date"
            required
            value={end}
            onChange={(e) => setEnd(e.target.value)}
          />
        </label>
        <button className="primary" type="submit">
          Apply dates
        </button>
      </form>
      {invalid && <p role="alert">{invalid}</p>}
      <p className="muted">
        Both dates are included, up to 90 days. Attempts stay assigned to the
        UTC date when their budget was reserved.
      </p>
      <ProblemBanner error={usage.error} retry={() => void usage.refetch()} />
      {usage.isPending && <p role="status">Loading usage…</p>}
      {data && (
        <>
          <div className="usage-totals">
            <div>
              <span>Accounted model charges</span>
              <strong>{usd(total)}</strong>
              <span>
                {estimates > 0
                  ? `Includes ${estimates} estimated attempt${estimates === 1 ? "" : "s"}`
                  : "No estimated charges recorded"}
              </span>
            </div>
            <div>
              <span>Outstanding reservations</span>
              <strong>{usd(data.reserved_micro_usd)}</strong>
              <span>Held budget, not additional settled charges</span>
            </div>
          </div>
          {data.buckets.length === 0 ? (
            <p>No model attempts recorded in this date range.</p>
          ) : (
            <div
              className="usage-table-wrap"
              tabIndex={0}
              role="region"
              aria-label="Usage by day; scroll horizontally for all columns"
            >
              <table>
                <caption>
                  Recorded attempts and model charges by UTC day
                </caption>
                <thead>
                  <tr>
                    {[
                      "UTC day",
                      "Model",
                      "Attempts",
                      "Input tokens",
                      "Output tokens",
                      "Charges (USD)",
                      "Estimated attempts",
                    ].map((h) => (
                      <th key={h} scope="col">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.buckets.map((b) => (
                    <tr key={`${b.day}:${b.model_key}`}>
                      <th scope="row">{b.day}</th>
                      <td>{b.model_key}</td>
                      <td>{b.requests}</td>
                      <td>{b.input_tokens.toLocaleString()}</td>
                      <td>{b.output_tokens.toLocaleString()}</td>
                      <td>{usd(b.cost_micro_usd)}</td>
                      <td>{b.estimated_attempts}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted">
            Attempts include retries and reservations, even when a provider was
            not called. Token totals include recorded counts only; missing
            counts are not evidence of zero use. Estimated charges
            conservatively account for uncertain provider usage.
          </p>
        </>
      )}
      <h2>Infrastructure is separate</h2>
      <p>
        Hosting, storage and local-model compute costs are not measured by this
        ledger. A local model’s zero provider charge does not mean it is free to
        operate.
      </p>
    </main>
  );
}
