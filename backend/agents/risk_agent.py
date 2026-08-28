from typing import Dict, Any, Optional


class RiskAgent:
    """Synthesizes the other three agents into a final, deterministic ALLOW/BLOCK
    verdict. The gate itself never depends on the LLM — remediation_ai only
    enriches the *explanation* attached to a BLOCK, so a flaky/unavailable AI
    provider can never change whether a PR is blocked.

    The verdict and its remediation are produced in two phases so callers can
    publish the merge gate immediately and enrich it afterwards:

        verdict = agent.decide(pr_data)        # deterministic, no I/O beyond the lexicon cache
        ...publish verdict to GitHub...
        await agent.enrich(verdict, pr_data)   # persists the failure + calls the LLM (BLOCK only)

    ``evaluate_pr`` keeps the original one-shot behaviour for existing callers.
    """

    def __init__(self, impact_agent, memory_agent, simulation_agent, remediation_ai: Optional[object] = None):
        self.impact = impact_agent
        self.memory = memory_agent
        self.sim = simulation_agent
        self.remediation = remediation_ai

    async def evaluate_pr(self, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        verdict = self.decide(pr_data)
        await self.enrich(verdict, pr_data)
        return verdict

    def decide(self, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic ALLOW/BLOCK gate. No LLM, no disk writes — just the three
        analyzers and the in-memory regression lexicon."""
        files = pr_data.get("files", [])

        impact_result = self.impact.analyze(files)
        sim_result = self.sim.analyze(files)
        historical_pattern = self.memory.check_lexicon(sim_result["issues"])

        block = sim_result["failed"] or (impact_result["critical_hit"] and historical_pattern is not None)

        if not block:
            return {
                "verdict": "ALLOW",
                "reason": "Survived static adversarial checks and semantic impact analysis.",
                "impact": impact_result,
                "issues": sim_result["issues"],
            }

        details = [
            f"{issue['category']} risk in {issue['file']} (line {issue['line']}): {issue['description']}"
            for issue in sim_result["issues"]
        ]
        if impact_result["critical_hit"]:
            details.append(f"Semantic blast radius: {impact_result['flow']} ({impact_result['path']})")
        if historical_pattern:
            details.append(f"Regression match: {historical_pattern}")

        return {
            "verdict": "BLOCK",
            "reason": f"Static adversarial analysis detected {len(sim_result['issues'])} issue(s)"
            + (f" in a critical path ({impact_result['flow']})" if impact_result["critical_hit"] else ""),
            "details": details,
            "impact": impact_result,
            "issues": sim_result["issues"],
            "suggestion": {"source": "pending", "summary": "", "fixes": []},
        }

    async def enrich(self, verdict: Dict[str, Any], pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """For a BLOCK: persist the failure for future regression detection and
        attach an LLM-authored (or deterministic-fallback) remediation suggestion.
        No-op for ALLOW. Nothing here can change the verdict."""
        if verdict["verdict"] != "BLOCK":
            return verdict

        self.memory.store_failure(pr_data, verdict["issues"])

        if self.remediation is not None:
            diff_context = "\n".join(f.get("patch", "") for f in pr_data.get("files", []))
            verdict["suggestion"] = await self.remediation.suggest_fix(verdict["issues"], diff_context)
        else:
            verdict["suggestion"] = {"source": "none", "summary": "No remediation engine configured.", "fixes": []}
        return verdict
