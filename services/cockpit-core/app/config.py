from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    tz: str = "Europe/Rome"

    redis_password: str = ""
    redis_host: str = "redis"
    redis_port: int = 6379

    database_url: str = "postgresql://lifecockpit:change-this-postgres-password@postgres:5432/lifecockpit"

    openrouter_api_key: str = ""
    openrouter_model: str = "qwen/qwen3-next-80b-a3b-instruct:free"
    openrouter_free_models: str = "qwen/qwen3-next-80b-a3b-instruct:free"
    openrouter_timeout_seconds: int = 45
    openrouter_temperature: float = 0.2
    openrouter_max_tokens: int = 700

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2"

    privacy_node_url: str = "http://privacy-node:8100"

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    rag_collection_name: str = "life_cockpit_memory"
    rag_vector_size: int = 384
    rag_chunk_size_chars: int = 1100
    rag_chunk_overlap_chars: int = 180
    rag_semantic_similarity_threshold: float = 0.72
    rag_default_top_k: int = 6
    rag_dense_weight: float = 0.7
    rag_sparse_weight: float = 0.3
    rag_agentic_chunk_max_chars: int = 16000
    rag_query_candidates: int = 24
    rag_rerank_candidates: int = 12

    cockpit_worker_concurrency: int = 4
    smart_buffering_enabled: bool = True
    smart_buffer_seconds: int = 12
    smart_buffer_ttl_seconds: int = 120
    loop_block_from_me: bool = True
    allow_local_degraded_mode: bool = True
    circuit_breaker_failure_threshold: int = 4
    circuit_breaker_open_seconds: int = 90
    dead_letter_enabled: bool = True
    semantic_cache_enabled: bool = True
    semantic_cache_ttl_seconds: int = 300
    dead_letter_anomaly_window_minutes: int = 15
    dead_letter_anomaly_threshold: int = 3
    dead_letter_alert_cooldown_seconds: int = 900

    proactive_default_user_id: str = "marco"
    proactive_notify_whatsapp_enabled: bool = True

    evolution_api_url: str = "http://evolution-api:8080"
    evolution_api_key: str = ""
    evolution_instance: str = ""
    proactive_whatsapp_number: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_url: str = ""
    google_oauth_scopes: str = (
        "openid,email,profile,"
        "https://www.googleapis.com/auth/gmail.readonly,"
        "https://www.googleapis.com/auth/drive.readonly,"
        "https://www.googleapis.com/auth/calendar.readonly"
    )
    google_sync_http_timeout_seconds: int = 45
    google_gmail_bootstrap_max_results: int = 100
    google_gmail_history_page_size: int = 100
    google_drive_bootstrap_page_size: int = 50
    google_drive_download_max_bytes: int = 1500000
    google_calendar_bootstrap_past_days: int = 90
    google_calendar_bootstrap_future_days: int = 180
    google_calendar_bootstrap_page_size: int = 250

    @property
    def redis_broker_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def redis_result_backend(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/1"
        return f"redis://{self.redis_host}:{self.redis_port}/1"

    @property
    def openrouter_models(self) -> list[str]:
        raw = [item.strip() for item in self.openrouter_free_models.split(",")]
        parsed = [item for item in raw if item]
        if not parsed:
            parsed = [self.openrouter_model]
        free_only = [item for item in parsed if item.endswith(":free")]
        return free_only or ["qwen/qwen3-next-80b-a3b-instruct:free"]

    @property
    def google_scopes(self) -> list[str]:
        raw = [item.strip() for item in self.google_oauth_scopes.split(",")]
        return [item for item in raw if item]


settings = Settings()
