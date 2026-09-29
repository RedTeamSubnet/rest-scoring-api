import os
from typing import Literal

from typing_extensions import Self
from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from redteam_core.config import BaseConfig, ENV_PREFIX_SCORING_API


class ScoringApiMainConfig(BaseConfig):
    PORT: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Port used by the scoring API health server",
    )
    CORE_API_URL: str = Field(
        default="",
        description="Base URL for rest-core-api submission reads",
    )
    CORE_API_KEY: str = Field(
        default="",
        description="X-API-KEY used for rest-core-api requests",
        repr=False,
    )
    POLL_INTERVAL: int = Field(
        default=1200,
        ge=1,
        description="Seconds between scoring passes",
    )
    LOGGING_LEVEL: Literal["INFO", "DEBUG", "TRACE"] = Field(
        default="INFO",
        description="Application logging level (INFO, DEBUG, or TRACE)",
    )
    CACHE_DIR: str = Field(
        default="/var/lib/rest-scoring-api/cache", description="Cache directory path"
    )
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX_SCORING_API)

    @model_validator(mode="after")
    def validate_cache_dir(self) -> Self:
        """Ensure cache directory exists and is writable."""
        expanded = os.path.expanduser(self.CACHE_DIR)
        os.makedirs(expanded, exist_ok=True)
        if not os.access(expanded, os.W_OK):
            raise ValueError(f"Cache directory not writable: {expanded}")
        return self


__all__ = ["ScoringApiMainConfig"]
