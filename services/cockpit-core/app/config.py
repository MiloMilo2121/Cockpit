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
    openrouter_model: str = "qwen/qwen3-32b:free"

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2"

    privacy_node_url: str = "http://privacy-node:8100"

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""

    cockpit_worker_concurrency: int = 4

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


settings = Settings()
