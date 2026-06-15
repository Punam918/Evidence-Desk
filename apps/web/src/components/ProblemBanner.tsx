import { ApiError } from "../lib/api";
export function ProblemBanner({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  if (!error) return null;
  return (
    <div className="problem" role="alert">
      <strong>Something needs your attention</strong>
      <p>
        {error instanceof Error
          ? error.message
          : "The request could not be completed."}
      </p>
      {error instanceof ApiError && error.requestId && (
        <small>Request {error.requestId}</small>
      )}
      {retry && <button onClick={retry}>Try again</button>}
    </div>
  );
}
