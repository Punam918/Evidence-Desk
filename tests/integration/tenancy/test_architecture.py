"""Prevent transport and provider modules from accumulating persistence logic."""

import ast
from pathlib import Path


def test_routes_and_domain_services_do_not_embed_sql():
    root = Path("apps/api/src/evidencedesk")
    files = list((root / "routes").glob("*.py")) + list(root.glob("*/service.py"))
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"execute", "exec_driver_sql"}, (
                    f"{path}:{node.lineno} should call a repository"
                )


def test_provider_modules_have_no_database_dependency():
    for path in Path("apps/api/src/evidencedesk/providers").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("sqlalchemy", "psycopg", "evidencedesk.db")
                )
