"""Instrumented analysis pipeline.

This is the single orchestration path. It wires the four analyzers, runs them in
the fixed order Impact -> Simulation -> Memory -> Risk, times each one with
``time.perf_counter`` (real measurement, never faked), and returns a structured
:class:`AnalysisResult` carrying every metric the CLI displays.

The FastAPI webhook server (``guardrail_main``) uses the same agents and the same
``RiskAgent.synthesize`` scoring, so the verdict a webhook produces and the
verdict ``pipelineai check`` produces are identical.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.impact_agent import ImpactAgent
from agents.memory_agent import MemoryAgent
from agents.simulation_agent import SimulationAgent, RULE_COUNT
from agents.risk_agent import RiskAgent, BLOCK_THRESHOLD
from integrations.github_client import GitHubClient, GitHubAPIError
from utils.ai_remediation import AIRemediation

from .config import Config


@dataclass
class StageTiming:
    name: str
    duration_ms: float
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    owner: str
    repo: str
    pr_number: int
    branch: str
    verdict: str                     # ALLOW | BLOCK | ERROR
    risk_score: int
    reason: str
    findings: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    impact: Dict[str, Any] = field(default_factory=dict)
    historical_match: Optional[str] = None
    suggestion: Dict[str, Any] = field(default_factory=dict)
    files_analyzed: int = 0
    timings: List[StageTiming] = field(default_factory=list)
    fetch_ms: Optional[float] = None
    remediation_ms: Optional[float] = None
    verdict_ms: float = 0.0          # fetch + all analyzers + score (NOT remediation)
    total_ms: float = 0.0
    github_status_posted: Optional[bool] = None
    error: Optional[str] = None

    @property
    def block_threshold(self) -> int:
        return BLOCK_THRESHOLD

    def timing(self, name: str) -> Optional[StageTiming]:
        return next((t for t in self.timings if t.name == name), None)


class AnalysisPipeline:
    def __init__(self, config: Config):
        self.config = config
        self.impact = ImpactAgent()
        self.memory = MemoryAgent(store_path=str(config.lexicon_path))
        self.sim = SimulationAgent()
        self.remediation = AIRemediation(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.openai_base_url,
        )
        self.risk = RiskAgent(self.impact, self.memory, self.sim, remediation_ai=self.remediation)
        self._github: Optional[GitHubClient] = None

    def _client(self) -> GitHubClient:
        if self._github is None:
            self._github = GitHubClient(token=self.config.github_token)
        return self._github

    async def _fetch(self, owner: str, repo: str, pr_number: int):
        """Fetch changed files and PR metadata (branch, head SHA) concurrently."""
        import asyncio

        gc = self._client()

        async def _meta():
            try:
                r = await gc._get_client().get(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                    headers=gc._headers(),
                )
                if r.status_code == 200:
                    j = r.json()
                    return {"branch": j.get("head", {}).get("ref", ""),
                            "head_sha": j.get("head", {}).get("sha", "")}
            except Exception:
                pass
            return {}

        files, meta = await asyncio.gather(
            gc.get_pull_request_files(owner, repo, pr_number), _meta()
        )
        return files, meta

    async def aclose(self) -> None:
        if self._github is not None:
            await self._github.aclose()

    # ------------------------------------------------------------------ #
    async def analyze_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        branch: str = "",
        remediate: bool = True,
        post_status: bool = False,
        head_sha: str = "",
        progress=None,
    ) -> AnalysisResult:
        """Fetch a real PR from GitHub, run the pipeline, optionally post the
        commit status and generate an LLM remediation. ``progress`` is an optional
        callback ``progress(stage_name)`` invoked as each stage *starts*."""
        t_total = time.perf_counter()

        def tick(stage: str):
            if progress:
                progress(stage)

        tick("fetch")
        t0 = time.perf_counter()
        try:
            files, meta = await self._fetch(owner, repo, pr_number)
        except GitHubAPIError as e:
            return AnalysisResult(
                owner=owner, repo=repo, pr_number=pr_number, branch=branch,
                verdict="ERROR", risk_score=0, reason=str(e), error=str(e),
                fetch_ms=(time.perf_counter() - t0) * 1000,
                total_ms=(time.perf_counter() - t_total) * 1000,
            )
        fetch_ms = (time.perf_counter() - t0) * 1000
        branch = branch or meta.get("branch", "")
        head_sha = head_sha or meta.get("head_sha", "")

        result = self._analyze_files(files, owner, repo, pr_number, branch, progress=progress)
        result.fetch_ms = fetch_ms
        result.verdict_ms = fetch_ms + sum(t.duration_ms for t in result.timings)

        if post_status and head_sha:
            tick("github-status")
            state = {"ALLOW": "success", "BLOCK": "failure"}.get(result.verdict, "error")
            desc = result.reason
            result.github_status_posted = await self._client().set_commit_status(
                owner, repo, head_sha, state, desc
            )

        if remediate and result.verdict == "BLOCK":
            tick("remediation")
            t0 = time.perf_counter()
            verdict_dict = {"verdict": "BLOCK", "issues": result.issues,
                            "suggestion": {"source": "pending", "summary": "", "fixes": []}}
            await self.risk.enrich(verdict_dict, {"id": pr_number, "files": files})
            result.suggestion = verdict_dict["suggestion"]
            result.remediation_ms = (time.perf_counter() - t0) * 1000

        result.total_ms = (time.perf_counter() - t_total) * 1000
        return result

    # ------------------------------------------------------------------ #
    def analyze_files(
        self, files: List[Dict[str, Any]], owner="", repo="", pr_number=0, branch="", *, progress=None,
    ) -> AnalysisResult:
        """Offline path: analyze an already-fetched file list (used by tests and
        the benchmark). No network, no remediation."""
        t_total = time.perf_counter()
        r = self._analyze_files(files, owner, repo, pr_number, branch, progress=progress)
        r.verdict_ms = sum(t.duration_ms for t in r.timings)
        r.total_ms = (time.perf_counter() - t_total) * 1000
        return r

    def _analyze_files(self, files, owner, repo, pr_number, branch, *, progress=None) -> AnalysisResult:
        def tick(stage):
            if progress:
                progress(stage)

        timings: List[StageTiming] = []

        tick("impact")
        t0 = time.perf_counter()
        impact_result = self.impact.analyze(files)
        functions_touched = sum(len(r_["functions_touched"]) for r_ in impact_result["records"])
        ast_ok = sum(1 for r_ in impact_result["records"] if r_["ast_parseable"])
        py_files = sum(1 for r_ in impact_result["records"] if r_["file"].endswith(".py"))
        timings.append(StageTiming("impact", (time.perf_counter() - t0) * 1000, {
            "files_analyzed": len(files),
            "functions_touched": functions_touched,
            "ast_parseable": f"{ast_ok}/{py_files}" if py_files else "0/0",
            "critical_path_hit": impact_result["critical_hit"],
            "flow": impact_result["flow"],
        }))

        tick("simulation")
        t0 = time.perf_counter()
        sim_result = self.sim.analyze(files)
        by_sev: Dict[str, int] = {}
        for i in sim_result["issues"]:
            k = "high" if i["severity"] >= 7 else "medium" if i["severity"] >= 5 else "low"
            by_sev[k] = by_sev.get(k, 0) + 1
        timings.append(StageTiming("simulation", (time.perf_counter() - t0) * 1000, {
            "attack_patterns": sim_result.get("rules_evaluated", RULE_COUNT),
            "added_lines_scanned": sim_result.get("added_lines_scanned", 0),
            "findings": len(sim_result["issues"]),
            "by_severity": by_sev,
        }))

        tick("memory")
        t0 = time.perf_counter()
        historical = self.memory.check_lexicon(sim_result["issues"])
        timings.append(StageTiming("memory", (time.perf_counter() - t0) * 1000, {
            "historical_patterns": len(self.memory._cache),
            "matches": 1 if historical else 0,
        }))

        tick("risk")
        t0 = time.perf_counter()
        verdict = self.risk.synthesize(impact_result, sim_result, historical)
        timings.append(StageTiming("risk", (time.perf_counter() - t0) * 1000, {
            "risk_score": verdict["risk_score"],
            "threshold": BLOCK_THRESHOLD,
            "decision": verdict["verdict"],
        }))

        return AnalysisResult(
            owner=owner, repo=repo, pr_number=pr_number, branch=branch,
            verdict=verdict["verdict"], risk_score=verdict["risk_score"], reason=verdict["reason"],
            findings=verdict.get("findings", []), issues=verdict.get("issues", []),
            impact=impact_result, historical_match=historical,
            files_analyzed=len(files), timings=timings,
        )
