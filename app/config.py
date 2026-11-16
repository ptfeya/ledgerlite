from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://ledgerlite:ledgerlite@localhost:5432/ledgerlite"
    # No default on purpose: the app must not start with a guessable signing key.
    jwt_secret: str = Field(min_length=16)
    jwt_ttl_seconds: int = 3600
    seed_password: str = "ledgerlite-dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
