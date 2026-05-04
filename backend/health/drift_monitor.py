import json

class DriftMonitor:
    def __init__(self):
        self.failure_counts = {}

    def log_failure(self, flow):
        if flow not in self.failure_counts:
            self.failure_counts[flow] = 0
        self.failure_counts[flow] += 1
        
        if self.failure_counts[flow] > 5:
            self.alert_drift(flow)

    def alert_drift(self, flow):
        print(f"🚨 SYSTEMIC DRIFT ALERT 🚨")
        print(f"Module '{flow}' has failed Chaos simulation 5+ times recently.")
        print(f"Technical debt or architectural drift likely. Requires engineering leadership review.")

    def get_stats(self):
        return json.dumps(self.failure_counts, indent=2)
