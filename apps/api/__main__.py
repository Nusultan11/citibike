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
        access_log=False,
        proxy_headers=False,
        limit_concurrency=128,
        timeout_keep_alive=5,
        h11_max_incomplete_event_size=16384,
    )


if __name__ == "__main__":
    main()
