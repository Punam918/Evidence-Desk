from uuid import UUID

from evidencedesk.retrieval.fusion import fuse
from evidencedesk.retrieval.models import (
    Candidate,
    CandidateLists,
    Passage,
    ResolvedScope,
    RevisionScope,
)


def test_joint_branch_support_and_uuid_ties_are_deterministic():
    workspace, actor, kb, doc, rev = [UUID(int=i) for i in range(1, 6)]
    refs = (RevisionScope(rev, doc, kb, "a" * 64, 4),)
    scope = ResolvedScope(workspace, actor, (kb,), refs, "fixture", "fixture", 1, 1, "ask")
    ids = [UUID(int=i) for i in range(10, 14)]
    passages = {
        uid: Passage(
            uid, doc, rev, kb, "Title", "evidence", "b" * 64, 1, i, None, None, 0, 8, (), {}
        )
        for i, uid in enumerate(ids)
    }
    a, b, c, d = ids
    dense = (
        Candidate(a, "dense", 1, 0.9, {}),
        Candidate(b, "dense", 2, 0.8, {}),
        Candidate(d, "dense", 3, 0.5, {}),
    )
    sparse = (
        Candidate(c, "sparse", 1, 8.0, {}),
        Candidate(b, "sparse", 2, 5.0, {}),
        Candidate(d, "sparse", 3, 4.0, {}),
    )
    result = fuse(CandidateLists(dense, sparse, (), {}), passages, scope)
    assert [e.passage.chunk_id for e in result] == [b, d, a, c]
    assert result[0].dense_rank == 2 and result[0].sparse_rank == 2
    assert result[2].fused_score == result[3].fused_score
