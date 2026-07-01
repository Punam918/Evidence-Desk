"""Operator-only saver migration; runtime credentials cannot run DDL."""
import asyncio
import os
import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def main() -> None:
    async with await psycopg.AsyncConnection.connect(os.environ["ED_MIGRATION_DATABASE_URL"],autocommit=True,row_factory=dict_row) as conn:
        await conn.execute("SET search_path TO lg_checkpoints")
        await AsyncPostgresSaver(conn).setup()
        await conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA lg_checkpoints FROM PUBLIC")
        await conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA lg_checkpoints TO ed_checkpoint")
        await conn.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA lg_checkpoints TO ed_checkpoint")
if __name__ == "__main__":
    asyncio.run(main())
