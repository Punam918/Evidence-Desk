# Live EvidenceDesk media

These artifacts were captured by `scripts/capture_live_media.mjs` from the real local Docker Compose application. The script logs in through Keycloak, uses the passed synthetic integrated workspace, and makes no mocked browser requests. See `manifest.json` for the exact source report, image identities, route provenance, and local-CA browser note.

| Asset | Scenario | Caption |
| --- | --- | --- |
| `01-live-evidence-walkthrough.webm` | Live navigation | Browser-recorded authenticated walkthrough of answer, citation, responsive, and abstention states. |
| `01-workspace-home.png` | Workspace hub | Authenticated organization workspace selection. |
| `02-knowledge-base.png` | Source collection | Published knowledge base and document upload surface. |
| `03-revision-history.png` | Publication | The revision currently used by answers. |
| `04-grounded-answer.png` | Answer | Local cited answer from the published Harbor policy. |
| `05-citation-inspection.png` | Evidence | Exact source passage and revision metadata. |
| `06-brief-approval.png` | Controlled write | Proposal/approval surface for a private brief. |
| `07-private-brief.png` | Persistence | Approved private brief retaining its citation. |
| `08-responsive-answer.png` | Responsive UI | Answer workflow in a 390px viewport. |
| `09-evidence-abstention.png` | Failure behavior | Empty-knowledge evidence limitation. |

The source documents and organization names are fictional. This capture validates application behavior; it does not claim a production deployment or answer-quality benchmark.
