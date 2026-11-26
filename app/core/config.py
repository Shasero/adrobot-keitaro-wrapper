from pydantic import Field, PositiveInt, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "AdRobot"
    database_url: str = Field(default="sqlite:///./adrobot.db", repr=False)
    keitaro_base_url: str = Field(
        default="https://example.invalid/admin_api/v1", repr=False
    )
    keitaro_api_key: SecretStr | None = Field(default=None, repr=False)
    keitaro_domain_id: PositiveInt | None = None
    keitaro_group_id: PositiveInt | None = None
    keitaro_traffic_source_id: PositiveInt | None = None
    campaign_name_prefix: str = "test_shapovalov_"
    keitaro_timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)
    offer_cache_ttl_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)

    @field_validator(
        "keitaro_api_key",
        "keitaro_domain_id",
        "keitaro_group_id",
        "keitaro_traffic_source_id",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if info.field_name.endswith("_id") and value is not None:
            if type(value) is not int and not (isinstance(value, str) and value.isascii() and value.isdecimal()):
                raise ValueError("Expected a positive integer ID")
        return value
