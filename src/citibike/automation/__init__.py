"""F7 orchestration of existing batch components; never imported by the API."""

import os

# Local, ignored metadata by default. No telemetry/account is needed for F7.
os.environ.setdefault("PREFECT_HOME", "data/automation/prefect-home")
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "false")
os.environ.setdefault("DO_NOT_TRACK", "true")
os.environ.setdefault("MLFLOW_ENABLE_ASYNC_LOGGING", "false")
os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")

VERSION = "f7.0.0"
