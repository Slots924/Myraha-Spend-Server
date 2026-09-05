import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    database: str
    password: str
    client_key: str
    encryption_key: str
    keitaro_url: str = ""
    keitaro_key: str = ""
    meta_version: str = "v23.0"
    secure_cookie: bool = True
    seed_proxy: str = ""
    scheduler: bool = True

    @classmethod
    def env(cls):
        load_dotenv(ROOT / ".env")
        cfg = cls(
            database=os.getenv("DATABASE_PATH", str(ROOT / "data/spend/spend.sqlite")),
            password=os.getenv("APP_PASSWORD", ""),
            client_key=os.getenv("CLIENT_KEY", ""),
            encryption_key=os.getenv("ENCRYPTION_KEY", ""),
            keitaro_url=os.getenv("KEITARO_URL", "").rstrip("/"),
            keitaro_key=os.getenv("KEITARO_API_KEY", ""),
            meta_version=os.getenv("META_API_VERSION", "v23.0"),
            secure_cookie=os.getenv("COOKIE_SECURE", "true").lower() == "true",
            seed_proxy=os.getenv("INITIAL_PROXY", ""),
            scheduler=os.getenv("SCHEDULER_ENABLED", "true").lower() == "true",
        )
        if len(cfg.password) < 24 or not cfg.encryption_key:
            raise RuntimeError("Заповніть APP_PASSWORD (мінімум 24 символи) та ENCRYPTION_KEY у .env")
        return cfg
