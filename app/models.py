from datetime import datetime, date, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, field_validator


class Value(BaseModel):
    value: str | None = Field(default=None, max_length=20000)

    @field_validator("value")
    @classmethod
    def no_newline(cls, v):
        if v and any(ord(c) < 32 or ord(c) > 255 for c in v):
            raise ValueError("Некоректні символи заголовка")
        return v


class Cookie(BaseModel):
    name: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_!#$%&'*+.^`|~-]+$")
    value: str = Field(max_length=20000, pattern=r"^[\x21-\x3A\x3C-\x7E]*$")
    domain: str = Field(default=".facebook.com", max_length=256)
    path: str = Field(default="/", max_length=1024)
    secure: bool = True
    httpOnly: bool = False
    session: bool = False
    hostOnly: bool = False
    expirationDate: float | None = None
    sameSite: str | None = Field(default=None, max_length=40)

    @field_validator("domain")
    @classmethod
    def facebook_only(cls, v):
        d = v.lstrip(".").lower()
        if d != "facebook.com" and not d.endswith(".facebook.com"):
            raise ValueError("Дозволено лише Facebook cookies")
        return v


class Cookies(BaseModel):
    items: list[Cookie] = Field(max_length=300)


class Ingest(BaseModel):
    schemaVersion: Literal[2, 3]
    installationId: UUID
    sentAt: datetime
    extensionVersion: str = Field(default="", max_length=64)
    reason: str = Field(default="", max_length=64)
    enabled: bool = True
    cookies: Cookies
    token: Value = Field(default_factory=Value)
    userAgent: Value = Field(default_factory=Value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, v):
        if v.tzinfo is None or v > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError("sentAt потребує часового поясу та коректного часу")
        return v.astimezone(timezone.utc)


class Settings(BaseModel):
    interval_hours: Literal[1, 4, 8, 12, 24] = 1
    facebook_enabled: bool = True
    keitaro_enabled: bool = True
    commission_percent: str = "10"
    earliest_date: date = date(2026, 9, 1)
    max_age_months: int = Field(default=2, ge=1, le=2)
    lookback_days: int = Field(default=5, ge=1, le=30)

    @field_validator("commission_percent")
    @classmethod
    def commission(cls, v):
        try:
            n = Decimal(v)
            if not n.is_finite() or not 0 <= n <= 1000 or n.as_tuple().exponent < -4:
                raise ValueError("Комісія: від 0 до 1000, до 4 знаків після крапки")
        except InvalidOperation:
            raise ValueError("Некоректна комісія")
        return str(n)


class ProxyInput(BaseModel):
    name: str = Field(default="Proxy", min_length=1, max_length=120)
    protocol: Literal["http", "https", "socks5h"] = "http"
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.\-]+$")
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=1024)
    refresh_url: str = Field(default="", max_length=2048)

    @field_validator("refresh_url")
    @classmethod
    def refresh_https(cls, v):
        from urllib.parse import urlsplit
        u = urlsplit(v)
        if v and (u.scheme != "https" or not u.hostname or u.username or u.fragment):
            raise ValueError("Refresh URL має бути HTTPS без логіна")
        return v
