"""Runtime configuration, loaded from the environment (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


# Default classification model per provider.
_DEFAULT_MODEL = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}


@dataclass(frozen=True)
class Config:
    database_url: str
    classify_provider: str  # 'gemini' | 'anthropic'
    anthropic_api_key: str | None
    gemini_api_key: str | None
    classify_model: str
    planit_contact: str

    @classmethod
    def from_env(cls) -> "Config":
        gemini_key = os.environ.get("GEMINI_API_KEY") or None
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or None

        # Explicit provider wins; otherwise infer from whichever key is present,
        # preferring Gemini.
        provider = os.environ.get("CLASSIFY_PROVIDER")
        if not provider:
            provider = "gemini" if gemini_key else "anthropic"

        model = os.environ.get("CLASSIFY_MODEL") or _DEFAULT_MODEL.get(
            provider, _DEFAULT_MODEL["gemini"]
        )

        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://localhost:5432/datacentre"
            ),
            classify_provider=provider,
            anthropic_api_key=anthropic_key,
            gemini_api_key=gemini_key,
            classify_model=model,
            planit_contact=os.environ.get("PLANIT_CONTACT", "unknown@example.com"),
        )


config = Config.from_env()
