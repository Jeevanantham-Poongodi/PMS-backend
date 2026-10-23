from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration. Business thresholds do not belong here (constitution VIII)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres connection used by the API. Each user request switches to the `authenticated`
    # role with the verified token's claims, so RLS applies (research R1).
    database_url: str | None = None

    supabase_url: str | None = None
    # "jwks": project uses asymmetric signing keys; "secret": legacy shared JWT secret.
    supabase_jwt_mode: Literal["jwks", "secret"] = "jwks"
    supabase_jwt_secret: str | None = None
    supabase_jwt_audience: str = "authenticated"
    # Service role key. Used only by operator scripts (scripts/bootstrap.py), never by requests.
    supabase_key: str | None = None

    cors_origins: str = "http://localhost:5173"

    # Demo deployments only: privileged roles may sign in without MFA (relaxes FR-008).
    demo_mode: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jwt_issuer(self) -> str | None:
        return f"{self.supabase_url.rstrip('/')}/auth/v1" if self.supabase_url else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
