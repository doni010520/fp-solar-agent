from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    timezone: str = "America/Bahia"
    buffer_debounce_seconds: int = 8

    # Allowlist de telefones (separados por vírgula). Vazio = responde todo mundo.
    # Use só dígitos, com DDI. Ex: ALLOWED_PHONES=5573999000111,5511988887777
    allowed_phones: str = ""

    @property
    def allowed_phones_set(self) -> set[str]:
        if not self.allowed_phones.strip():
            return set()
        return {p.strip() for p in self.allowed_phones.split(",") if p.strip()}

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4.1-mini"
    openai_vision_model: str = "gpt-4o"
    openai_transcribe_model: str = "whisper-1"

    # Uazapi
    uazapi_base_url: str
    uazapi_token: str
    uazapi_webhook_secret: str = ""
    internal_group_id: str

    # Supabase
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str = ""
    database_url: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
