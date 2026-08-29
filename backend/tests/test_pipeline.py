import pytest

from pipelineai.config import Config
from pipelineai.pipeline import AnalysisPipeline

VULN = (
    "@@ -0,0 +1,4 @@\n"
    "+def run(cmd):\n"
    "+    import subprocess\n"
    "+    return subprocess.run(cmd, shell=True)\n"
    "+    q = f\"SELECT * FROM t WHERE x = {cmd}\"\n"
)
CLEAN = "@@ -0,0 +1,2 @@\n+def add(a, b):\n+    return a + b\n"


@pytest.fixture
def pipe(tmp_path):
    cfg = Config(lexicon_path=tmp_path / "lex.json")
    return AnalysisPipeline(cfg)


def test_block_on_real_vulnerable_diff(pipe):
    r = pipe.analyze_files([{"filename": "svc/api.py", "patch": VULN}])
    assert r.verdict == "BLOCK"
    assert r.risk_score >= r.block_threshold
    ids = {f["rule_id"] for f in r.findings}
    assert "shell_injection" in ids
    assert "sql_dynamic_query" in ids  # the assign-then-execute pattern


def test_allow_on_clean_diff(pipe):
    r = pipe.analyze_files([{"filename": "svc/math.py", "patch": CLEAN}])
    assert r.verdict == "ALLOW"
    assert r.risk_score < r.block_threshold
    assert r.findings == []


def test_every_stage_is_timed_and_ordered(pipe):
    r = pipe.analyze_files([{"filename": "a.py", "patch": CLEAN}])
    assert [t.name for t in r.timings] == ["impact", "simulation", "memory", "risk"]
    assert all(t.duration_ms >= 0 for t in r.timings)
    # verdict_ms is the sum of the measured stages, not an invented number
    assert abs(r.verdict_ms - sum(t.duration_ms for t in r.timings)) < 1e-6


def test_metrics_are_real_not_placeholder(pipe):
    r = pipe.analyze_files([{"filename": "svc/api.py", "patch": VULN}])
    sim = r.timing("simulation").detail
    assert sim["attack_patterns"] >= 10
    assert sim["added_lines_scanned"] == 4
    assert sim["findings"] == len(r.issues)
    impact = r.timing("impact").detail
    assert impact["files_analyzed"] == 1


def test_pipeline_and_riskagent_agree(pipe):
    """The CLI pipeline and the webhook's RiskAgent.decide must return the same
    verdict + score for the same input (both go through RiskAgent.synthesize)."""
    files = [{"filename": "svc/api.py", "patch": VULN}]
    via_pipeline = pipe.analyze_files(files)
    via_decide = pipe.risk.decide({"id": 1, "files": files})
    assert via_pipeline.verdict == via_decide["verdict"]
    assert via_pipeline.risk_score == via_decide["risk_score"]
