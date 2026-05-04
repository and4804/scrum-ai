from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    wa_trigger_prefix: str = Field(default="/pm", alias="WA_TRIGGER_PREFIX")
    wa_sidecar_url: str = Field(default="http://localhost:3000", alias="WA_SIDECAR_URL")
    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    notion_api_key: str = Field(alias="NOTION_API_KEY")
    notion_api_version: str = Field(default="2022-06-28", alias="NOTION_API_VERSION")
    app_timezone: str = Field(default="Asia/Kolkata", alias="APP_TIMEZONE")
    app_location: str = Field(default="Pune, India", alias="APP_LOCATION")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
