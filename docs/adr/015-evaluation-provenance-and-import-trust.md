# ADR015: Explicit evaluation provenance and import trust

Accepted implementation refinement, 2026-10-12. Implements T25's existing sanitized aggregate import; does not broaden benchmark acceptance.

The local implementation is not in a Git repository. Requiring a commit would encourage fabricated provenance. New imports require a SHA256 source snapshot manifest hash and accept a nullable real Git commit. The offline snapshot includes sorted relative source paths and hashes, never secret values. Git identity alone does not establish a clean tree.

Each import declares `draft_diagnostic`, `reviewed_benchmark` or `operational`. These are administrator assertions; storing a report does not certify labels, execute an experiment or promote a model. UI labels must preserve that distinction and show denominators, missing measurements and uncertainty. No raw cases, prompts, answers, artifact URLs or remote fetching are accepted. Admins remain responsible for sanitizing bounded textual hardware/version labels; syntactic validation cannot prove absence of sensitive content.

Existing rows become `legacy_unclassified` with unknown source snapshot, retaining historical records without guessing provenance. New API imports cannot use that legacy scope. Admin/owner access is required for reads and writes, matching the existing OpenAPI authorization contract. Reports are immutable, workspace-scoped, imported idempotently and audited. Migration adds no privilege bypass.
