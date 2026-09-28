import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "k8s" / "migrate.sh"


@pytest.fixture
def migration_runner(tmp_path: Path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["KUBECTL_CALLS"], "a") as output:
    output.write(json.dumps(args) + "\\n")
action = next(arg for arg in args if arg in {"set", "create", "wait", "describe", "logs"})
if action == os.environ.get("FAIL_ACTION"):
    sys.exit(1)
if action == "set":
    print("rendered-migration-manifest")
elif action == "create":
    assert Path(args[args.index("-f") + 1]).read_text().strip() == "rendered-migration-manifest"
    print("job.batch/fina-migrate-generated")
"""
    )
    kubectl.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    rollout = tmp_path / "rollout"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "KUBECTL_CALLS": str(calls),
        "KUBE_CONTEXT": "deployment-context",
        "NAMESPACE": "deployment-namespace",
        "FINA_MIGRATION_IMAGE": "registry.example/fina@sha256:exact-deployment-digest",
    }
    environment.pop("FAIL_ACTION", None)

    def run(*, fail_action: str = "", missing: str = ""):
        env = {**environment, "FAIL_ACTION": fail_action}
        if missing:
            env.pop(missing)
        # Model the deploy job's set -e: Helm can run only after migration succeeds.
        result = subprocess.run(
            ["sh", "-ec", 'sh "$1"; touch "$2"', "deploy", str(SCRIPT), str(rollout)],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        invocations = (
            [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        )
        return result, invocations, rollout.exists()

    return run


def test_migration_completion_allows_rollout_with_same_image_and_target(migration_runner):
    result, calls, rolled_out = migration_runner()
    assert result.returncode == 0, result.stderr
    assert rolled_out
    assert "migrate=registry.example/fina@sha256:exact-deployment-digest" in calls[0]
    assert "--local" in calls[0]
    for call in calls:
        assert call[:4] == [
            "--context",
            "deployment-context",
            "--namespace",
            "deployment-namespace",
        ]
    wait = next(call for call in calls if "wait" in call)
    assert "job.batch/fina-migrate-generated" in wait
    assert "--for=condition=complete" in wait
    assert "--timeout=360s" in wait
    assert "completed successfully" in result.stdout


@pytest.mark.parametrize("action", ["set", "create", "wait"])
def test_render_create_or_migration_failure_blocks_rollout(migration_runner, action):
    result, calls, rolled_out = migration_runner(fail_action=action)
    assert result.returncode != 0
    assert not rolled_out
    if action == "set":
        assert len(calls) == 1
    elif action == "create":
        assert not any("wait" in call for call in calls)
    else:
        assert any("describe" in call for call in calls)
        assert any("logs" in call for call in calls)
        assert "application rollout must stop" in result.stderr


def test_log_collection_failure_does_not_fail_completed_migration(migration_runner):
    result, _, rolled_out = migration_runner(fail_action="logs")
    assert result.returncode == 0
    assert rolled_out


@pytest.mark.parametrize("missing", ["KUBE_CONTEXT", "NAMESPACE", "FINA_MIGRATION_IMAGE"])
def test_missing_target_or_image_fails_before_creating_a_job(migration_runner, missing):
    result, calls, rolled_out = migration_runner(missing=missing)
    assert result.returncode != 0
    assert not calls
    assert not rolled_out
