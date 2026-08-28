import json
import pytest
from utils.ai_remediation import AIRemediation

ISSUES = [
    {
        "file": "backend/parser.py",
        "line": 4,
        "category": "error-handling",
        "rule_id": "bare_except",
        "description": "Bare except swallows errors.",
        "recommendation": "Catch a specific exception.",
    }
]


class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc

    async def create(self, **kwargs):
        if self._exc:
            raise self._exc
        return _FakeResponse(self._content)


class _FakeClient:
    def __init__(self, content=None, exc=None):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content, exc)})()


@pytest.mark.asyncio
async def test_no_api_key_uses_fallback_without_crashing():
    remediation = AIRemediation(api_key="")
    result = await remediation.suggest_fix(ISSUES, "some diff")
    assert result["source"] == "fallback"
    assert len(result["fixes"]) == 1


@pytest.mark.asyncio
async def test_no_issues_short_circuits():
    remediation = AIRemediation(api_key="")
    result = await remediation.suggest_fix([], "")
    assert result["source"] == "none"


@pytest.mark.asyncio
async def test_valid_openai_response_is_used():
    remediation = AIRemediation(api_key="fake-key")
    valid_json = json.dumps({"summary": "One issue found.", "fixes": [{"file": "backend/parser.py", "fix": "Catch ValueError."}]})
    remediation._client = _FakeClient(content=valid_json)
    result = await remediation.suggest_fix(ISSUES, "diff context")
    assert result["source"] == "openai"
    assert result["summary"] == "One issue found."


@pytest.mark.asyncio
async def test_malformed_json_falls_back():
    remediation = AIRemediation(api_key="fake-key")
    remediation._client = _FakeClient(content="not json at all {{{")
    result = await remediation.suggest_fix(ISSUES, "diff context")
    assert result["source"] == "fallback"
    assert "unusable response" in result["reason"]


@pytest.mark.asyncio
async def test_missing_required_keys_falls_back():
    remediation = AIRemediation(api_key="fake-key")
    remediation._client = _FakeClient(content=json.dumps({"foo": "bar"}))
    result = await remediation.suggest_fix(ISSUES, "diff context")
    assert result["source"] == "fallback"


@pytest.mark.asyncio
async def test_api_exception_falls_back():
    remediation = AIRemediation(api_key="fake-key")
    remediation._client = _FakeClient(exc=TimeoutError("timed out"))
    result = await remediation.suggest_fix(ISSUES, "diff context")
    assert result["source"] == "fallback"
    assert "TimeoutError" in result["reason"]
