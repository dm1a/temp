import runpy
from pathlib import Path


def test_initial_migration_is_raw_sql() -> None:
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "0001_initial_schema.py"
    migration = runpy.run_path(str(migration_path))
    statements = migration["UPGRADE_STATEMENTS"]

    assert len(statements) == 20
    assert sum("CREATE TABLE" in statement for statement in statements) == 7
    assert sum("GENERATED ALWAYS AS" in statement for statement in statements) == 1
    assert "pg_catalog.russian" in "\n".join(statements)


def test_alembic_has_no_target_metadata() -> None:
    env_path = Path(__file__).parents[1] / "alembic" / "env.py"
    env_source = env_path.read_text()

    assert "target_metadata = None" in env_source
    assert "Base.metadata" not in env_source
