"""Runtime configuration. Every knob the acceptance criteria name is settable."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEPJ_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./mep_judge.db"
    redis_url: str = "redis://localhost:6379"

    # Zoning. MAX_ELEMENTS is the merge ceiling; a zone bigger than this is a
    # zone a reviewer cannot hold in their head.
    max_elements_per_zone: int = 800
    grid_fallback_m: float = 6.0
    zone_buffer_m: float = 2.0

    # Resolver
    max_attempts_per_clash: int = 3
    worker_concurrency: int = 4

    # Speckle. Absent credentials degrade the store to the local backend and
    # say so; they never silently disable the read-only guarantee.
    speckle_host: str = "http://localhost:3000"
    speckle_token: str | None = None

    artifacts_dir: str = "./artifacts/mep-judge"
    upload_dir: str = "./var/uploads"
    build_sha: str = "dev"

    # Auth
    bootstrap_api_key: str | None = None

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
