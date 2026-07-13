from pydantic import Field
from pydantic_settings import SettingsConfigDict

from redteam_core.config import BaseConfig, ENV_PREFIX_SCORING_API


class ScoringApiMainConfig(BaseConfig):
    WALLET_DIR: str = Field(
        default="~/.bittensor/wallets", description="Directory where wallets are stored"
    )
    WALLET_NAME: str = Field(
        default="scoring-api", description="Name of the wallet to use for validation"
    )
    HOTKEY_NAME: str = Field(
        default="default", description="Name of the hotkey to use for validation"
    )
    # HOTKEY_ADDRESS: Optional[str] = Field(
    #     default=None,
    #     description="SS58 address of the hotkey to use for validation (overrides HOTKEY_NAME if set)",
    # )
    UID: int = Field(
        default=-1,
        description="UID of the validator (overrides automatic detection if set)",
    )
    PORT: int = Field(default=8000, description="Port for the scoring API ping server")
    BATCH_LIMIT: int = Field(
        default=50,
        description="Maximum number of unscored commits to fetch per forward pass",
    )
    STORAGE_API_PREFIX: str = Field(
        default="/api/v1",
        description="Path prefix for storage API endpoints",
    )
    BASELINE_COMMIT_ID: str = Field(
        default="synthetic-baseline-aded41464b53d08fdadd4790dc9c8562133a6bbac4566e07fe8336b0ed67e8bc",
        description="Commit id used as the baseline comparison target",
    )
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX_SCORING_API)


__all__ = ["ScoringApiMainConfig"]
