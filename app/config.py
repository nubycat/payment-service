from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    name: str = "candidate-service"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/candidate",
        validation_alias="DATABASE_URL",
    )

    provider_url: str = Field(
        default="http://provider-simulator:8081",
        validation_alias="PROVIDER_URL",
    )
    provider_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        validation_alias="PROVIDER_TIMEOUT",
    )
    dispatch_polling_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        validation_alias="DISPATCH_POLLING_INTERVAL",
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in _LOG_LEVELS:
            allowed = ", ".join(sorted(_LOG_LEVELS))
            raise ValueError(f"log level must be one of: {allowed}")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
