from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ARB_",
        extra="ignore",
    )

    mode: Mode = Mode.PAPER

    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polygon_rpc: str = "https://polygon-rpc.com"

    private_key: SecretStr | None = None
    funder_address: str | None = None
    signature_type: int = 0
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    api_passphrase: SecretStr | None = None

    min_net_edge_bps: int = 50
    max_basket_usd: Decimal = Field(default=Decimal("50"))
    max_open_baskets: int = 3
    max_open_baskets_per_event: int = 1
    daily_loss_stop_usd: Decimal = Field(default=Decimal("100"))
    kill_switch_file: Path = Path("./KILL")
    resolution_skip_hours: int = 24

    paper_latency_ms: int = 250

    db_path: Path = Path("./arbitrage.db")
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    log_level: str = "INFO"

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE

    def require_live_credentials(self) -> None:
        if not self.is_live:
            return
        missing = [
            name
            for name, val in [
                ("ARB_PRIVATE_KEY", self.private_key),
                ("ARB_FUNDER_ADDRESS", self.funder_address),
            ]
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"Live mode requires: {', '.join(missing)}. Set them in .env or run paper mode."
            )


settings = Settings()
