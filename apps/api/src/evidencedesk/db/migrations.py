"""Execute reviewed multi-statement PostgreSQL DDL without treating SQL text as bind parameters."""

from alembic import op


def execute_sql(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
