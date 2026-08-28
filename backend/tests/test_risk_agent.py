import pytest
from agents.impact_agent import ImpactAgent
from agents.memory_agent import MemoryAgent
from agents.simulation_agent import SimulationAgent
from agents.risk_agent import RiskAgent

CLEAN_PATCH = "@@ -1,1 +1,2 @@\n+def add(a, b):\n+    return a + b\n"
BARE_EXCEPT_PATCH = "@@ -1,1 +1,3 @@\n+try:\n+    risky()\n+except:\n+    pass\n"


class _FakeRemediation:
    def __init__(self):
        self.calls = 0

    async def suggest_fix(self, issues, diff_context):
        self.calls += 1
        return {"source": "openai", "summary": "fake suggestion", "fixes": []}


@pytest.mark.asyncio
async def test_allow_when_no_issues(tmp_path):
    memory = MemoryAgent(store_path=str(tmp_path / "lexicon.json"))
    remediation = _FakeRemediation()
    risk = RiskAgent(ImpactAgent(), memory, SimulationAgent(), remediation_ai=remediation)

    verdict = await risk.evaluate_pr({"id": 1, "files": [{"filename": "a.py", "patch": CLEAN_PATCH}]})

    assert verdict["verdict"] == "ALLOW"
    assert remediation.calls == 0  # AI must not be called when nothing is blocked


@pytest.mark.asyncio
async def test_block_when_adversarial_issue_found(tmp_path):
    memory = MemoryAgent(store_path=str(tmp_path / "lexicon.json"))
    remediation = _FakeRemediation()
    risk = RiskAgent(ImpactAgent(), memory, SimulationAgent(), remediation_ai=remediation)

    verdict = await risk.evaluate_pr({"id": 1, "files": [{"filename": "parser.py", "patch": BARE_EXCEPT_PATCH}]})

    assert verdict["verdict"] == "BLOCK"
    assert remediation.calls == 1
    assert verdict["suggestion"]["summary"] == "fake suggestion"


@pytest.mark.asyncio
async def test_block_persists_failure_for_future_regression_detection(tmp_path):
    store = str(tmp_path / "lexicon.json")
    risk = RiskAgent(ImpactAgent(), MemoryAgent(store_path=store), SimulationAgent(), remediation_ai=_FakeRemediation())

    await risk.evaluate_pr({"id": 1, "files": [{"filename": "parser.py", "patch": BARE_EXCEPT_PATCH}]})

    # Fresh MemoryAgent instance over the same file simulates a restart.
    fresh_memory = MemoryAgent(store_path=store)
    match = fresh_memory.check_lexicon(SimulationAgent().analyze(
        [{"filename": "parser.py", "patch": BARE_EXCEPT_PATCH}]
    )["issues"])
    assert match is not None


@pytest.mark.asyncio
async def test_works_without_remediation_configured(tmp_path):
    memory = MemoryAgent(store_path=str(tmp_path / "lexicon.json"))
    risk = RiskAgent(ImpactAgent(), memory, SimulationAgent(), remediation_ai=None)

    verdict = await risk.evaluate_pr({"id": 1, "files": [{"filename": "parser.py", "patch": BARE_EXCEPT_PATCH}]})

    assert verdict["verdict"] == "BLOCK"
    assert verdict["suggestion"]["source"] == "none"
