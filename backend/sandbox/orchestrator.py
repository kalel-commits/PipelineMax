from .chaos_injector import ChaosInjector
import random

class SandboxOrchestrator:
    def __init__(self):
        self.chaos = ChaosInjector()

    def execute_with_chaos(self, branch, target_flow):
        scenario = self.chaos.inject(target_flow)
        
        # Simulate execution
        print(f"Executing branch {branch} in sandbox with chaos '{scenario}'")
        
        # If it's a malformed payload and auth flow, simulate failure
        if target_flow == "Authentication Flow" and scenario == "malformed_payload":
            return 1, f"Unhandled exception parsing payload in {target_flow}"
        
        # Randomly succeed or fail for demo purposes
        if random.random() > 0.8:
            return 1, f"Failed under stress scenario: {scenario}"
            
        return 0, "Execution succeeded."
