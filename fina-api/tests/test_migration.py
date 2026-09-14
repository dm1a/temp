from io import StringIO
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from fina.db.schema import CURRENT_SCHEMA_REVISION

ROOT = Path(__file__).parents[1]


def test_readiness_revision_matches_single_initial_migration() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1
    assert revisions[0].down_revision is None
    assert revisions[0].revision == script.get_current_head() == CURRENT_SCHEMA_REVISION


def test_migration_does_not_require_mcp_key(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://fina:fina@localhost:5432/fina")
    monkeypatch.delenv("FINA_MCP_API_KEY", raising=False)
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "head", sql=True)
    assert "CREATE TABLE calls" in output.getvalue()
    assert "claim_token UUID" in output.getvalue()
