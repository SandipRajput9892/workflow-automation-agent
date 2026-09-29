"""Central configuration, loaded from environment variables / .env."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Claude ---------------------------------------------------------
    # The SDK also resolves ANTHROPIC_API_KEY itself; this is only read so we
    # can fail fast with a clear message.
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5"
    claude_max_tokens: int = 16000
    # Server-side refusal fallback: if the primary model declines a request,
    # the API re-runs it on a fallback model within the same call.
    enable_refusal_fallback: bool = True

    # --- LLM provider ---------------------------------------------------
    # "anthropic" = Claude (settings above); "groq" = Groq's OpenAI-compatible API
    llm_provider: Literal["anthropic", "groq"] = "anthropic"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_max_tokens: int = 8000
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # --- Workflow limits ------------------------------------------------
    max_steps: int = 15
    # Retries per step (each with Claude-adjusted input) before it is marked failed.
    executor_max_retries: int = 1
    # Times the reflection agent may send a corrective plan back for execution
    # before the run is declared failed.
    max_correction_rounds: int = 2
    # Calls allowed to get a valid plan / corrective plan from Claude (first
    # try + corrective retries) when the output is malformed or fails validation.
    max_output_attempts: int = 3
    # Times the planner may be invoked in one run (initial plan + replans the
    # supervisor asks for after a gate rejection or a failed approach).
    max_plan_requests: int = 3

    # --- Supervisor safety gate -----------------------------------------
    # When a human must approve a plan before it runs:
    #   never     - no human approval (supervisor's own review still applies)
    #   high_risk - only plans the supervisor rates high risk
    #   always    - every plan, including corrective plans
    approval_mode: Literal["never", "high_risk", "always"] = "high_risk"
    # Email domains considered internal; sending anywhere else is flagged as external.
    # Env var takes a JSON list, e.g. INTERNAL_DOMAINS=["ourco.com"]
    internal_domains: list[str] = []
    # A single email/invite with more recipients than this is "bulk" (high risk).
    bulk_recipient_threshold: int = 5
    # More outbound messages (emails + Slack posts) than this in one plan is high risk.
    bulk_message_threshold: int = 10

    # --- Memory ---------------------------------------------------------
    chroma_dir: Path = PROJECT_ROOT / "data" / "chroma"
    chroma_collection: str = "workflow_memory"
    # "default" = Chroma's built-in ONNX MiniLM model (downloaded on first use)
    # "hash"    = dependency-free offline embedding (good for tests / air-gapped)
    embedding_backend: str = "default"
    memory_top_k: int = 3
    # Past workflows farther than this cosine distance (0 = identical, 2 = opposite)
    # are considered unrelated and not shown to the planner.
    memory_max_distance: float = 0.6

    # --- Paths ----------------------------------------------------------
    mock_api_dir: Path = PROJECT_ROOT / "data" / "mock_apis"
    sample_workflows_path: Path = PROJECT_ROOT / "data" / "sample_workflows.json"
    # LangGraph checkpoints: every run's state after each step, so runs can be
    # resumed after a crash or while waiting for human approval.
    checkpoint_db: Path = PROJECT_ROOT / "data" / "checkpoints.sqlite"
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"

    # --- Backend API ----------------------------------------------------
    backend_database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'backend.sqlite').as_posix()}"
    # Workflows executing at once; more requests queue up.
    backend_max_concurrent_runs: int = 4
    # Origins allowed by CORS. ["*"] allows any origin (without credentials).
    # Env var takes a JSON list, e.g. CORS_ORIGINS=["http://localhost:5173"]
    cors_origins: list[str] = ["*"]

    @property
    def audit_log_path(self) -> Path:
        return self.log_dir / "audit_log.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_dir / "agent.log", encoding="utf-8"),
        ],
        force=True,
    )
    # Keep third-party noise down.
    for noisy in ("httpx", "httpx2", "chromadb", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
