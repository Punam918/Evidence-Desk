import { useId, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type Schema } from "../../lib/api";
import { ProblemBanner } from "../../components/ProblemBanner";

export function FeedbackControls({
  workspaceId,
  messageId,
}: {
  workspaceId: string;
  messageId: string;
}) {
  const label = useId();
  const [vote, setVote] = useState<-1 | 1 | null>(null);
  const [reason, setReason] = useState("");
  const [saved, setSaved] = useState<Schema["Feedback"] | null>(null);
  const mutation = useMutation({
    mutationFn: (value: Schema["FeedbackInput"]) =>
      api.feedback(workspaceId, messageId, value),
    onMutate: () => {
      const previous = saved;
      return previous;
    },
    onSuccess: (value) => {
      setSaved(value);
      setVote(value.vote);
    },
    onError: (_error, _value, previous) => {
      setVote(previous?.vote ?? null);
    },
  });
  function submit(value: -1 | 1) {
    setVote(value);
    mutation.mutate({ vote: value, reason });
  }
  return (
    <div className="feedback-controls">
      <span>Was this answer helpful?</span>
      <div className="feedback-buttons">
        <button
          aria-pressed={vote === 1}
          disabled={mutation.isPending}
          onClick={() => submit(1)}
        >
          Helpful
        </button>
        <button
          aria-pressed={vote === -1}
          disabled={mutation.isPending}
          onClick={() => submit(-1)}
        >
          Not helpful
        </button>
      </div>
      <details>
        <summary>Add a reason</summary>
        <label htmlFor={label}>Feedback reason (optional)</label>
        <textarea
          id={label}
          maxLength={1000}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          disabled={mutation.isPending}
        />
        <button
          disabled={vote === null || mutation.isPending}
          onClick={() => vote !== null && submit(vote)}
        >
          Save feedback
        </button>
        <p className="muted">
          Saved privately with this answer. Feedback is not automatically used
          for training.
        </p>
      </details>
      <p role="status">
        {mutation.isPending
          ? "Saving feedback…"
          : mutation.isSuccess
            ? "Feedback saved. You can change it."
            : ""}
      </p>
      <ProblemBanner error={mutation.error} />
    </div>
  );
}
