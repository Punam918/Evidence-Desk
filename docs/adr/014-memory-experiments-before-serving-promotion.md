# ADR014: Keep memory experiments outside serving until paired evaluation

Status: adopted for the local implementation,2026-10-12. Related tasks:T21,T24,T25.

The approved design makes rewrite and summarization optional and requires evaluation before promotion. Implementing a continuously scheduled summary worker now would introduce recurring content egress and spending without evidence that the summaries help. It would also add a separate conversation-scoped attempt lifecycle before its operational need is established.

Implement both as explicit experiments bound to an already admitted, fenced chat run. Reuse its existing private history checks, native adapters, UTC budget ledger and maximum three attempts/$0.05. Summaries can be generated and optimistically stored with provenance; a pure memory composer demonstrates the shared2048-token allowance. Serving continues with recent history only. No worker automatically schedules either experiment or sends summary context in production answers.

This placement permits reproducible scope, failure and accounting tests while keeping promotion separate. Ambiguous auxiliary transport interrupts the run; it never retries or silently starts another uncertain request. This has an availability cost which the paired experiment must include. The prompts' semantic fidelity, usefulness and latency remain unmeasured by contract fixtures.

After T24/T25, promote only if paired quality/security/cost evidence supports it. A production background summary lifecycle, independent conversation reservation, retention scheduling and migration of eligible settings require a subsequent implementation/review; no such worker is claimed here. This decision defers an optional serving feature, not the completed baseline memory path or the implemented experimental modules. See the memory experiment report for reproducible evidence and limitations.
