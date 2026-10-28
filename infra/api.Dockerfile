FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BIKEFLOW_API_HOST=0.0.0.0
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 apps/__init__.py ./apps/__init__.py
COPY --chown=10001:10001 apps/api/ ./apps/api/
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "apps.api"]
