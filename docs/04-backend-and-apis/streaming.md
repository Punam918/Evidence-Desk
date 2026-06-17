# SSE protocol v1

1. POST run returns202 with run ID/events URL. API transaction persists `run.accepted` seq1.
2. Client authenticated `fetch(events_url,{credentials:'same-origin',headers:{'Last-Event-ID':lastSeq}})` streams UTF-8 and parses full SSE frames; native EventSource cannot set custom headers, so use a tested fetch reader.
3. Each application frame includes `id: <seq>`, `event: <type>`, and one JSON `data:` line conforming to [event schema](../../contracts/events/run-event.schema.json). Sequence increases by one per run. Commit before publish. Network delivery is at least once; deduplicate IDs.
4. Heartbeat comment `: ping` every15s has no ID and is not persisted. Caddy buffering disabled, Cache-Control:no-store, proxy idle timeout75s. Server closes reader at65s if no terminal and client reconnects.
5. Terminal types: answer.completed, run.failed, run.cancelled, run.refused. Exactly one. Normal order accepted → phase(s) → draft deltas → completed; no deltas required for abstention. Brief proposal/approval occurs after completed answer through REST polling; it never appends to a terminal run event log.

`answer.delta` contains decoded partial sentence text, sentence_index and draft=true. Client accumulates per-sentence buffers in sequence. No tool arguments or provider JSON fragments displayed. Completed answer replaces all draft buffers and activates source links. `run.failed` with discard_draft=true clears text; otherwise preserve labeled incomplete draft. Status interrupted maps to failed event code `provider_interrupted` or `worker_lost`; retry button starts a new run. No automatic resend of user text after an HTTP retry.

Reconnect: missing Last-Event-ID=0; parse nonnegative integer, <=last committed seq. Future ID409, malformed409. Replay seq>last then follow notifications/poll500ms. Events retained24h; after expiry410 with instruction GET run/messages. SSE endpoint authorizes current membership, conversation ownership and source tombstones before any replay, then every5s/batch; revoked session closes stream and next reconnect gets401/404. Deleted source prevents replay of content-bearing events and terminalizes active run with discard_draft. Completed old run with revoked source is served only as redacted message via GET, SSE returns410 `source_unavailable`.

Cancel POST is idempotent; sets cancel_requested and returns202 state. Worker polls between stages and on chunks, aborts provider request and emits cancelled. Race finalization vs cancel resolves under run lock: first committed terminal wins; cancel of terminal is no-op returning terminal state. Browser tab close does not cancel; clear Stop button does. Budget charges include attempted generation until provider cancellation takes effect.

Client tests: frames split at every byte, multibyte UTF-8 split, CRLF/LF, duplicated events, empty comments, malformed payload, reconnect before first event, gap detection (reconnect from last good), terminal replay, logout during stream, deleted citation and two tabs. Contract examples must validate against event schema, not only look plausible.

Revocation linearization: new read/replay authorization after tombstone commit must fail. An already authorized batch can be in the server/network buffer at deletion acknowledgement; it cannot be recalled. Recheck before every batch and at most5s during idle streams, cap draft batches256bytes or100ms. Security tests distinguish denied new reads from this bounded in-flight exposure instead of claiming instantaneous network retraction.

## v2 graph transport boundary

LangGraph runtime streams/checkpoint/debug events are internal. Translate only authorized application phases/deltas into the existing committed run_events schema; never proxy graph values, tool arguments, prompts or hidden reasoning. A brief interrupt is a separate REST ToolExecution lifecycle after terminal answer, so no pending-approval event extends a completed SSE log. Reconnect reads PostgreSQL events only and never invokes/resumes a graph.
