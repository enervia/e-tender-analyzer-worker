from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    worker_api_key: str = ""
    max_scan_pages: int = 100
    default_start_id: int = 346000
    default_end_id: int = 360321
    request_delay_seconds: float = 1.2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
