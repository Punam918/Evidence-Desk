# ADR020: Conversation expiry through the existing purge queue

Accepted implementation refinement,2026-10-12, within T28's90-day conversation retention requirement.

`conversation-expire --workspace UUID` tombstones at most25 conversations per invocation whose creation timestamp is at least90 days old. This is a fixed conversation lifetime measured from creation, not a rolling inactivity window; sending a new message does not extend storage indefinitely. The cutoff uses the database transaction timestamp. Browsing does not affect expiry. Workspace knowledge bases have their own lifecycle and do not expire with a conversation.

The operator command needs only ordinary application database credentials and an explicit workspace scope. It takes the existing admission/workspace locks, selects undeleted rows with row locks, and calls the same repository deletion operation as the private conversation API. Each job retains the original creator identity, even if that creator's membership has since been removed. It creates a tombstone, requests cancellation of active runs, and enqueues the existing conversation purge worker atomically. It does not impersonate a browser session or expose a maintenance endpoint. Deleted workspaces are left to workspace deletion orchestration.

The existing purge worker handles lease invalidation, conservative accounting, checkpoint cleanup, private child deletion and durable receipts. An expiry command response is a count of newly tombstoned conversations, not a receipt of physical erasure. A10-second deadline bounds each transaction. Failures roll back the complete page; repeat invocations ignore already tombstoned rows. `may_have_more` means the page was full and is not an assertion that no locked rows or future expiry work remain.

Fleet enumeration, scheduling and a measured24-hour erasure SLA remain unfinished. A supervisor must drain expiry pages and consume purge jobs, revisit locked rows, and schedule future sweeps. No new database schema or public API fields are introduced. T29 must measure age-selection performance before production-scale claims.
