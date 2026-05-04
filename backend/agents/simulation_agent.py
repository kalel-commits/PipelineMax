class SimulationAgent:
    def __init__(self, sandbox_orchestrator):
        self.sandbox = sandbox_orchestrator

    def run_with_chaos(self, branch, target_flow):
        """
        Executes code deterministically and injects adversarial failures.
        """
        return self.sandbox.execute_with_chaos(branch, target_flow)
