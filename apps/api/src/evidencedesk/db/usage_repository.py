"""Integer-micro-USD accounting helpers shared by terminal recovery and provider execution."""

import json
import math
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.billing.models import Attempt
from evidencedesk.chat.models import Answer
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease
from evidencedesk.providers.base import ModelFault, ModelResult, Prices, Usage


async def settle_abandoned(conn: AsyncConnection, ctx: AuthContext, run_id: UUID) -> None:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT * FROM app.model_usage WHERE workspace_id=:w AND run_id=:run AND state='reserved' ORDER BY attempt_no FOR UPDATE"
                ),
                {"w": ctx.workspace_id, "run": run_id},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        charge = (
            0
            if row["execution_state"] in ("not_issued", "definite_failure")
            else row["reserved_micro_usd"]
        )
        result = await conn.execute(
            text("""UPDATE app.budget_windows SET reserved_micro_usd=reserved_micro_usd-:reserved,spent_micro_usd=spent_micro_usd+:charge
            WHERE workspace_id=:w AND reserved_micro_usd>=:reserved AND ((period='day' AND starts_at=:day) OR (period='month' AND starts_at=:month))"""),
            {
                "w": ctx.workspace_id,
                "reserved": row["reserved_micro_usd"],
                "charge": charge,
                "day": row["day_start"],
                "month": row["month_start"],
            },
        )
        if result.rowcount != 2:
            raise ValueError("Budget ledger windows are inconsistent")
        await conn.execute(
            text("""UPDATE app.model_usage SET cost_micro_usd=:charge,state=:state,usage_estimated=:estimated,
            execution_state=CASE WHEN execution_state='issued' THEN 'ambiguous' ELSE execution_state END
            WHERE workspace_id=:w AND id=:id"""),
            {
                "w": ctx.workspace_id,
                "id": row["id"],
                "charge": charge,
                "state": "settled"
                if row["execution_state"] in ("not_issued", "definite_failure")
                else "estimated",
                "estimated": row["execution_state"] not in ("not_issued", "definite_failure"),
            },
        )


async def all_settled(conn: AsyncConnection, ctx: AuthContext, run_id: UUID) -> bool:
    return not bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM app.model_usage WHERE workspace_id=:w AND run_id=:run AND state='reserved' LIMIT 1"
                ),
                {"w": ctx.workspace_id, "run": run_id},
            )
        ).first()
    )


