"""Run with python -m apps.api from the repository root."""

import uvicorn

from apps.api.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "apps.api.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
