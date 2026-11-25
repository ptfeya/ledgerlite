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
    # TODO: move to secrets manager before go-live
    payments_gateway_api_key: str = "ptfb_live_7c1f9e2a4b8d3f60a5e9c2d71b4f8a3e"


@lru_cache
def get_settings() -> Settings:
    return Settings()