async def reserve(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    request_hash: str,
    model_key: str,
    model_id: str,
    prices: Prices,
    amount: int,
    hosted: bool,
    purpose: Literal["generation", "rewrite", "summary"] = "generation",
) -> Attempt:
    await JobRepository.fence(conn, lease)
    run = (
        (
            await conn.execute(
                text("""SELECT r.*,c.deleted_at AS conversation_deleted FROM app.runs r
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
        WHERE r.workspace_id=:w AND r.id=:run AND r.user_id=:u FOR UPDATE OF r"""),
                {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .one()
    )
    if (
        run["status"] not in ("queued", "running")
        or run["cancel_requested"]
        or run["conversation_deleted"]
    ):
        raise Problem(409, "run_cancelled", "The run cannot issue another attempt.")
    workspace = (
        (
            await conn.execute(
                text("SELECT * FROM app.workspaces WHERE id=:w"), {"w": ctx.workspace_id}
            )
        )
        .mappings()
        .one()
    )
    if hosted and workspace["privacy_mode"] == "local_only":
        raise Problem(409, "privacy_policy", "Hosted inference is disabled for this workspace.")
    if run["requested_model_key"] not in ("auto", model_key):
        raise Problem(409, "model_mismatch", "Explicit model selection cannot change providers.")
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT * FROM app.model_usage WHERE workspace_id=:w AND run_id=:run ORDER BY attempt_no FOR UPDATE"
                ),
                {"w": ctx.workspace_id, "run": lease.run_id},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        if (
            row["purpose"] == purpose
            and row["request_sha256"] == request_hash
            and row["model_key"] == model_key
            and row["execution_state"] in ("not_issued", "response_stored")
        ):
            snapshot = row["price_snapshot"]
            frozen_prices = Prices(
                row["price_version"],
                Decimal(snapshot["input"]),
                Decimal(snapshot["output"]),
                Decimal(snapshot["cached_input"]),
            )
            result = None
            if row["response"]:
                raw = row["response"]
                result = ModelResult(
                    raw["status"],
                    Answer.model_validate(raw["answer"]) if raw["answer"] else None,
                    Usage.model_validate(raw["usage"]) if raw["usage"] else None,
                    raw["resolved_model_id"],
                    raw["provider_response_id"],
                    raw["raw_text"],
                )
            return Attempt(
                row["id"],
                row["attempt_no"],
                row["reserved_micro_usd"],
                frozen_prices,
                row["execution_state"],
                result,
            )
    if any(
        row["execution_state"] in ("issued", "ambiguous")
        or (row["execution_state"] == "response_stored" and row["purpose"] == purpose)
        or row["state"] == "reserved"
        for row in rows
    ):
        raise Problem(409, "provider_interrupted", "An existing attempt must not be replayed.")
    if purpose != "generation" and any(row["purpose"] in (purpose, "generation") for row in rows):
        raise Problem(
            409, "auxiliary_attempt_limit", "Optional memory allows one attempt before generation."
        )
    if len(rows) >= 3 or sum(row["cost_micro_usd"] or 0 for row in rows) + amount > 50_000:
        raise Problem(429, "run_budget_exceeded", "The run's attempt or cost limit was reached.")
    day = (
        await conn.execute(text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date"))
    ).scalar_one()
    month = day.replace(day=1)
    for period, start, limit in [
        ("day", day, workspace["daily_budget_micro_usd"]),
        ("month", month, workspace["monthly_budget_micro_usd"]),
    ]:
        await conn.execute(
            text("""INSERT INTO app.budget_windows(workspace_id,period,starts_at,limit_micro_usd) VALUES(:w,:period,:start,:limit)
            ON CONFLICT(workspace_id,period,starts_at) DO UPDATE SET limit_micro_usd=EXCLUDED.limit_micro_usd"""),
            {"w": ctx.workspace_id, "period": period, "start": start, "limit": limit},
        )
        result_update = await conn.execute(
            text("""UPDATE app.budget_windows SET reserved_micro_usd=reserved_micro_usd+:amount
            WHERE workspace_id=:w AND period=:period AND starts_at=:start AND (:amount=0 OR spent_micro_usd+reserved_micro_usd+:amount<=limit_micro_usd)"""),
            {"w": ctx.workspace_id, "period": period, "start": start, "amount": amount},
        )
        if result_update.rowcount != 1:
            raise Problem(429, "budget_exceeded", "Workspace inference budget is exhausted.")
    attempt_id = uuid4()
    await conn.execute(
        text("""INSERT INTO app.model_usage(workspace_id,id,run_id,user_id,conversation_id,attempt_no,purpose,model_key,resolved_model_id,
        price_version,price_snapshot,reserved_micro_usd,request_sha256,state,day_start,month_start)
        VALUES(:w,:id,:run,:u,:conversation,:number,:purpose,:key,:model,:price_version,CAST(:prices AS jsonb),:amount,:hash,'reserved',:day,:month)"""),
        {
            "w": ctx.workspace_id,
            "id": attempt_id,
            "run": lease.run_id,
            "u": ctx.user_id,
            "conversation": run["conversation_id"],
            "number": len(rows) + 1,
            "purpose": purpose,
            "key": model_key,
            "model": model_id,
            "price_version": prices.version,
            "prices": json.dumps(
                {
                    "input": str(prices.input),
                    "output": str(prices.output),
                    "cached_input": str(prices.cached_input),
                }
            ),
            "amount": amount,
            "hash": request_hash,
            "day": day,
            "month": month,
        },
    )
    return Attempt(attempt_id, len(rows) + 1, amount, prices, "not_issued")


async def mark_issued(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, attempt: Attempt, hosted: bool
) -> None:
    await JobRepository.fence(conn, lease)
    # This transaction is the last admission point before transport; no transaction spans HTTP.
    row = (
        (
            await conn.execute(
                text("""SELECT r.cancel_requested,r.status,w.privacy_mode,c.deleted_at FROM app.runs r
        JOIN app.workspaces w ON w.id=r.workspace_id JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
        WHERE r.workspace_id=:w AND r.id=:run AND r.user_id=:u FOR UPDATE OF r"""),
                {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .one()
    )
    if row["cancel_requested"] or row["status"] not in ("queued", "running") or row["deleted_at"]:
        raise Problem(409, "run_cancelled", "The run cannot issue a provider request.")
    if hosted and row["privacy_mode"] == "local_only":
        raise Problem(409, "privacy_policy", "Hosted inference is disabled.")
    result = await conn.execute(
        text("""UPDATE app.model_usage SET execution_state='issued',issued_at=clock_timestamp()
        WHERE workspace_id=:w AND id=:id AND run_id=:run AND user_id=:u AND execution_state='not_issued' AND state='reserved'"""),
        {"w": ctx.workspace_id, "id": attempt.id, "run": lease.run_id, "u": ctx.user_id},
    )
    if result.rowcount != 1:
        raise Problem(409, "provider_interrupted", "This provider request was already issued.")


async def settle(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    attempt: Attempt,
    outcome: ModelResult | ModelFault,
    seconds: float,
) -> None:
    await JobRepository.fence(conn, lease)
    row = (
        (
            await conn.execute(
                text(
                    "SELECT * FROM app.model_usage WHERE workspace_id=:w AND id=:id AND run_id=:run AND user_id=:u FOR UPDATE"
                ),
                {"w": ctx.workspace_id, "id": attempt.id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .one()
    )
    if row["state"] != "reserved":
        return
    if row["execution_state"] != "issued":
        raise Problem(
            409, "attempt_not_issued", "Only an issued request can settle provider usage."
        )
    usage = outcome.usage if isinstance(outcome, ModelResult) else None
    estimated = usage is None and not (
        isinstance(outcome, ModelFault) and outcome.execution == "definite_failure"
    )
    snapshot = row["price_snapshot"]
    prices = Prices(
        row["price_version"],
        Decimal(snapshot["input"]),
        Decimal(snapshot["output"]),
        Decimal(snapshot["cached_input"]),
    )
    charge = prices.charge(usage) if usage else row["reserved_micro_usd"] if estimated else 0
    response = None
    if isinstance(outcome, ModelResult):
        response = json.dumps(
            {
                "status": outcome.status,
                "answer": outcome.answer.model_dump() if outcome.answer else None,
                "usage": usage.model_dump() if usage else None,
                "resolved_model_id": outcome.resolved_model_id,
                "provider_response_id": outcome.provider_response_id,
                "raw_text": outcome.raw_text,
            }
        )
    updated = await conn.execute(
        text("""UPDATE app.budget_windows SET reserved_micro_usd=reserved_micro_usd-:reserved,spent_micro_usd=spent_micro_usd+:charge
        WHERE workspace_id=:w AND reserved_micro_usd>=:reserved AND ((period='day' AND starts_at=:day) OR (period='month' AND starts_at=:month))"""),
        {
            "w": ctx.workspace_id,
            "reserved": row["reserved_micro_usd"],
            "charge": charge,
            "day": row["day_start"],
            "month": row["month_start"],
        },
    )
    if updated.rowcount != 2:
        raise ValueError("Budget ledger windows are inconsistent")
    await conn.execute(
        text("""UPDATE app.model_usage SET state=:state,usage_estimated=:estimated,execution_state=:execution,response=CAST(:response AS jsonb),
        cost_micro_usd=:charge,input_tokens=:input,output_tokens=:output,cached_input_tokens=:cached,reasoning_output_tokens=:reasoning,inference_seconds=:seconds,
        resolved_model_id=:model,failure_code=:failure_code,failure_retryable=:failure_retryable,
        retry_after_seconds=:retry_after WHERE workspace_id=:w AND id=:id"""),
        {
            "w": ctx.workspace_id,
            "id": attempt.id,
            "state": "estimated" if estimated else "settled",
            "estimated": estimated,
            "execution": "response_stored"
            if isinstance(outcome, ModelResult)
            else outcome.execution,
            "response": response,
            "charge": charge,
            "input": usage.input_tokens if usage else None,
            "output": usage.output_tokens if usage else None,
            "cached": usage.cached_input_tokens if usage else None,
            "reasoning": usage.reasoning_output_tokens if usage else None,
            "seconds": seconds,
            "failure_code": outcome.code[:80] if isinstance(outcome, ModelFault) else None,
            "failure_retryable": isinstance(outcome, ModelFault)
            and outcome.retryable
            and (
                outcome.retry_after is None
                or (math.isfinite(outcome.retry_after) and outcome.retry_after >= 0)
            ),
            "retry_after": outcome.retry_after
            if isinstance(outcome, ModelFault)
            and outcome.retry_after is not None
            and math.isfinite(outcome.retry_after)
            and outcome.retry_after >= 0
            else None,
            "model": outcome.resolved_model_id
            if isinstance(outcome, ModelResult)
            else row["resolved_model_id"],
        },
    )


async def check_run_egress(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> None:
    row = (
        (
            await conn.execute(
                text("""SELECT r.status,r.cancel_requested,c.deleted_at FROM app.runs r
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
        WHERE r.workspace_id=:w AND r.id=:run AND r.user_id=:u"""),
                {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if (
        not row
        or row["status"] not in ("queued", "running")
        or row["cancel_requested"]
        or row["deleted_at"]
    ):
        raise Problem(409, "run_cancelled", "The run can no longer emit content.")


async def has_generation(conn: AsyncConnection, ctx: AuthContext, run_id: UUID) -> bool:
    return bool(
        (
            await conn.execute(
                text("""SELECT 1 FROM app.model_usage WHERE workspace_id=:w AND run_id=:run AND user_id=:u
        AND purpose='generation' AND execution_state='response_stored' AND state IN ('settled','estimated') LIMIT 1"""),
                {"w": ctx.workspace_id, "run": run_id, "u": ctx.user_id},
            )
        ).first()
    )


async def remaining_budget(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> int:
    """Read-only routing quote. Only reserve() can admit spending under row locks."""
    await JobRepository.fence(conn, lease)
    result = (
        await conn.execute(
            text("""SELECT LEAST(
                GREATEST(0,50000-COALESCE((SELECT sum(CASE WHEN state='reserved'
                    THEN reserved_micro_usd ELSE COALESCE(cost_micro_usd,0) END)
                    FROM app.model_usage WHERE workspace_id=:w AND run_id=:run),0)),
                GREATEST(0,w.daily_budget_micro_usd-COALESCE(d.spent_micro_usd+d.reserved_micro_usd,0)),
                GREATEST(0,w.monthly_budget_micro_usd-COALESCE(m.spent_micro_usd+m.reserved_micro_usd,0)))
            FROM app.workspaces w
            LEFT JOIN app.budget_windows d ON d.workspace_id=w.id AND d.period='day'
                AND d.starts_at=(clock_timestamp() AT TIME ZONE 'UTC')::date
            LEFT JOIN app.budget_windows m ON m.workspace_id=w.id AND m.period='month'
                AND m.starts_at=date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')::date
            WHERE w.id=:w"""),
            {"w": ctx.workspace_id, "run": lease.run_id},
        )
    ).scalar_one()
    return int(result)
