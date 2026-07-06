from uuid import uuid4

from evidencedesk.embeddings.models import SparseVector
from evidencedesk.retrieval.cache import QueryEmbeddingCache


def test_vectors_are_isolated_by_workspace_revision_profile_and_mutation():
    cache = QueryEmbeddingCache()
    workspace = uuid4()
    key = cache.key(workspace, "revision-a", "hybrid", "private question")
    dense = [1.0, 0.0]
    sparse = SparseVector(indices=[1], values=[2.0])
    cache.put(key, dense, sparse)
    dense[0] = 99
    sparse.values[0] = 99
    result = cache.get(key)
    assert result == ([1.0, 0.0], SparseVector(indices=[1], values=[2.0]))
    result[0][0] = 88
    result[1].values[0] = 88
    assert cache.get(key)[0] == [1.0, 0.0]
    assert cache.get(key)[1].values == [2.0]
    for other in (
        cache.key(uuid4(), "revision-a", "hybrid", "private question"),
        cache.key(workspace, "revision-b", "hybrid", "private question"),
        cache.key(workspace, "revision-a", "dense", "private question"),
    ):
        assert cache.get(other) is None
    assert "private question" not in repr(cache.entries)


def test_fixed_expiry_and_lru_capacity_do_not_extend_on_reads():
    now = [0.0]
    cache = QueryEmbeddingCache(capacity=2, clock=lambda: now[0])
    keys = [cache.key(uuid4(), "rev", "dense", "q") for _ in range(3)]
    cache.put(keys[0], [1.0], None)
    now[0] = 100
    cache.put(keys[1], [1.0], None)
    assert cache.get(keys[0]) is not None
    cache.put(keys[2], [1.0], None)
    assert cache.get(keys[1]) is None
    now[0] = 300
    assert cache.get(keys[0]) is None
    assert cache.get(keys[2]) is not None
    now[0] = 400
    assert cache.get(keys[2]) is None
