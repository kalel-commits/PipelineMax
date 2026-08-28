import httpx
import pytest
from integrations import github_client as gc_module
from integrations.github_client import (
    GitHubClient,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubAPIError,
)


class _FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data or []
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so tests never hit the real network."""

    def __init__(self, responder, **kwargs):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None, params=None):
        return self._responder("GET", url, params)

    async def post(self, url, headers=None, json=None):
        return self._responder("POST", url, json)


def _patch_client(monkeypatch, responder):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(responder, **kwargs)

    monkeypatch.setattr(gc_module.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_get_pull_request_files_filters_and_returns(monkeypatch):
    def responder(method, url, params):
        return _FakeResponse(200, [
            {"filename": "backend/app.py", "patch": "+x = 1", "status": "modified", "additions": 1, "deletions": 0},
            {"filename": "assets/logo.png", "patch": "", "status": "added"},
            {"filename": "node_modules/lib/x.js", "patch": "+y", "status": "modified"},
        ])

    _patch_client(monkeypatch, responder)
    client = GitHubClient(token="t")
    files = await client.get_pull_request_files("o", "r", 1)
    assert len(files) == 1
    assert files[0]["filename"] == "backend/app.py"


@pytest.mark.asyncio
async def test_auth_error_raised_on_401(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, p: _FakeResponse(401))
    client = GitHubClient(token="bad")
    with pytest.raises(GitHubAuthError):
        await client.get_pull_request_files("o", "r", 1)


@pytest.mark.asyncio
async def test_rate_limit_error_raised_on_403_with_zero_remaining(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, p: _FakeResponse(403, headers={"X-RateLimit-Remaining": "0"}))
    client = GitHubClient(token="t")
    with pytest.raises(GitHubRateLimitError):
        await client.get_pull_request_files("o", "r", 1)


@pytest.mark.asyncio
async def test_not_found_error_raised_on_404(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, p: _FakeResponse(404))
    client = GitHubClient(token="t")
    with pytest.raises(GitHubNotFoundError):
        await client.get_pull_request_files("o", "r", 1)


@pytest.mark.asyncio
async def test_set_commit_status_returns_false_on_failure_without_raising(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, p: _FakeResponse(500))
    client = GitHubClient(token="t")
    ok = await client.set_commit_status("o", "r", "sha", "success", "done")
    assert ok is False


@pytest.mark.asyncio
async def test_set_commit_status_returns_true_on_success(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, p: _FakeResponse(201, {}))
    client = GitHubClient(token="t")
    ok = await client.set_commit_status("o", "r", "sha", "success", "done")
    assert ok is True
