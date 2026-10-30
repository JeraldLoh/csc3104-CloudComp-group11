"""Settings for the Easy-read Service, read from environment variables."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # The Ollama server (its own container: `ollama` in compose and in the cluster).
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/"))
    # A small instruction model, because the cluster has no GPU: about 1 GB, a few seconds per route on 2 vCPUs.
    # Locally a larger one (e.g. llama3.2:3b) writes better text if the machine has the memory.
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"))
    # A whole route is one request; CPU inference of ~10 steps can take most of a minute.
    ollama_timeout_s: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT_S", "60")))
    # How long Ollama keeps the model in memory after a request, so the next one doesn't wait for it to load.
    ollama_keep_alive: str = field(default_factory=lambda: os.getenv("OLLAMA_KEEP_ALIVE", "30m"))
    # Longest wording for one step; the caregiver app's text box allows the same.
    max_chars: int = field(default_factory=lambda: int(os.getenv("EASYREAD_MAX_CHARS", "300")))


settings = Settings()
