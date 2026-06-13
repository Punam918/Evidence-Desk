"""Read-only Qdrant transport. Every request has a PostgreSQL-resolved tenant/revision scope."""

import asyncio
import math
import time
from dataclasses import replace
from uuid import UUID

from evidencedesk.db.session import AuthContext
from evidencedesk.embeddings.models import SparseVector
from evidencedesk.errors import Problem
from evidencedesk.observability.stages import instrument
from evidencedesk.retrieval.models import Branch, Candidate, CandidateLists, ResolvedScope
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

INDEX_ERRORS = (
    ResponseHandlingException,
    UnexpectedResponse,
    ResourceExhaustedResponse,
    TimeoutError,
    OSError,
)
PAYLOAD = (
    "workspace_id",
    "kb_id",
    "document_id",
    "revision_id",
    "profile_id",
    "text_sha256",
    "manifest_sha256",
)


class SearchIndex:
    def __init__(self, connection: AsyncQdrantClient):
        self.client = connection

    @staticmethod
    def filter(ctx: AuthContext, scope: ResolvedScope) -> models.Filter:
        if (
            ctx.workspace_id != scope.workspace_id
            or ctx.user_id != scope.user_id
            or not 1 <= len(scope.revisions) <= 4096
        ):
            raise Problem(404, "not_found", "Search scope is unavailable.")
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="workspace_id", match=models.MatchValue(value=str(ctx.workspace_id))
                ),
                models.FieldCondition(
                    key="kb_id", match=models.MatchAny(any=[str(k) for k in scope.kb_ids])
                ),
                models.FieldCondition(
                    key="revision_id",
                    match=models.MatchAny(any=[str(r.revision_id) for r in scope.revisions]),
                ),
                models.FieldCondition(
                    key="profile_id", match=models.MatchValue(value=scope.profile_id)
                ),
            ]
        )

    @instrument("retrieval.query")
    async def query(
        self,
        ctx: AuthContext,
        scope: ResolvedScope,
        dense: list[float] | None,
        sparse: SparseVector | None,
        *,
        exact: bool = True,
    ) -> CandidateLists:
        condition = self.filter(ctx, scope)
        if dense is not None and (
            len(dense) != 384
            or not all(math.isfinite(v) for v in dense)
            or abs(math.sqrt(sum(v * v for v in dense)) - 1) > 1e-3
        ):
            raise Problem(422, "invalid_vectors", "Dense query vector is invalid.")
        if dense is None and sparse is None:
            raise Problem(
                503, "retrieval_unavailable", "Both search encoders are unavailable.", True
            )
        # A historical verification does not establish present availability. Count the authorized
        # point set and required named vectors before serving; never call an incomplete corpus empty.
        complete = condition.model_copy(deep=True)
        assert isinstance(complete.must, list)
        complete.must.extend(
            [
                models.HasVectorCondition(has_vector="dense"),
                models.HasVectorCondition(has_vector="bm25"),
            ]
        )
        try:
            count = await self.client.count(
                scope.collection, count_filter=complete, exact=True, timeout=3
            )
            total = await self.client.count(
                scope.collection, count_filter=condition, exact=True, timeout=3
            )
        except INDEX_ERRORS as exc:
            raise Problem(
                503, "index_unavailable", "The selected index is unavailable.", True
            ) from exc
        expected = sum(r.expected_points for r in scope.revisions)
        if count.count != expected or total.count != expected:
            raise Problem(
                503, "index_unavailable", "The selected index is incomplete or inconsistent.", True
            )
        scopes = (
            [replace(scope, revisions=(r,)) for r in scope.revisions]
            if scope.mode == "compare"
            else [scope]
        )
        requests: list[models.QueryRequest] = []
        labels: list[tuple[Branch, int]] = []
        limit = 20 if scope.mode == "compare" else 40
        for group, subset in enumerate(scopes):
            for branch, vector in [
                ("dense", dense),
                (
                    "sparse",
                    models.SparseVector(indices=sparse.indices, values=sparse.values)
                    if sparse is not None and sparse.indices
                    else None,
                ),
            ]:
                if vector is None:
                    continue
                requests.append(
                    models.QueryRequest(
                        query=vector,
                        using="dense" if branch == "dense" else "bm25",
                        filter=self.filter(ctx, subset),
                        params=models.SearchParams(exact=exact),
                        limit=limit,
                        with_payload=models.PayloadSelectorInclude(include=list(PAYLOAD)),
                        with_vector=False,
                    )
                )
                labels.append(("dense" if branch == "dense" else "sparse", group))
        if not requests:  # Empty BM25 for an all-stopword query is valid, not an encoder outage.
            return CandidateLists((), (), (), {})
        start = time.perf_counter()
        degraded = []
        results: list[models.QueryResponse | BaseException]
        try:
            results = list(
                await self.client.query_batch_points(scope.collection, requests, timeout=3)
            )
            if len(results) != len(requests):
                raise Problem(
                    503,
                    "retrieval_unavailable",
                    "The index returned an invalid response batch.",
                    True,
                )
        except INDEX_ERRORS:
            # One batch transport error gives no per-branch outcome. Rescue each independently once,
            # preserving the exact same filters and the shared caller deadline.
            results = list(
                await asyncio.gather(
                    *[
                        self.client.query_points(
                            scope.collection,
                            query=r.query,
                            using=r.using,
                            query_filter=r.filter,
                            search_params=r.params,
                            limit=r.limit or limit,
                            with_payload=r.with_payload or False,
                            with_vectors=False,
                            timeout=3,
                        )
                        for r in requests
                    ],
                    return_exceptions=True,
                )
            )
            degraded.append("batch_transport_failed")
        found: dict[Branch, list[Candidate]] = {"dense": [], "sparse": []}
        successful = set(range(len(scopes))) if sparse is not None and not sparse.indices else set()
        for (branch, group), result in zip(labels, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if not isinstance(result, INDEX_ERRORS):
                    raise result
                degraded.append(branch + "_unavailable")
                continue
            successful.add(group)
            if len(result.points) > limit:
                raise Problem(
                    503, "retrieval_unavailable", "Index candidate limits were exceeded.", True
                )
            seen = set()
            for rank, point in enumerate(result.points, 1):
                try:
                    uid = UUID(str(point.id))
                except ValueError:
                    degraded.append("invalid_candidate")
                    continue
                if uid in seen or not math.isfinite(point.score) or point.payload is None:
                    degraded.append("invalid_candidate")
                    continue
                seen.add(uid)
                found[branch].append(Candidate(uid, branch, rank, point.score, point.payload))
        if len(successful) != len(scopes):
            raise Problem(
                503,
                "retrieval_unavailable",
                "Search is unavailable for a selected source scope.",
                True,
            )
        return CandidateLists(
            tuple(found["dense"]),
            tuple(found["sparse"]),
            tuple(sorted(set(degraded))),
            {"index": (time.perf_counter() - start) * 1000},
        )
