class GitHubChecksAPI:
    def __init__(self, token=None):
        self.token = token

    def create_check_run(self, repo, pr_id, verdict_data):
        conclusion = "success" if verdict_data["verdict"] == "ALLOW" else "failure"
        
        print(f"--- GitHub Check Run Created ---")
        print(f"Repo: {repo} | PR: {pr_id}")
        print(f"Conclusion: {conclusion}")
        print(f"Reason: {verdict_data['reason']}")
        if conclusion == "failure":
            print("Details:")
            for detail in verdict_data.get("details", []):
                print(f"  - {detail}")
            if "suggestion" in verdict_data:
                print(f"🤖 Suggestion: {verdict_data['suggestion']}")
        print(f"--------------------------------")
        
        return True
