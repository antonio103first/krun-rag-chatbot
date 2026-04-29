"""Configuration loader for the KRUN RAG chatbot.

Loads `config.yaml` and overlays environment variables from `.env`. Handles the
Windows ↔ WSL path quirk that comes up because the Obsidian vault lives on
`C:\\Users\\anton\\...` while the Python process may run from WSL where that
appears as `/mnt/c/Users/anton/...`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Project root -----------------------------------------------------------
# config.py lives in <root>/rag/config.py, so root is two parents up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


# --- Path utilities ---------------------------------------------------------
_WINDOWS_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/]")


def normalize_vault_path(raw: str) -> Path:
    """Convert a vault path to one usable from the current OS.

    On WSL/Linux, `C:\\Users\\anton\\...` is rewritten to `/mnt/c/Users/anton/...`.
    On Windows, paths are returned as-is.
    """
    if not raw:
        raise ValueError("vault path is empty")

    is_wsl = "microsoft" in os.uname().release.lower() if hasattr(os, "uname") else False
    is_linux = os.name == "posix"

    match = _WINDOWS_DRIVE_RE.match(raw)
    if match and (is_wsl or is_linux):
        drive = match.group(1).lower()
        rest = raw[match.end():].replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(raw)


# --- Sub-config models ------------------------------------------------------
class VaultConfig(BaseModel):
    path: str
    include_dirs: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)

    @property
    def resolved_path(self) -> Path:
        return normalize_vault_path(self.path)


class StorageConfig(BaseModel):
    lancedb_path: str = "./data/lancedb"
    table_name: str = "vault_chunks"

    @property
    def resolved_path(self) -> Path:
        p = Path(self.lancedb_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


class EmbeddingConfig(BaseModel):
    model_name: str = "BAAI/bge-m3"
    dimension: int = 1024
    batch_size: int = 32
    device: Literal["auto", "cpu", "cuda"] = "auto"
    max_seq_length: int = 8192


class ChunkingConfig(BaseModel):
    strategy: Literal["header_aware", "sliding"] = "header_aware"
    max_tokens: int = 800
    overlap_tokens: int = 80
    min_tokens: int = 50
    preserve_breadcrumb: bool = True


class BM25Config(BaseModel):
    tokenizer: Literal["char_trigram", "kiwipiepy", "lancedb_fts"] = "char_trigram"
    k1: float = 1.5
    b: float = 0.75


class RetrievalConfig(BaseModel):
    bm25_top_k: int = 30
    vector_top_k: int = 30
    rrf_k: int = 60
    final_top_k: int = 8
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"


class GenerationConfig(BaseModel):
    gen_model: str = "claude-sonnet-4-6"
    analyzer_model: str = "claude-haiku-4-5"
    max_tokens: int = 2048
    temperature: float = 0.2
    stream: bool = True
    prompt_caching: bool = True
    zdr_enabled: bool = False


class WatcherConfig(BaseModel):
    enabled: bool = False
    debounce_seconds: float = 2.0
    ignore_patterns: list[str] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


# --- Top-level settings -----------------------------------------------------
class Settings(BaseSettings):
    """Composite settings. Loads config.yaml, overlays env vars, exposes typed access."""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Secrets / overrides via env
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    zdr_enabled_env: bool = Field(default=False, alias="ZDR_ENABLED")
    vault_path_env: str | None = Field(default=None, alias="VAULT_PATH")
    lancedb_path_env: str | None = Field(default=None, alias="LANCEDB_PATH")
    log_level_env: str | None = Field(default=None, alias="LOG_LEVEL")
    claude_gen_model_env: str | None = Field(default=None, alias="CLAUDE_GEN_MODEL")
    claude_analyzer_model_env: str | None = Field(default=None, alias="CLAUDE_ANALYZER_MODEL")

    # Filled from yaml in `from_files`
    vault: VaultConfig = Field(default_factory=lambda: VaultConfig(path=""))
    storage: StorageConfig = StorageConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    chunking: ChunkingConfig = ChunkingConfig()
    bm25: BM25Config = BM25Config()
    retrieval: RetrievalConfig = RetrievalConfig()
    generation: GenerationConfig = GenerationConfig()
    watcher: WatcherConfig = WatcherConfig()
    logging_cfg: LoggingConfig = Field(default_factory=LoggingConfig, alias="logging")

    @field_validator("anthropic_api_key")
    @classmethod
    def _strip_key(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @classmethod
    def from_files(
        cls,
        config_path: Path | None = None,
        env_path: Path | None = None,
    ) -> "Settings":
        env_path = env_path or DEFAULT_ENV_PATH
        if env_path.exists():
            load_dotenv(env_path, override=False)

        config_path = config_path or DEFAULT_CONFIG_PATH
        yaml_data: dict = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f) or {}

        # Pydantic uses `logging` as alias for the LoggingConfig section.
        if "logging" in yaml_data:
            yaml_data["logging_cfg"] = yaml_data.pop("logging")

        settings = cls(**yaml_data)

        # Apply env overrides (env > yaml).
        if settings.vault_path_env:
            settings.vault.path = settings.vault_path_env
        if settings.lancedb_path_env:
            settings.storage.lancedb_path = settings.lancedb_path_env
        if settings.zdr_enabled_env:
            settings.generation.zdr_enabled = True
        if settings.log_level_env:
            settings.logging_cfg.level = settings.log_level_env
        if settings.claude_gen_model_env:
            settings.generation.gen_model = settings.claude_gen_model_env
        if settings.claude_analyzer_model_env:
            settings.generation.analyzer_model = settings.claude_analyzer_model_env

        return settings

    def ensure_runtime_dirs(self) -> None:
        """Create LanceDB / cache directories if missing."""
        self.storage.resolved_path.mkdir(parents=True, exist_ok=True)


_cached: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Module-level singleton accessor."""
    global _cached
    if _cached is None or reload:
        _cached = Settings.from_files()
    return _cached
