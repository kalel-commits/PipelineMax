import asyncio

from .github_client import GitHubAPIError, GitHubAuthError, GitHubNotFoundError, GitHubRateLimitError

_ERROR_PREFIX = {
    GitHubAuthError: "GitHub auth failed",
    GitHubRateLimitError: "GitHub rate limit hit",
    GitHubNotFoundError: "Repo/PR not found",
}


class WebhookHandler:
    """Real end-to-end orchestration for a single PR event: fetch the changed
    files from GitHub, run them through the risk agent, publish the verdict as a
    commit status. Network/GitHub failures are surfaced as an 'error' status
    instead of crashing the background task or silently defaulting to ALLOW."""

    def __init__(self, github_client, risk_agent, github_checks):
        self.github_client = github_client
        self.risk_agent = risk_agent
        self.github_checks = github_checks
        self._enrich_tasks: set = set()  # keep refs so fire-and-forget enrich isn't GC'd

    async def handle_pr_event(self, payload: dict) -> dict:
        pr = payload["pull_request"]
        repo_data = payload["repository"]
        owner = repo_data["owner"]["login"]
        repo = repo_data["name"]
        pull_number = pr["number"]
        sha = pr["head"]["sha"]

        # The "pending" status and the PR-files fetch are independent GitHub calls
        # with no ordering dependency -- overlap them instead of paying two serial
        # round trips. set_pending never raises (returns bool), so it's safe to
        # let it run alongside and reap it afterwards.
        pending_task = asyncio.ensure_future(self.github_checks.set_pending(owner, repo, sha))

        try:
            files = await self.github_client.get_pull_request_files(owner, repo, pull_number)
        except GitHubAPIError as e:
            await pending_task
            prefix = _ERROR_PREFIX.get(type(e), "GitHub API error")
            await self.github_checks.publish_error(owner, repo, sha, f"{prefix}: {e}")
            return {"verdict": "ERROR", "reason": str(e)}

        pr_data = {"id": pull_number, "branch": pr.get("head", {}).get("ref", "main"), "files": files}

        # Deterministic gate first, then publish the merge status immediately. The
        # LLM remediation call (BLOCK only, ~1-2s) does not feed the commit status
        # and never changes the verdict, so it runs afterwards as a detached task:
        # it must not sit on the path between the webhook and the gate landing on
        # the PR, and a slow/failing LLM must not turn a delivered verdict into an
        # error via the caller's timeout.
        verdict = self.risk_agent.decide(pr_data)
        await pending_task
        await self.github_checks.publish_verdict(owner, repo, sha, verdict)

        if verdict["verdict"] == "BLOCK":
            task = asyncio.ensure_future(self._enrich(owner, repo, sha, verdict, pr_data))
            self._enrich_tasks.add(task)
            task.add_done_callback(self._enrich_tasks.discard)

        return verdict

    async def _enrich(self, owner, repo, sha, verdict, pr_data) -> None:
        try:
            await self.risk_agent.enrich(verdict, pr_data)
            suggestion = verdict.get("suggestion", {})
            if suggestion.get("summary"):
                print(f"--- Remediation for {owner}/{repo}@{sha[:7]} "
                      f"({suggestion.get('source')}): {suggestion['summary']} ---")
        except Exception as e:  # pragma: no cover - defensive; verdict is already on the PR
            print(f"[WebhookHandler] remediation enrichment failed for {owner}/{repo}@{sha[:7]}: {e}")
