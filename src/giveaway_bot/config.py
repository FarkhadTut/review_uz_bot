from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# drand "quicknet" (League of Entropy): unchained BLS beacon, one round every 3 seconds.
QUICKNET_CHAIN_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
QUICKNET_GENESIS = 1692803367
QUICKNET_PERIOD = 3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    # @username or numeric -100... id of the contest channel; the bot must be its administrator.
    channel_id: str = "@reviewuznews"
    channel_url: str = "https://t.me/reviewuznews"
    admin_ids: Annotated[list[int], NoDecode] = []

    db_path: str = "data/giveaway.db"

    drand_chain_hash: str = QUICKNET_CHAIN_HASH
    drand_genesis: int = QUICKNET_GENESIS
    drand_period: int = QUICKNET_PERIOD
    drand_relays: Annotated[list[str], NoDecode] = [
        "https://api.drand.sh",
        "https://api2.drand.sh",
        "https://api3.drand.sh",
        "https://drand.cloudflare.com",
    ]
    # Randomness is accepted only when this many relays return the same value.
    drand_min_agreeing: int = Field(default=2, ge=2)
    # How far ahead of the commitment post the drawn round lies. Too short and the post
    # could land after the round is already public.
    beacon_lead_seconds: int = Field(default=60, ge=60)  # PRD: at least 1 minute

    claim_window_hours: int = 48
    # The winner is reminded this many hours before the claim deadline.
    claim_reminder_hours: int = Field(default=12, ge=1)
    # Weekly notice delivery rate (Telegram allows ~30 messages/s per bot).
    notice_per_second: float = Field(default=20.0, gt=0, le=25)
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

    @model_validator(mode="after")
    def _reminder_inside_window(self) -> "Settings":
        if self.claim_reminder_hours >= self.claim_window_hours:
            raise ValueError("CLAIM_REMINDER_HOURS must be less than CLAIM_WINDOW_HOURS")
        return self

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"
