from typing import Optional
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from redteam_core.config import BaseConfig, ENV_PREFIX_SCORING_API


class ScoringApiMainConfig(BaseConfig):
    WALLET_NAME: str = Field(
        default="validator", description="Name of the wallet to use for validation"
    )
    HOTKEY_NAME: str = Field(
        default="default", description="Name of the hotkey to use for validation"
    )
    HOTKEY_ADDRESS: Optional[str] = Field(
        default=None,
        description="SS58 address of the hotkey to use for validation (overrides HOTKEY_NAME if set)",
    )
    UID: int = Field(
        default=-1,
        description="UID of the validator (overrides automatic detection if set)",
    )
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX_SCORING_API)


__all__ = ["ScoringApiMainConfig"]
