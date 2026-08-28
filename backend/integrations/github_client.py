import httpx
from typing import List, Dict, Any, Optional

GITHUB_API = "https://api.github.com"

# Files we skip: not source code, or GitHub already renders them unreadable as text.
_SKIP_EXTENSIONS = {
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".eot", ".pdf", ".zip", ".tar", ".gz", ".mp4", ".mp3", ".db", ".sqlite3",
}
_SKIP_PATH_SEGMENTS = ("node_modules/", "vendor/", ".git/", "dist/", "build/", "__pycache__/")

# Hard caps so one huge PR can't stall the guardrail or blow past GitHub rate limits.
MAX_FILES_ANALYZED = 50
MAX_PATCH_CHARS = 4000


class GitHubAPIError(Exception):
    """Base class for GitHub API failures the caller should react to explicitly."""


class GitHubAuthError(GitHubAPIError):
    pass


class GitHubNotFoundError(GitHubAPIError):
    pass


class GitHubRateLimitError(GitHubAPIError):
    pass


def _is_analyzable(filename: str) -> bool:
    lower = filename.lower()
    if any(seg in lower for seg in _SKIP_PATH_SEGMENTS):
        return False
    for ext in _SKIP_EXTENSIONS:
        if lower.endswith(ext):
            return False
    return True


def _raise_for_status(resp: httpx.Response, context: str) -> None:
    if resp.status_code == 401:
        raise GitHubAuthError(f"{context}: invalid or missing GitHub token")
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubRateLimitError(f"{context}: GitHub API rate limit exceeded")
    if resp.status_code == 403:
        raise GitHubAuthError(f"{context}: forbidden (token lacks required scope)")
    if resp.status_code == 404:
        raise GitHubNotFoundError(f"{context}: repository, PR, or file not found")
    if resp.status_code >= 400:
        raise GitHubAPIError(f"{context}: GitHub API returned {resp.status_code}")


class GitHubClient:
    """Thin async wrapper around the GitHub REST API calls the guardrail needs."""

    def __init__(self, token: str = "", timeout: float = 10.0):
        self.token = token
        self.timeout = timeout
        # One pooled client, reused for every call. A fresh httpx.AsyncClient per
        # request meant a new TCP + TLS handshake to api.github.com every time;
        # the guardrail makes 3 calls per PR (set_pending, get files, publish
        # verdict), so reuse collapses ~6 handshakes into 1 warm connection.
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Accept": "application/vnd.github.v3+json"},
                limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not getattr(self._client, "is_closed", False):
            await self._client.aclose()
        self._client = None

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def get_pull_request_files(self, owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
        """Fetch changed files for a PR, each with its filename and unified diff patch.

        Paginates until MAX_FILES_ANALYZED is reached or GitHub stops returning pages.
        Raises GitHubAPIError subclasses on auth/rate-limit/not-found so callers can
        distinguish "PR touched no analyzable files" from "we couldn't ask GitHub".
        """
        files: List[Dict[str, Any]] = []
        page = 1
        client = self._get_client()
        while len(files) < MAX_FILES_ANALYZED:
            url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/files"
            try:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params={"per_page": 100, "page": page},
                )
            except httpx.TimeoutException as e:
                raise GitHubAPIError(f"Timed out fetching PR files: {e}") from e
            except httpx.NetworkError as e:
                raise GitHubAPIError(f"Network error fetching PR files: {e}") from e

            _raise_for_status(resp, "get_pull_request_files")
            batch = resp.json()
            if not batch:
                break

            for entry in batch:
                filename = entry.get("filename", "")
                if not _is_analyzable(filename):
                    continue
                patch = entry.get("patch", "")  # binary/renamed-only files omit "patch"
                if len(patch) > MAX_PATCH_CHARS:
                    patch = patch[:MAX_PATCH_CHARS] + "\n# ...(patch truncated for analysis)"
                files.append(
                    {
                        "filename": filename,
                        "patch": patch,
                        "status": entry.get("status", "modified"),
                        "additions": entry.get("additions", 0),
                        "deletions": entry.get("deletions", 0),
                    }
                )
                if len(files) >= MAX_FILES_ANALYZED:
                    break

            if len(batch) < 100:
                break
            page += 1

        return files

    async def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,
        description: str,
        context: str = "PipelineAI / Guardrail",
        target_url: Optional[str] = None,
    ) -> bool:
        """Set a GitHub commit status. Returns True on success, False on failure
        (never raises — a status-post failure shouldn't crash the webhook handler,
        but the caller can check the return value and log accordingly)."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{sha}"
        data = {
            "state": state,  # pending, success, failure, error
            "description": description[:140],
            "context": context,
        }
        if target_url:
            data["target_url"] = target_url

        try:
            resp = await self._get_client().post(url, headers=self._headers(), json=data)
            _raise_for_status(resp, "set_commit_status")
            return True
        except Exception as e:
            print(f"[GitHubClient] Failed to set commit status: {e}")
            return False
