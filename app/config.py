from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEBHOOK_URL_ERROR = "WEBHOOK_URL must be an absolute http:// or https:// URL"


class Settings(BaseSettings):
    # hide_input_in_errors keeps supplied values (secrets included) out of
    # validation messages.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "postgresql://ledgerlite:ledgerlite@localhost:5432/ledgerlite"
    # No default on purpose: the app must not start with a guessable signing key.
    jwt_secret: str = Field(min_length=16)
    jwt_ttl_seconds: int = 3600
    seed_password: str = "ledgerlite-dev"
    # Unset or blank disables webhooks: no new events are queued or delivered.
    webhook_url: str | None = None
    # Required whenever webhook_url is set; no default on purpose.
    webhook_signing_secret: SecretStr | None = Field(default=None, validate_default=True)

    @field_validator("webhook_url", mode="before")
    @classmethod
    def _blank_url_disables_webhooks(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("webhook_url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(char.isspace() or not char.isprintable() for char in value):
            raise ValueError(_WEBHOOK_URL_ERROR)
        try:
            parts = urlsplit(value)
            parts.port  # noqa: B018 - raises ValueError for an invalid port
        except ValueError:
            raise ValueError(_WEBHOOK_URL_ERROR) from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(_WEBHOOK_URL_ERROR)
        # Reject what the stdlib transport cannot send rather than fail at delivery:
        # http.client writes the request target as ASCII (percent-encode instead),
        # and the socket layer IDNA-encodes the host.
        if not (parts.path + parts.query).isascii():
            raise ValueError(_WEBHOOK_URL_ERROR)
        try:
            parts.hostname.encode("idna")
        except UnicodeError:
            raise ValueError(_WEBHOOK_URL_ERROR) from None
        return value

    @field_validator("webhook_signing_secret")
    @classmethod
    def _secret_required_when_enabled(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        if info.data.get("webhook_url") and not (value and value.get_secret_value().strip()):
            raise ValueError("WEBHOOK_SIGNING_SECRET is required when WEBHOOK_URL is set")
        return value

    @property
    def webhooks_enabled(self) -> bool:
        return self.webhook_url is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
