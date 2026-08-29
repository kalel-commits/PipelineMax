from typing import Dict, Any, Optional, List

# A PR is BLOCKED when its risk score reaches this threshold. The score function
# below is calibrated so that the two hard rules the guardrail has always
# enforced -- (a) any severity>=7 static finding, (b) a severity-critical path
# change that matches a stored regression -- always land at or above it.
BLOCK_THRESHOLD = 70


def compute_risk_score(
    issues: List[Dict[str, Any]],
    impact_result: Dict[str, Any],
    historical_match: Optional[str],
) -> int:
    """Deterministic 0-100 risk score. A transparent weighted sum of the signals
    the three analyzers actually produce -- no model, no randomness, same input
    always yields the same number.

      base            = highest static-finding severity (1-9)  x 9      -> 0..81
      pile-up         = +4 per extra finding, capped at 3 extras        -> 0..12
      critical path   = +12 if the change reaches an auth/config/etc. path
      known regression= +15 if this exact failure pattern was seen before
    """
    max_sev = max((i.get("severity", 0) for i in issues), default=0)
    score = max_sev * 9
    if len(issues) > 1:
        score += min(len(issues) - 1, 3) * 4
    if impact_result.get("critical_hit"):
        score += 12
    if historical_match:
        score += 15

    # Floors: the historically-enforced hard rules must always clear the gate.
    if any(i.get("severity", 0) >= 7 for i in issues):
        score = max(score, 75)
    if impact_result.get("critical_hit") and historical_match:
        score = max(score, 72)

    return max(0, min(100, score))


def severity_label(score: int) -> str:
    if score >= BLOCK_THRESHOLD:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "NONE"


class RiskAgent:
    """Synthesizes the other three analyzers into a single deterministic risk
    score, and gates the PR on it. The gate never depends on the LLM --
    ``remediation_ai`` only enriches the *explanation* attached to a BLOCK, so a
    flaky or unavailable AI provider can never change whether a PR is blocked.

    Two phases so callers can publish the merge gate immediately and enrich it
    afterwards:

        verdict = agent.decide(pr_data)        # deterministic, no network I/O
        ...publish verdict to GitHub...
        await agent.enrich(verdict, pr_data)   # persist + LLM remediation (BLOCK only)

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
        """Deterministic ALLOW/BLOCK gate. Runs the three analyzers and the
        in-memory regression lexicon; no LLM, no disk writes, no network."""
        files = pr_data.get("files", [])
        impact_result = self.impact.analyze(files)
        sim_result = self.sim.analyze(files)
        historical_pattern = self.memory.check_lexicon(sim_result["issues"])
        return self.synthesize(impact_result, sim_result, historical_pattern)

    def synthesize(
        self,
        impact_result: Dict[str, Any],
        sim_result: Dict[str, Any],
        historical_pattern: Optional[str],
    ) -> Dict[str, Any]:
        """Turn the three analyzers' outputs into a scored ALLOW/BLOCK verdict.
        Pure function of its inputs -- no I/O -- so an orchestrator can time each
        analyzer separately and still get the identical verdict."""
        issues = sim_result["issues"]
        score = compute_risk_score(issues, impact_result, historical_pattern)
        block = score >= BLOCK_THRESHOLD

        findings = [
            {
                "severity": severity_label(min(100, issue.get("severity", 0) * 11)),
                "category": issue["category"],
                "title": issue["description"],
                "file": issue["file"],
                "line": issue["line"],
                "evidence": issue.get("evidence", ""),
                "rule_id": issue.get("rule_id", ""),
            }
            for issue in sorted(issues, key=lambda i: -i.get("severity", 0))
        ]
        if historical_pattern:
            findings.append({
                "severity": "HIGH", "category": "regression", "title": historical_pattern,
                "file": impact_result.get("path", ""), "line": 0, "evidence": "", "rule_id": "memory_match",
            })

        if not block:
            return {
                "verdict": "ALLOW",
                "risk_score": score,
                "reason": (
                    f"Risk score {score}/100 (< {BLOCK_THRESHOLD}). "
                    "Survived static adversarial checks and semantic impact analysis."
                ),
                "impact": impact_result,
                "issues": issues,
                "findings": findings,
                "historical_match": historical_pattern,
            }

        details = [
            f"{issue['category']} risk in {issue['file']} (line {issue['line']}): {issue['description']}"
            for issue in issues
        ]
        if impact_result["critical_hit"]:
            details.append(f"Semantic blast radius: {impact_result['flow']} ({impact_result['path']})")
        if historical_pattern:
            details.append(f"Regression match: {historical_pattern}")

        return {
            "verdict": "BLOCK",
            "risk_score": score,
            "reason": (
                f"Risk score {score}/100 (>= {BLOCK_THRESHOLD}). "
                f"Static adversarial analysis detected {len(issues)} issue(s)"
                + (f" in a critical path ({impact_result['flow']})" if impact_result["critical_hit"] else "")
            ),
            "details": details,
            "impact": impact_result,
            "issues": issues,
            "findings": findings,
            "historical_match": historical_pattern,
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
