import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_defaults():
    settings = Settings()

    assert settings.app_name == "AdRobot"
    assert settings.database_url == "sqlite:///./adrobot.db"
    assert settings.keitaro_base_url == "https://example.invalid/admin_api/v1"
    assert settings.keitaro_api_key is None
    assert settings.keitaro_domain_id is None
    assert settings.keitaro_group_id is None
    assert settings.keitaro_traffic_source_id is None
    assert settings.campaign_name_prefix == "test_shapovalov_"
    assert settings.keitaro_timeout_seconds == 15
    assert settings.offer_cache_ttl_seconds == 300


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "APP_NAME=Тестовое приложение\n"
        "KEITARO_DOMAIN_ID=101\n"
        "KEITARO_GROUP_ID=\n"
        "KEITARO_TRAFFIC_SOURCE_ID=103\n",
        encoding="utf-8",
    )
    assert Settings().app_name == "Тестовое приложение"

    monkeypatch.setenv("APP_NAME", "AdRobot Test")
    monkeypatch.setenv("KEITARO_DOMAIN_ID", "")
    monkeypatch.setenv("KEITARO_TRAFFIC_SOURCE_ID", "104")
    monkeypatch.setenv("KEITARO_API_KEY", "")
    monkeypatch.setenv("OFFER_CACHE_TTL_SECONDS", "60")

    settings = Settings()
    assert settings.app_name == "AdRobot Test"
    assert settings.keitaro_domain_id is None
    assert settings.keitaro_group_id is None
    assert settings.keitaro_traffic_source_id == 104
    assert settings.keitaro_api_key is None
    assert settings.offer_cache_ttl_seconds == 60


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEITARO_TIMEOUT_SECONDS", "0"),
        ("KEITARO_TIMEOUT_SECONDS", "inf"),
        ("OFFER_CACHE_TTL_SECONDS", "0"),
        ("OFFER_CACHE_TTL_SECONDS", "inf"),
        ("KEITARO_DOMAIN_ID", "-1"),
        ("KEITARO_GROUP_ID", "0"),
        ("KEITARO_TRAFFIC_SOURCE_ID", "invalid"),
    ],
)
def test_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings()


def test_key_is_hidden(monkeypatch):
    marker = "synthetic-test-marker"
    monkeypatch.setenv("KEITARO_API_KEY", marker)
    settings = Settings()

    assert isinstance(settings.keitaro_api_key, SecretStr)
    assert marker not in repr(settings)
    monkeypatch.setenv("KEITARO_TIMEOUT_SECONDS", marker)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert marker not in str(error.value)
