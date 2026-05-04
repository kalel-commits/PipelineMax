import random

class ChaosInjector:
    def __init__(self):
        self.chaos_scenarios = [
            "dropped_db_connection",
            "malformed_payload",
            "high_latency",
            "null_pointers"
        ]

    def inject(self, target_flow):
        # Simulate injecting a failure payload based on the flow
        scenario = random.choice(self.chaos_scenarios)
        print(f"Injecting chaos scenario '{scenario}' into {target_flow}")
        return scenario
