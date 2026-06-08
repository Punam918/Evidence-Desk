import argparse
import asyncio
from uuid import UUID


def main() -> None:
    from evidencedesk.observability.config import reject_automatic_content_tracing

    reject_automatic_content_tracing()
    parser = argparse.ArgumentParser(prog="evidencedesk")
    parser.add_argument(
        "command",
        choices=[
            "api",
            "ingest-worker",
            "index-worker",
            "index-reconcile",
            "chat-worker",
            "brief-worker",
            "model-probe",
            "metrics-worker",
            "checkpoint-prune",
            "replay-prune",
            "conversation-purge-worker",
            "source-redact",
            "conversation-expire",
            "source-objects-purge",
        ],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--once", action="store_true", help="Process at most one claim and exit")
    parser.add_argument("--workspace", type=UUID, help="Internal scoped maintenance workspace")
    parser.add_argument("--after", type=UUID, help="Resume an index/thread audit after this UUID")
    parser.add_argument(
        "--orphans",
        action="store_true",
        help="Audit one100-point orphan page instead; --after is its point UUID",
    )
    parser.add_argument("--model", help="Logical registry key for a synthetic readiness probe")
    parser.add_argument(
        "--allow-paid",
        action="store_true",
        help="Explicitly permit two synthetic hosted probe requests",
    )
    parser.add_argument(
        "--max-cost-micro-usd",
        type=int,
        default=0,
        help="Per-invocation worst-case probe cap, at most10000",
    )
    args = parser.parse_args()
    if args.command == "api":
        import uvicorn

        uvicorn.run(
            "evidencedesk.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
        )
    else:
        from evidencedesk.workers.runtime import indexing, ingestion, reconcile, with_signals

        if args.command == "source-objects-purge":
            if args.workspace is None:
                parser.error("source-objects-purge requires --workspace")
            from evidencedesk.workers.source_objects import purge as purge_objects

            operation = purge_objects(args.workspace)
        elif args.command == "conversation-expire":
            if args.workspace is None:
                parser.error("conversation-expire requires --workspace")
            from evidencedesk.workers.conversation_retention import expire

            operation = expire(args.workspace)
        elif args.command == "source-redact":
            if args.workspace is None:
                parser.error("source-redact requires --workspace")
            from evidencedesk.workers.source_redaction import redact

            operation = redact(args.workspace)
        elif args.command == "conversation-purge-worker":
            from evidencedesk.workers.conversation_purge import purge

            operation = purge(args.once)
        elif args.command == "replay-prune":
            if args.workspace is None:
                parser.error("replay-prune requires --workspace")
            from evidencedesk.workers.replay_retention import prune as prune_replay

            operation = prune_replay(args.workspace)
        elif args.command == "checkpoint-prune":
            if args.workspace is None:
                parser.error("checkpoint-prune requires --workspace")
            from evidencedesk.workers.checkpoint_retention import prune

            operation = prune(args.workspace, args.after)
        elif args.command == "metrics-worker":
            from evidencedesk.workers.metrics import monitor

            operation = monitor(args.once)
        elif args.command == "model-probe":
            if args.model is None:
                parser.error("model-probe requires --model")
            from evidencedesk.workers.probe import probe_model

            operation = probe_model(
                args.model, allow_paid=args.allow_paid, max_cost_micro_usd=args.max_cost_micro_usd
            )
        elif args.command == "brief-worker":
            from evidencedesk.workers.brief import brief

            operation = brief(args.once)
        elif args.command == "chat-worker":
            from evidencedesk.workers.chat import chat

            operation = chat(args.once)
        elif args.command == "index-reconcile":
            if args.workspace is None:
                parser.error("index-reconcile requires --workspace")
            operation = reconcile(args.workspace, args.after, orphans=args.orphans)
        else:
            operation = (
                ingestion(args.once) if args.command == "ingest-worker" else indexing(args.once)
            )
        try:
            from evidencedesk.observability.runtime import runtime

            async def monitored() -> int:
                async with runtime(args.command):
                    return await with_signals(operation)

            code = asyncio.run(monitored())
        except KeyboardInterrupt, asyncio.CancelledError:
            code = 0
        except Exception:
            # Validation/transport exceptions can contain DSNs or credentials. Report no raw exception.
            print(
                "Worker stopped after an unexpected failure. Check mounted configuration and service health."
            )
            code = 1
        raise SystemExit(code)
