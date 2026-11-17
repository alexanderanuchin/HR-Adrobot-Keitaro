from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://adrobot:local@localhost:5432/adrobot"
    internal_api_key: str = ""
    keitaro_base_url: str = "https://tlgk.host"
    keitaro_api_key: str = ""
    keitaro_http_timeout: float = 15.0
    log_level: str = "INFO"

@lru_cache
def get_settings() -> Settings:
    return Settings()
