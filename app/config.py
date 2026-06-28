"""Application configuration, loaded from environment / `.env`."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # External API auth — comma-separated list of accepted client keys.
    api_keys: str = "sk-harness-changeme"

    # Web login — comma-separated "user:password" pairs (e.g. "alice:secret").
    # Empty means no one can log in through the web UI (programmatic X-API-Key
    # access still works).
    auth_users: str = ""
    # Comma-separated usernames seeded as admins on first boot (when the DB user
    # row is created). After that, roles are managed in the DB / admin panel.
    admin_users: str = ""
    # PBKDF2 iteration count for password hashing (DB-backed users).
    pbkdf2_iterations: int = 200_000
    # Secret used to sign session tokens. Falls back to `api_keys` when unset;
    # set an explicit value in production.
    auth_secret: str | None = None
    # Session-token lifetime in seconds (default 7 days).
    auth_token_ttl_seconds: int = 7 * 24 * 3600
    # Default for self-registration when the DB has no stored value. Admins flip
    # the live setting from the admin panel (persisted in the app_settings table);
    # this only seeds the initial state. Off by default for private deployments.
    registration_enabled: bool = False

    # Defaults
    default_llm_provider: str = "anthropic"
    default_search_backend: str = "duckduckgo"
    default_anthropic_model: str = "claude-opus-4-8"
    default_openai_model: str = "gpt-4o"
    max_tokens: int = 16000

    # Agent tool-use loop budget (how many tool round-trips before the model is
    # forced to answer). Deep research gets a larger budget.
    max_iterations: int = 6
    deep_research_iterations: int = 16

    # Chat history (per-user conversation persistence). Disable to keep the API
    # fully stateless (chat still works; history endpoints return 404).
    chat_history_enabled: bool = True
    chat_db_path: str = "data/chat_history.db"

    # LLM provider keys
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # "local" provider (OpenAI-compatible server such as Ollama / vLLM)
    local_openai_base_url: str | None = None
    local_openai_api_key: str = "not-needed"
    local_model: str = "llama3.1"
    # Whether the local model accepts OpenAI-style image_url content (vision).
    # Off by default since many local text models reject image parts; set true
    # for vision-capable models (e.g. Qwen-VL, LLaVA) served over an
    # OpenAI-compatible API.
    local_supports_vision: bool = False

    # Search provider keys / config
    searxng_url: str | None = None
    brave_api_key: str | None = None
    tavily_api_key: str | None = None
    serper_api_key: str | None = None
    perplexity_api_key: str | None = None
    google_api_key: str | None = None
    google_cse_id: str | None = None

    request_timeout: float = 30.0

    # Attachments (inline base64 in the chat request)
    max_request_bytes: int = 25 * 1024 * 1024  # reject oversized chat bodies
    max_document_chars: int = 50_000  # extracted text cap per document
    allowed_image_types: str = "image/png,image/jpeg,image/webp,image/gif"
    allowed_document_types: str = (
        "application/pdf,text/plain,text/markdown,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    # fetch_url tool
    fetch_timeout: float = 15.0
    fetch_max_bytes: int = 2 * 1024 * 1024  # cap downloaded bytes per page
    fetch_max_chars: int = 40_000  # cap text returned to the model
    fetch_user_agent: str = "OnebixHarness/0.1 (+fetch_url)"
    fetch_block_private_ips: bool = True  # SSRF guard

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def allowed_image_type_set(self) -> set[str]:
        return {t.strip() for t in self.allowed_image_types.split(",") if t.strip()}

    @property
    def allowed_document_type_set(self) -> set[str]:
        return {t.strip() for t in self.allowed_document_types.split(",") if t.strip()}

    @property
    def auth_user_map(self) -> dict[str, str]:
        """Parse `auth_users` ("user:pass,user2:pass2") into {user: pass}."""
        users: dict[str, str] = {}
        for pair in self.auth_users.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            user, _, password = pair.partition(":")
            user = user.strip()
            if user:
                users[user] = password
        return users

    @property
    def admin_user_set(self) -> set[str]:
        """Usernames seeded as admins (from `admin_users`)."""
        return {u.strip() for u in self.admin_users.split(",") if u.strip()}

    @property
    def auth_db_path(self) -> str:
        """SQLite file backing the user + usage tables (shares the chat DB)."""
        return self.chat_db_path

    @property
    def token_secret(self) -> str:
        """Signing secret for session tokens (falls back to `api_keys`)."""
        return self.auth_secret or self.api_keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
