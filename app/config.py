from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "postgresql://ledgerlite:ledgerlite@localhost:5432/ledgerlite"
    # No default on purpose: the app must not start with a guessable signing key.
    jwt_secret: str = Field(min_length=16)
    jwt_ttl_seconds: int = 3600
    seed_password: str = "ledgerlite-dev"
    payments_gateway_api_key: SecretStr

    @field_validator("payments_gateway_api_key")
    @classmethod
    def require_nonblank_gateway_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
