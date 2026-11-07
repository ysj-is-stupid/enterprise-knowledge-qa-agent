"""Runtime configuration shared by the CLI and the future agent executor."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Validated runtime limits and model settings."""

    mode: str = "offline"
    max_decision_steps: int = 10
    max_tool_calls: int = 8
    max_same_action_failures: int = 2
    retry_base_delay_seconds: float = 0.25
    retry_max_delay_seconds: float = 2.0
    search_top_k: int = 5
    search_min_score: float = 0.18
    conflict_score_margin: float = 0.15
    history_message_limit: int = 6
    history_memory_limit: int = 6
    max_claims: int = 6
    max_claim_chars: int = 240
    max_total_claim_chars: int = 800
    request_timeout_seconds: int = 30
    api_key: str | None = None
    base_url: str | None = None
    model_name: str | None = None

    @classmethod
    def from_environment(cls, dotenv_path: str | Path = ".env") -> "AgentConfig":
        """Load non-secret defaults from environment variables.

        Secrets are retained only in memory and must never be serialized to Trace.
        """

        _load_dotenv(dotenv_path)
        timeout = int(
            os.getenv("KNOWLEDGE_QA_TIMEOUT_SECONDS")
            or os.getenv("DEEPSEEK_TIMEOUT_SECONDS")
            or "30"
        )
        return cls(
            request_timeout_seconds=timeout,
            api_key=(os.getenv("KNOWLEDGE_QA_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or None),
            base_url=(
                os.getenv("KNOWLEDGE_QA_BASE_URL")
                or os.getenv("DEEPSEEK_BASE_URL")
                or None
            ),
            model_name=(os.getenv("KNOWLEDGE_QA_MODEL") or os.getenv("DEEPSEEK_MODEL") or None),
        )

    def public_summary(self) -> dict[str, object]:
        """Return the safe subset allowed in diagnostics and Trace."""

        return {
            "mode": self.mode,
            "max_decision_steps": self.max_decision_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_same_action_failures": self.max_same_action_failures,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "search_top_k": self.search_top_k,
            "search_min_score": self.search_min_score,
            "conflict_score_margin": self.conflict_score_margin,
            "history_message_limit": self.history_message_limit,
            "history_memory_limit": self.history_memory_limit,
            "max_claims": self.max_claims,
            "max_claim_chars": self.max_claim_chars,
            "max_total_claim_chars": self.max_total_claim_chars,
            "request_timeout_seconds": self.request_timeout_seconds,
            "api_key_configured": bool(self.api_key),
            "base_url_configured": bool(self.base_url),
            "model_name": self.model_name,
        }


def _load_dotenv(path: str | Path) -> None:
    """Load a minimal KEY=VALUE file without adding a third-party dependency."""

    source = Path(path)
    if not source.is_file():
        return
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
