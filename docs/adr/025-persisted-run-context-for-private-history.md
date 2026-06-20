# ADR025 — Persisted run context for private history

Status: accepted for implementation,2026-10-21.

Saved assistant messages outlive the browser tab that started their run. The UI therefore needs a safe way to reopen an older answer for its terminal status, comparison context, and private brief proposal. Client-side remembered state or guessed IDs would be unreliable and can cross a private-conversation boundary.

The existing `runs.assistant_message_id` relationship is exposed as nullable `Message.run_id` after the same creator, workspace, conversation, and source-liveness checks used for message history. `GET /runs/{id}` remains creator-scoped and returns the persisted `intent`. For `compare` runs it also returns an ordered two-item `comparison` view reconstructed from the stored revision IDs and current PostgreSQL authorization/liveness state. It never returns execution metadata, prompt content, or persisted document names. A removed/redacted side is explicitly unavailable with its display metadata cleared.

This is a backward-compatible response expansion and needs no migration: run intent and requested revision IDs already exist in the durable run record. It enables the frontend to recover older answers and comparisons through the API without weakening source redaction, creator-private chat ownership, or brief approval payload binding.
