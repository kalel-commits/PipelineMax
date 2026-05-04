import json

class RiskAgent:
    def __init__(self, simulation_agent, impact_agent, memory_agent, remediation_ai=None):
        self.sim = simulation_agent
        self.impact = impact_agent
        self.memory = memory_agent
        self.remediation = remediation_ai

    def evaluate_pr(self, pr_data):
        files_changed = pr_data.get("files_changed", [])
        branch = pr_data.get("branch", "main")
        
        # 1. Agents analyze in parallel
        semantic_map = self.impact.analyze_flow(files_changed)
        historical_pattern = self.memory.check_lexicon(files_changed)
        
        # 2. Chaos Simulation (The Crucible)
        sim_exit_code, chaos_logs = self.sim.run_with_chaos(
            branch=branch, 
            target_flow=semantic_map['flow']
        )
        
        # 3. Explainable Synthesis
        if sim_exit_code != 0:
            suggestion = "Add null checks or explicit types before parsing input."
            if self.remediation:
                suggestion = self.remediation.suggest_fix(chaos_logs, pr_data.get("diff", ""))
                
            self.memory.store_failure(pr_data, chaos_logs)
            
            return {
                "verdict": "BLOCK", 
                "reason": f"Chaos Simulation failed in {semantic_map['flow']}",
                "details": [
                    f"Test failed under adversarial payload (Exit Code {sim_exit_code})",
                    f"Semantic Blast Radius: {semantic_map['path']}"
                ] + ([f"Warning: {historical_pattern}"] if historical_pattern else []),
                "suggestion": suggestion
            }
            
        # 4. Success Path
        return {"verdict": "ALLOW", "reason": "Survived chaos testing and semantic checks."}
