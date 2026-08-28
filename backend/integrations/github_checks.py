from typing import Dict, Any


class GitHubChecksAPI:
    """Publishes a guardrail verdict as a real GitHub commit status (the check
    that gates the PR's merge button), via the shared GitHubClient."""

    def __init__(self, github_client):
        self.github_client = github_client

    async def set_pending(self, owner: str, repo: str, sha: str) -> bool:
        return await self.github_client.set_commit_status(owner, repo, sha, "pending", "Agents running...")

    async def publish_verdict(self, owner: str, repo: str, sha: str, verdict_data: Dict[str, Any]) -> bool:
        state = "success" if verdict_data["verdict"] == "ALLOW" else "failure"
        description = verdict_data["reason"]

        print(f"--- GitHub Check Run: {owner}/{repo}@{sha[:7]} ---")
        print(f"Verdict: {verdict_data['verdict']} | {description}")
        if verdict_data["verdict"] == "BLOCK":
            for detail in verdict_data.get("details", []):
                print(f"  - {detail}")
            suggestion = verdict_data.get("suggestion", {})
            if suggestion.get("summary"):
                print(f"  Suggestion ({suggestion.get('source')}): {suggestion['summary']}")
        print("-" * 40)

        return await self.github_client.set_commit_status(owner, repo, sha, state, description)

    async def publish_error(self, owner: str, repo: str, sha: str, message: str) -> bool:
        print(f"--- GitHub Check Run ERROR: {owner}/{repo}@{sha[:7]}: {message} ---")
        return await self.github_client.set_commit_status(owner, repo, sha, "error", message[:140])
