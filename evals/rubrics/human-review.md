# Human review rubric v1

Review with question, permitted evidence and answer; hide provider/variant. Split answer into atomic factual claims before scoring. For each claim record supported/contradicted/not-established, source span IDs and citation link validity. A plausible statement unsupported by supplied evidence is not established.

Relevance0: wrong issue or unusable;1: partially addresses issue with significant omission;2: directly answers requested issue with no material unsupported addition. Completeness0: misses core gold claims;1: some requested claims;2: all material claims within available evidence. Conflict handling2 requires representing both sources and uncertainty;0 silently chooses unsupported authority. Abstention correct only when requested conclusion lacks supporting permitted evidence; source-free generic advice cannot count as grounded answer.

Citation link correct requires accessible exact version and semantic support for the linked claim. Merely matching vocabulary is insufficient. Quotes must match normalized source text. Scope leak is a critical failure even if answer otherwise accurate. Record uncertainty and adjudicate; do not force uncertain labels into agreement. No score may be invented for timed-out/invalid output: record error and include in task failure denominator.

Review sheet fields: case_id, candidate_blind_id, reviewer_id, claim_id, support_label, gold_span_ids, citation_correct, relevance0..2, completeness0..2, abstention_correct|null, security_failure, notes. Save adjudication reason and source link for disagreements. Re-review20% sample before freezing labels.
