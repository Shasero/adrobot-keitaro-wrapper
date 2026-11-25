import os
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import URL

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings_keys = {field.upper() for field in Settings.model_fields}
    for name in tuple(os.environ):
        if name.upper() in settings_keys:
            monkeypatch.delenv(name)

    def reject_network(*args, **kwargs):
        pytest.fail("Tests must not make external HTTP requests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)


@pytest.fixture
def database_url(tmp_path):
    return str(URL.create("sqlite", database=str(tmp_path / "adrobot.db")))


@pytest.fixture
def alembic_config(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture
def client(database_url):
    from app.main import create_app

    application = create_app(Settings(database_url=database_url))
    with TestClient(application) as test_client:
        yield test_client
