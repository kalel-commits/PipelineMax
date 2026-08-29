import json

from typer.testing import CliRunner

from pipelineai.cli import app

runner = CliRunner()


def test_version_runs():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert "pipelineai" in r.stdout


def test_config_redacts_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("WEBHOOK_SECRET", "another-secret")
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0
    assert "super-secret-token-value" not in r.stdout
    assert "another-secret" not in r.stdout
    assert "set (" in r.stdout  # shows length marker instead


def test_check_local_block_exit_code(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def r(c):\n    import subprocess\n    return subprocess.run(c, shell=True)\n")
    r = runner.invoke(app, ["check", "--local", str(f), "--no-remediate"])
    assert r.exit_code == 1            # BLOCK -> exit 1 (CI-friendly)
    assert "BLOCK" in r.stdout


def test_check_local_allow_exit_code(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text("def add(a, b):\n    return a + b\n")
    r = runner.invoke(app, ["check", "--local", str(f), "--no-remediate"])
    assert r.exit_code == 0            # ALLOW -> exit 0
    assert "ALLOW" in r.stdout


def test_check_local_json_is_valid(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("x = eval(input())\n")
    r = runner.invoke(app, ["check", "--local", str(f), "--no-remediate", "--json"])
    data = json.loads(r.stdout)
    assert data["verdict"] == "BLOCK"
    assert data["risk_score"] >= data["block_threshold"]
    assert "timings_ms" in data and "impact" in data["timings_ms"]
