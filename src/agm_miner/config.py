from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="agm-miner", alias="APP_NAME")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    github_token: str = Field(alias="GITHUB_TOKEN")

    database_url: str = Field(alias="DATABASE_URL")
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="research_user", alias="POSTGRES_USER")
    postgres_password: str = Field(default="research_password", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="agent_artifacts_research", alias="POSTGRES_DB")

    raw_data_dir: str = Field(default="data/raw", alias="RAW_DATA_DIR")
    gold_data_dir: str = Field(default="data/gold", alias="GOLD_DATA_DIR")

    core_rate_limit_min_remaining: int = Field(
        default=50, alias="CORE_RATE_LIMIT_MIN_REMAINING"
    )
    search_rate_limit_min_remaining: int = Field(
        default=2, alias="SEARCH_RATE_LIMIT_MIN_REMAINING"
    )
    rate_limit_sleep_buffer_seconds: int = Field(
        default=5, alias="RATE_LIMIT_SLEEP_BUFFER_SECONDS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
