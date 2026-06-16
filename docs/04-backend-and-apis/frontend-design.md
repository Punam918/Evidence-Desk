# Frontend and interaction specification

Final UI refinement is a required deliverable per the user's2026-10-19 request. After core behavior stabilizes, visually review and refine the actual product across all primary screens and responsive sizes; preserve accessibility and clear workflow states. Final showcase screenshots/videos must use the integrated running app. See [final-delivery acceptance](../11-implementation-plan/final-delivery.md).

React19/TypeScript/Vite SPA; TanStack Query server cache; route-based code splitting; generated API types. CSS tokens define neutral ink/surface, one blue accent, semantic success/warning/error, 8px spacing rhythm, readable 14–16px body. Prefer an uncluttered workspace with clear source/version/state information. No fake activity counters, benchmark scores or customer logos.

Desktop ≥1024px: workspace/history left rail240px, central chat max800px, optional source inspector360px. Tablet collapses history; mobile360px uses single pane and modal source sheet. Composer remains visible with safe-area padding. All status badges have text and color. Tables adapt to labeled cards without hiding key controls.

| Screen / components | API/state | Loading, empty and failure behavior | Acceptance |
|---|---|---|---|
| Login / SessionGate | auth redirect, GET me | Login expired clears all tenant caches; IdP outage offers retry with request ID | Keyboard login flow, no tokens in localStorage |
| WorkspaceSwitcher / MemberTable | workspaces and members | Empty→create; last-owner409 explains requirement; no global directory | Switching cancels readers and clears former workspace data |
| KnowledgeList / UploadDropzone | KB/docs + multipart; jobs poll2s then5s | Upload MIME/size preview; queued/parsing/etc text; failed reason/reprocess action for editors | Keyboard file picker, progress announcements throttled |
| DocumentDetail / RevisionTimeline | revisions/download | Published version vs pending replacement explicit; scan-only says OCR unavailable | Old active source remains usable during reprocess |
| Chat / Composer / ModelPicker | conversations/messages/runs/models | Disabled unavailable models with reason; empty KB suggestion; Stop button; failed run retry explicitly creates new run | No draft shown as final; local-only policy visible |
| Answer / CitationButton / SourceDrawer | final answer + chunk/download | Draft label while streaming; final citations numbered; deleted source unavailable | Exact revision/page/heading, safe text rendering |
| ComparePanel / BriefApproval | intent compare, two revisions, proposal/approval | Show side labels, proposed full answer and private destination; expiry/revocation state | Confirmed content hash, no invisible side effects |
| HistoryList / ConversationDelete | cursor pagination | Empty state, undo unavailable after confirmed delete, pending purge status | Creator-only list, keyboard controls |
| FeedbackControls | PUT feedback | Optimistic vote with rollback, optional reason, no implication of training | Repeat edits stable |
| UsageDashboard | usage date filters | API and estimated costs separate from fixed/GPU estimates; zero data vs unavailable distinct | Correct units/date timezone, no misleading totals |
| EvaluationDashboard | report list/detail | Dataset/config/model/hardware/denominators visible; no report means “Not measured” | Four-variant comparison with intervals, failures visible |
| Settings / HealthPanel | settings versioned patch + health | Dirty-state confirmation,409 reload-and-review, masked secrets never served | Admin only; privacy/budget changes explained |

State boundaries: URL holds workspace/conversation and filters; Query key includes workspace+user+resource; transient stream reducer keyed by run; persisted final message invalidates message query. Do not duplicate messages in a mutable global store. On logout clear all caches and stream readers; on workspace switch keep no hidden prior evidence in DOM. Use BroadcastChannel for logout notification across tabs.

Accessibility target WCAG2.2 AA: semantic buttons/labels, focus ring, focus restored after drawer, escape closes dialog, confirm destructive deletion, touch targets≥44px, contrast≥4.5:1 body, reduced motion, status live region polite and debounced (do not announce every token), error summary linked to fields. Manual keyboard/screen reader pass required in addition to axe.

Markdown render allowlist excludes raw HTML, remote images, iframe and JavaScript/data URLs; citation links are generated from server IDs only. External links require safe HTTPS scheme, rel=noopener noreferrer, clear external indicator. Document preview initially extracted text; original file only attachment download, never embed uploaded HTML on application origin.

Frontend artifacts to implement: generated client, SSE reader with byte-boundary tests, StreamReducer, shared ProblemBanner, role-aware action wrappers (UX only; backend enforces), fixtures for all states, Playwright upload→answer→source→feedback and compare→approve stories. Visual review at360/768/1280px; capture real screenshots after implementation.

## Durable approval display

ToolExecution may be approved or running after HTTP200. Poll its authorized GET until succeeded+brief_id, failed, rejected or expired; show pending work with retry-safe status refresh. Do not optimistically show a saved brief or restart the answer SSE stream. Hide LangGraph thread/checkpoint identifiers and Qdrant internals from product flows. Poll/reconnect uses current session and stops on logout or source revocation.

## Implemented evidence inspector (T18)

The source dialog loads the exact authorized chunk on every opening, keyed by user/workspace/chunk, and refreshes every five seconds while open. It displays plain text, page range, heading path and the immutable revision ID; superseded evidence is not silently replaced by the active revision. No uploaded HTML or remote content is rendered. Source failures hide previously cached passage text; the dialog offers a fresh access check. Query cache entries are discarded after the inspector unmounts. Escape returns focus to the citation button.

Downloads name the cited revision explicitly and use the same-origin authorized attachment proxy. A failed/missing original leaves the extracted passage available with a separate download error. Download requests abort when the inspector closes or the workspace changes. Already received evidence cannot be recalled; the five-second UI refresh is an availability refresh, not an authorization boundary.
