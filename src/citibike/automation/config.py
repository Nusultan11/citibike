"""Bounded local worker configuration. Credentials remain in environment only."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from citibike.storage import read_json


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Path("data")
    inventory: Path | None = None
    model_bundle: Path | None = None
    fallback_bundle: Path | None = None
    gbfs_path: Path | None = None
    forecast_path: Path | None = None
    stations: list[str] | None = None
    risk_config: Path | None = None
    actuals_dataset: Path | None = None
    # A stable decision origin is written once per UTC date before prediction.
    # An explicit origin is allowed only for isolated, labelled controlled tests.
    controlled_origin: str | None = None
    mode: str = Field(
        default="real_official", pattern="^(real_official|controlled_fixture)$"
    )
    max_changed_months: int = Field(default=2, ge=1, le=24)
    max_download_bytes: int = Field(default=1_000_000_000, ge=1, le=25_000_000_000)
    timeout_seconds: float = Field(default=300, ge=0.1, le=7200)
    max_rss_mb: int = Field(default=2048, ge=64, le=8192)
    min_free_mb: int = Field(default=512, ge=0)

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "automation"


def load(path: Path) -> Config:
    return Config.model_validate(read_json(path))
