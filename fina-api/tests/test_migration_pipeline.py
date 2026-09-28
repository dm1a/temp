import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


@pytest.fixture
def deploy_runner(tmp_path: Path):
    config = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    script = "\n".join(config["deploy:develop"]["script"])
    shutil.copytree(ROOT / ".chart", tmp_path / ".chart")
    package = tmp_path / "fixture.tgz"
    with tarfile.open(package, "w:gz") as archive:
        for name, contents in {
            "Chart.yaml": (
                "apiVersion: v2\nname: common-chart\nversion: 1.0.0\n"
                "appVersion: old\ndescription: common\n"
            ),
            "templates/deployment.yaml": "# shared chart fixture\n",
        }.items():
            data = contents.encode()
            info = tarfile.TarInfo("common-chart/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in ("git", "helm", "kubectl", "kubedog"):
        command = commands / name
        command.write_text(
            f"#!{sys.executable}\n"
            """import json
import os
import shutil
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_TRACE"], "a") as trace:
    trace.write(json.dumps([tool, args]) + "\\n")
operation = tool + ("-" + args[0] if tool == "helm" else "")
if operation == os.environ.get("FAIL_OPERATION"):
    sys.exit(1)
if tool == "git":
    print("v1.2.3")
elif tool == "helm" and args[0] == "pull":
    directory = Path(args[args.index("--destination") + 1])
    shutil.copyfile(os.environ["CHART_FIXTURE"], directory / "common-chart-1.0.0.tgz")
elif tool == "helm" and args[0] == "upgrade":
    chart = Path(args[-1])
    hook = (chart / "templates/fina-migration.yaml").read_text()
    assert '"helm.sh/hook": pre-install,pre-upgrade' in hook
    assert "appVersion: v1.2.3" in (chart / "Chart.yaml").read_text()
"""
        )
        command.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    environment = {
        **os.environ,
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "CHART_FIXTURE": str(package),
        "COMMAND_TRACE": str(trace),
        "K8S_CLUSTER_DEV": "development-context",
        "CR_DEVELOP_URL": "registry.example",
        "CI_PROJECT_PATH": "team/fina",
        "CI_PROJECT_NAME": "fina-adapter",
        "CI_COMMIT_SHORT_SHA": "abc123",
        "HELM_CHART_COMMON_REF": "v1.0.0",
        "HELM_COMMON_CHART_NAME": "helm-private-repo/common-chart",
        "HELM_SET_PARAMS": "",
        "NAMESPACE": "ai-agents",
        "KUBEDOG_TIMEOUT": "300",
    }

    def run(fail_operation=""):
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=tmp_path,
            env={**environment, "FAIL_OPERATION": fail_operation},
            text=True,
            capture_output=True,
            timeout=10,
        )
        return result, [json.loads(line) for line in trace.read_text().splitlines()]

    return run


def test_deploy_installs_hook_with_same_image_before_tracking_rollout(deploy_runner):
    result, calls = deploy_runner()
    assert result.returncode == 0, result.stderr
    upgrade = next(args for tool, args in calls if tool == "helm" and args[0] == "upgrade")
    assert "image.repository=registry.example/team/fina" in upgrade
    assert "image.tag=v1.2.3" in upgrade
    assert "finaMigrationImage=registry.example/team/fina:v1.2.3" in upgrade
    assert "--no-hooks=false" in upgrade
    assert upgrade[upgrade.index("--timeout") + 1] == "6m"
    assert calls[-1][0] == "kubedog"
    assert ["kubectl", ["config", "use-context", "development-context"]] in calls


@pytest.mark.parametrize("operation", ["helm-pull", "helm-upgrade"])
def test_chart_or_migration_failure_stops_rollout(deploy_runner, operation):
    result, calls = deploy_runner(operation)
    assert result.returncode != 0
    assert not any(tool == "kubedog" for tool, _ in calls)
    if operation == "helm-upgrade":
        assert any(tool == "kubectl" and "logs" in args for tool, args in calls)


def test_rollout_failure_still_fails_deployment(deploy_runner):
    result, _ = deploy_runner("kubedog")
    assert result.returncode != 0


@pytest.fixture
def hook_chart(tmp_path: Path):
    helm = os.environ.get("FINA_TEST_HELM") or shutil.which("helm")
    if not helm:
        pytest.skip("Helm is required for rendering tests; set FINA_TEST_HELM or install helm")
    (tmp_path / "templates").mkdir()
    (tmp_path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: migration-test\nversion: 0.1.0\nappVersion: unrelated\n"
    )
    shutil.copyfile(
        ROOT / ".chart/fina-migration.yaml", tmp_path / "templates/fina-migration.yaml"
    )
    config = yaml.safe_load((ROOT / ".chart/config-develop.yaml").read_text())
    config["finaMigrationImage"] = "registry.example/fina@sha256:" + "a" * 64

    def render(*, upgrade=False, missing=None):
        values = json.loads(json.dumps(config))
        if missing == "image":
            del values["finaMigrationImage"]
        elif missing:
            del values["config-gen"]["extraConfigMaps"]["config-fina"][missing]
        values["config-gen"]["extraConfigMaps"]["config-fina"]["FINA_DB_HOST"] = "new-db-host"
        (tmp_path / "values.yaml").write_text(yaml.safe_dump(values))
        args = [helm, "template", "fina-adapter", str(tmp_path), "--namespace", "ai-agents"]
        if upgrade:
            args.append("--is-upgrade")
        result = subprocess.run(args, text=True, capture_output=True, timeout=10)
        return result, values

    return render


@pytest.mark.parametrize("upgrade", [False, True])
def test_hook_uses_current_values_without_requiring_existing_configmap(hook_chart, upgrade):
    result, values = hook_chart(upgrade=upgrade)
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load(result.stdout)
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "hook-failed" not in annotations["helm.sh/hook-delete-policy"]
    assert job["metadata"]["name"] == "fina-adapter-migrate"
    assert job["metadata"]["namespace"] == "ai-agents"
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == values["finaMigrationImage"]
    assert container["command"] == ["alembic", "upgrade", "head"]
    assert "envFrom" not in container
    env = {entry["name"]: entry for entry in container["env"]}
    config = values["config-gen"]["extraConfigMaps"]["config-fina"]
    for key in (
        "FINA_DB_HOST",
        "FINA_DB_PORT",
        "FINA_DB_USER",
        "FINA_DB_NAME",
        "VAULT_URL",
        "VAULT_ENGINE",
        "VAULT_SECRET_PATH",
    ):
        assert env[key]["value"] == config[key]
    assert env["VAULT_ROLE_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "common-vault",
        "key": "VAULT_ROLE_ID_AI_AGENTS",
    }
    assert "FINA_MCP_API_KEY" not in env


@pytest.mark.parametrize("missing", ["image", "FINA_DB_NAME", "VAULT_SECRET_PATH"])
def test_hook_rejects_missing_image_or_database_configuration(hook_chart, missing):
    result, _ = hook_chart(missing=missing)
    assert result.returncode != 0
    assert ("Set finaMigrationImage" if missing == "image" else missing) in result.stderr
