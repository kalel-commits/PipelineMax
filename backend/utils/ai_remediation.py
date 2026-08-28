import json
from typing import List, Dict, Any

_SYSTEM_PROMPT = (
    "You are a CI/CD guardrail's remediation assistant. You are given a list of "
    "concrete, already-detected static-analysis issues from a GitHub pull request, "
    "each with a file, category, severity, and the offending line. "
    "Do not invent new issues and do not give generic advice unrelated to the list. "
    "For each issue, write a short, specific fix grounded in its 'evidence' line. "
    "Respond with ONLY a JSON object: "
    '{"summary": "<one sentence>", "fixes": [{"file": "...", "fix": "..."}]}'
)


def _fallback_suggestion(issues: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    """Deterministic, non-AI suggestion built straight from each issue's own
    recommendation field. Used whenever the LLM is unavailable, misconfigured,
    or returns something we can't trust — never leaves the caller without a
    suggestion, and never fabricates an issue that static analysis didn't find."""
    return {
        "source": "fallback",
        "reason": reason,
        "summary": f"{len(issues)} issue(s) detected by static analysis.",
        "fixes": [
            {"file": issue.get("file"), "fix": issue.get("recommendation", "Review this line manually.")}
            for issue in issues
        ],
    }


class AIRemediation:
    """Wraps the OpenAI Chat Completions API to turn detected issues into
    actionable, issue-grounded suggestions. Every failure mode (no key, timeout,
    rate limit, malformed response) degrades to the deterministic fallback above
    instead of raising or fabricating output."""

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini", timeout: float = 8.0,
                 base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url
        self._client = None
        if self.api_key:
            try:
                from openai import AsyncOpenAI

                # base_url lets this point at any OpenAI-compatible endpoint
                # (e.g. Gemini's OpenAI-compat API) without changing call sites.
                kwargs = {"api_key": self.api_key, "timeout": self.timeout}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = AsyncOpenAI(**kwargs)
            except ImportError:
                self._client = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def suggest_fix(self, issues: List[Dict[str, Any]], diff_context: str = "") -> Dict[str, Any]:
        if not issues:
            return {"source": "none", "summary": "No issues to remediate.", "fixes": []}

        if not self.configured:
            return _fallback_suggestion(issues, "OPENAI_API_KEY not configured")

        user_prompt = (
            "Detected issues (JSON):\n"
            + json.dumps(issues, indent=2)
            + "\n\nRelevant diff context (may be truncated):\n"
            + diff_context[:2000]
        )

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        except Exception as e:
            # Covers APITimeoutError, RateLimitError, APIConnectionError, AuthenticationError, etc.
            return _fallback_suggestion(issues, f"OpenAI API call failed: {type(e).__name__}: {e}")

        try:
            content = response.choices[0].message.content
            parsed = json.loads(content)
            if "summary" not in parsed or "fixes" not in parsed or not isinstance(parsed["fixes"], list):
                raise ValueError("response missing required keys")
        except (json.JSONDecodeError, KeyError, IndexError, ValueError, AttributeError) as e:
            return _fallback_suggestion(issues, f"OpenAI returned an unusable response: {e}")

        parsed["source"] = "openai"
        return parsed
