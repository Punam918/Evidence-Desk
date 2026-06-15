import { useEffect, useRef, useState, useId } from "react";
import { SourceDetails } from "../features/evidence/SourceDetails";
import type { Schema } from "../lib/api";

export function AnswerView({
  answer,
  citations,
  workspaceId,
}: {
  workspaceId: string;
  answer: Schema["Answer"];
  citations: Schema["Citation"][];
}) {
  const [source, setSource] = useState<Schema["Citation"] | null>(null);
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (source) dialog.current?.showModal();
    else dialog.current?.close();
  }, [source]);
  return (
    <div className="answer-content">
      {answer.status === "abstained" && (
        <span className="status-badge warning">
          Not enough supporting evidence
        </span>
      )}
      {answer.sentences.map((sentence, i) => (
        <p key={i}>
          {sentence.text}{" "}
          {sentence.citation_ids.map((id) => {
            const citation = citations.find((item) => item.evidence_id === id);
            return (
              <button
                className="citation"
                key={id}
                disabled={!citation?.available}
                onClick={() => citation && setSource(citation)}
                aria-label={`Open source ${id}${citation?.available ? "" : ", unavailable"}`}
              >
                {id}
              </button>
            );
          })}
        </p>
      ))}
      {answer.limitations.length > 0 && (
        <div className="limitations">
          <strong>Limitations</strong>
          {answer.limitations.map((text, i) => (
            <p key={i}>{text}</p>
          ))}
        </div>
      )}
      {citations.length > 0 && (
        <div className="source-summary">
          <span>
            {citations.length} cited{" "}
            {citations.length === 1 ? "source" : "sources"}
          </span>
          {citations.map((citation) => (
            <button
              className="source-chip"
              key={citation.evidence_id}
              disabled={!citation.available}
              onClick={() => setSource(citation)}
            >
              {citation.evidence_id} · {citation.title}
            </button>
          ))}
        </div>
      )}
      <dialog
        className="source-dialog"
        ref={dialog}
        onCancel={() => setSource(null)}
        onClose={() => setSource(null)}
        aria-labelledby={titleId}
      >
        <div className="dialog-header">
          <p className="eyebrow">SOURCE DETAILS</p>
          <button aria-label="Close source" onClick={() => setSource(null)}>
            ×
          </button>
        </div>
        {source && (
          <>
            <h2 id={titleId}>{source.title}</h2>
            <SourceDetails
              key={source.chunk_id}
              workspaceId={workspaceId}
              source={source}
            />
          </>
        )}
      </dialog>
    </div>
  );
}
