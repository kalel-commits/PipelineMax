class WebhookHandler:
    def __init__(self, risk_agent, github_checks):
        self.risk_agent = risk_agent
        self.github_checks = github_checks

    def handle_pr_event(self, payload):
        repo = payload.get("repository", {}).get("full_name")
        pr_id = payload.get("pull_request", {}).get("number")
        files_changed = payload.get("pull_request", {}).get("files_changed", [])
        branch = payload.get("pull_request", {}).get("head", {}).get("ref")
        
        pr_data = {
            "id": pr_id,
            "branch": branch,
            "files_changed": files_changed
        }
        
        verdict = self.risk_agent.evaluate_pr(pr_data)
        self.github_checks.create_check_run(repo, pr_id, verdict)
        
        return verdict
