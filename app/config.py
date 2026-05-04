from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="FastAPI Base", alias="APP_NAME")
    env: str = Field(default="development", alias="ENV")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="admin", alias="ADMIN_PASSWORD")
    admin_session_secret: str = Field(
        default="replace-this-secret", alias="ADMIN_SESSION_SECRET"
    )
    admin_session_ttl_seconds: int = Field(
        default=28800, alias="ADMIN_SESSION_TTL_SECONDS"
    )
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    mongo_uri: str | None = Field(default=None, alias="MONGO_URI")
    sql_url: str | None = Field(default=None, alias="SQL_URL")
    mongo_db_name: str = Field(default="fastapi_base", alias="MONGO_DB_NAME")
    mongo_submissions_collection: str = Field(
        default="submissions", alias="MONGO_SUBMISSIONS_COLLECTION"
    )
    mongo_visitors_collection: str = Field(
        default="visitors", alias="MONGO_VISITORS_COLLECTION"
    )
    mongo_settings_collection: str = Field(
        default="settings", alias="MONGO_SETTINGS_COLLECTION"
    )
    online_users_key: str = Field(
        default="fastapi-base:online-users", alias="ONLINE_USERS_KEY"
    )
    online_user_ttl_seconds: int = Field(
        default=10, alias="ONLINE_USER_TTL_SECONDS"
    )
    online_heartbeat_interval_seconds: int = Field(
        default=3, alias="ONLINE_HEARTBEAT_INTERVAL_SECONDS"
    )
    online_presence_broadcast_interval_seconds: float = Field(
        default=1.0, alias="ONLINE_PRESENCE_BROADCAST_INTERVAL_SECONDS"
    )
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"], alias="ALLOWED_HOSTS"
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CORS_ORIGINS"
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def parse_comma_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = [item.strip() for item in value.split(",") if item.strip()]
            return cleaned
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("redis_url", "mongo_uri", "sql_url", mode="before")
    @classmethod
    def parse_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        cleaned = str(value).strip()
        return cleaned or None


settings = Settings()
