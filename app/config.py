from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def csv(name, default):
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_publishable_key: str = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    cors_origins: tuple[str, ...] = csv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://127.0.0.1:5173,http://127.0.0.1:5174,http://127.0.0.1:5175",
    )
    allowed_hosts: tuple[str, ...] = csv(
        "ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
    )
    environment: str = os.getenv("APP_ENV", "development")
    redis_url: str = os.getenv("REDIS_URL", "")
    rate_limit_secret: str = os.getenv("RATE_LIMIT_SECRET", "")
    pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    ai_daily_limit: int = int(os.getenv("AI_DAILY_LIMIT", "100"))
    ai_concurrency: int = int(os.getenv("AI_CONCURRENCY", "2"))
    public_signup: bool = os.getenv("PUBLIC_SIGNUP_ENABLED", "true").lower() == "true"


settings = Settings()
