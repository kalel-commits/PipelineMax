"""Single source of truth for runtime configuration.

Reads the same environment variables the FastAPI app reads (loading ``backend/.env``
if present) so the CLI and the webhook server behave identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_once() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_BACKEND_DIR / ".env")


@dataclass
class Config:
    github_token: str = ""
    webhook_secret: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"
    agent_timeout_seconds: float = 10.0
    allowed_origins: list[str] = field(default_factory=list)
    port: int = 8000
    lexicon_path: Path = _BACKEND_DIR / "data" / "regression_lexicon.json"

    # --- derived / convenience --------------------------------------------------
    @property
    def github_authenticated(self) -> bool:
        return bool(self.github_token)

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def signature_enforced(self) -> bool:
        return bool(self.webhook_secret)

    @property
    def llm_provider(self) -> str:
        if not self.openai_base_url:
            return "openai"
        if "generativelanguage.googleapis" in self.openai_base_url:
            return "gemini (openai-compatible)"
        return "openai-compatible endpoint"

    def redacted(self) -> dict:
        """Config for display — secret *values* replaced with a set/unset marker."""
        def mark(v: str) -> str:
            return f"set ({len(v)} chars)" if v else "unset"

        return {
            "GITHUB_TOKEN": mark(self.github_token),
            "WEBHOOK_SECRET": mark(self.webhook_secret),
            "OPENAI_API_KEY": mark(self.openai_api_key),
            "OPENAI_BASE_URL": self.openai_base_url or "unset",
            "OPENAI_MODEL": self.openai_model,
            "AGENT_TIMEOUT_SECONDS": self.agent_timeout_seconds,
            "ALLOWED_ORIGINS": self.allowed_origins or "unset",
            "PORT": self.port,
            "lexicon_path": str(self.lexicon_path),
        }


def load_config() -> Config:
    _load_dotenv_once()
    try:
        timeout = float(os.getenv("AGENT_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10.0
    try:
        port = int(os.getenv("PORT", "8000"))
    except ValueError:
        port = 8000
    return Config(
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        agent_timeout_seconds=timeout,
        allowed_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()],
        port=port,
    )
