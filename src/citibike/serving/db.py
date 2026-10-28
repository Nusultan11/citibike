from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseUnavailable(Exception):
    """Safe public failure; never expose connection details/passwords."""


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )

    host: str = Field(default="127.0.0.1", validation_alias="PGHOST")
    port: int = Field(default=5432, validation_alias="PGPORT", ge=1, le=65535)
    database: str = Field(default="bikeflow", validation_alias="PGDATABASE")
    user: str = Field(default="bikeflow", validation_alias="PGUSER")
    password: SecretStr = Field(default=SecretStr(""), validation_alias="PGPASSWORD")


def connect(
    settings: DatabaseSettings | None = None,
    *,
    readonly: bool = False,
) -> psycopg.Connection[dict[str, Any]]:
    settings = settings or DatabaseSettings()
    if not settings.password.get_secret_value():
        raise DatabaseUnavailable("Serving database unavailable")
    try:
        return psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options=(
                "-c statement_timeout=5000"
                if readonly
                else "-c statement_timeout=120000"
            )
            + (" -c default_transaction_read_only=on" if readonly else ""),
        )
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None
