# Draft corpus and review status

2026-10-12. **T24 remains in progress: no human labels have been accepted.** The implementable curation package contains400 draft questions,200 adversarial definitions and50 workflow definitions over120 original synthetic source revisions. Structural checks pass and17 curation tests pass. Draft labels and fixture definitions are not evaluation results.

The100 development questions comprise60 direct,20 unanswerable,10 version comparisons and10 multi-document questions; the300 test questions comprise180/60/30/30. Forty synthetic systems belong to8 engineering archetypes. Split assignment groups the entire archetype, preventing template siblings from crossing splits. This leaves only8 independent clusters and creates domain shift between development and test. Do not treat400 parameterized questions as400 independent examples or claim corpus representativeness. Normalized question overlap is zero; semantic leakage review is still outstanding. True conversational follow-ups and a broader independently authored corpus remain expansion work.

Every source has original-fixture redistribution permission and a SHA256. Gold spans use exact Unicode codepoint offsets, quoted text and source IDs; validation checks their permitted workspace and document family. Both version-sensitive and multi-document cases require two source identities. The manifest records source, case-file and locked pipeline hashes. Generation refuses to overwrite an existing dataset/review directory.

Review package: `evals/review/benchmark-v1-draft/review.html` and `review-sheet.csv`. It contains questions and full permitted sources with draft labels collapsed; no model identities or candidate answers appear. The preselected independent second-review sample contains80 cases, stratified by split/category. Reviewers must check facts, exact spans, completeness, answerability, version handling, scope and leakage against the human-review-v1 rubric.

```bash
# Structural validation; prints zero accepted human labels until reviews exist.
uv run --no-sync python -m evals.curation validate
# Import a completed human review sheet; blank rows do not count.
uv run --no-sync python -m evals.curation import-reviews --sheet /path/to/completed-review.csv
# Refuses missing/stale/disputed reviews and missing independent second review.
uv run --no-sync python -m evals.curation freeze --output evals/reports/benchmark-v1-reviewed-freeze.json
```

Each imported record needs a real reviewer identifier, accept/revise/reject, explicit `human_attestation=true`, timezone-bearing review date, notes and the exact case hash. Imports are locked/atomic and idempotent; conflicting replacement records are rejected. The software validates recorded attestations, not the reviewer's real-world identity or diligence. Test-only reviewer identities exist solely in temporary test directories. No such reviews were added to this package.

An explicit `--single-reviewer-limitation` may waive only the second-review count; it cannot waive the400 primary reviews or unresolved disputes. A disputed/changed label needs correction in a newly versioned dataset and another hash-bound review; this initial importer does not silently overwrite prior judgments. The freeze envelope binds the exact dataset and review-file hashes and preserves the limitation. Human approval is a release/evaluation gate, not a reason to stop implementing independent evaluation tools.
