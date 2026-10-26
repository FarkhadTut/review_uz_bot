from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# drand "quicknet" (League of Entropy): unchained BLS beacon, one round every 3 seconds.
QUICKNET_CHAIN_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    # @username or numeric -100... id of the contest channel; the bot must be its administrator.
    channel_id: str = "@reviewuznews"
    channel_url: str = "https://t.me/reviewuznews"
    admin_ids: Annotated[list[int], NoDecode] = []

    db_path: str = "data/giveaway.db"

    drand_chain_hash: str = QUICKNET_CHAIN_HASH
    drand_relays: Annotated[list[str], NoDecode] = [
        "https://api.drand.sh",
        "https://drand.cloudflare.com",
    ]
    beacon_lead_seconds: int = 60

    claim_window_hours: int = 48
    retention_days: int = 30
    retention_checks_per_second: float = 20.0

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("drand_relays", mode="before")
    @classmethod
    def _split_urls(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip().rstrip("/") for x in v.split(",") if x.strip()]
        return v

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"
