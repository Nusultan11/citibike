from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.config import Settings
from apps.api.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/health" in response.json()["paths"]
    assert client.get("/docs").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/missing", 404, "not_found"),
        ("POST", "/health", 405, "method_not_allowed"),
    ],
)
def test_http_error(
    client: TestClient, method: str, path: str, status: int, code: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if status == 405:
        assert response.headers["allow"] == "GET"


def test_validation_error() -> None:
    application = create_app()

    @application.get("/validation-test")
    def validation_test(value: int) -> int:
        return value

    with TestClient(application) as client:
        response = client.get("/validation-test", params={"value": "private-input"})
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Invalid request"}
    }
    assert "private-input" not in response.text


def test_unexpected_error_is_sanitized() -> None:
    application = create_app()

    @application.get("/failure-test")
    def failure_test() -> None:
        raise RuntimeError("private-internal-detail")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/failure-test")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error"}
    }
    assert "private-internal-detail" not in response.text


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIKEFLOW_API_PORT", "8123")
    assert Settings().port == 8123


@pytest.mark.parametrize("port", ["0", "65536", "not-a-number"])
def test_invalid_settings(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("BIKEFLOW_API_PORT", port)
    with pytest.raises(ValidationError):
        Settings()
