class MemoryAgent:
    def __init__(self):
        # In a real system, this connects to a Vector DB or Graph Database storing past failures
        self.lexicon = {
            "auth_parser": "Broke token validation by missing null check on payload."
        }

    def check_lexicon(self, files_changed):
        for f in files_changed:
            if "data_parser" in f:
                return "This parsing pattern has historically broken auth."
        return None

    def store_failure(self, pr_data, chaos_logs):
        # Learn from the failure
        print(f"Storing failure pattern for PR {pr_data.get('id', 'unknown')}")
        pass
