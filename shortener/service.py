"""URL-shortening business logic."""

from datetime import datetime, timezone
import secrets
import sqlite3
from urllib.parse import urlsplit

from .database import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ValidationError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


class ShortenerService:
    def __init__(self, database: Database, base_url: str, max_url_length: int = 2048):
        self.database = database
        self.base_url = base_url.rstrip("/")
        self.max_url_length = max_url_length

    def create_link(self, url: str, custom_code: str | None = None, expires_at: str | None = None) -> dict:
        url = self.validate_url(url)
        if expires_at is not None:
            if not isinstance(expires_at, str):
                raise ValidationError("expires_at must be an ISO-8601 timestamp")
            try:
                expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError("expires_at must be an ISO-8601 timestamp") from exc
            if expiry.tzinfo is None:
                raise ValidationError("expires_at must include a timezone")
            if expiry <= datetime.now(timezone.utc):
                raise ValidationError("expires_at must be in the future")
            expires_at = expiry.astimezone(timezone.utc).isoformat(timespec="seconds")
        if custom_code is not None and not isinstance(custom_code, str):
            raise ValidationError("code must be a string")
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        code = custom_code or "".join(secrets.choice(alphabet) for _ in range(8))
        if not code.isascii() or not code.isalnum() or not 4 <= len(code) <= 12:
            raise ValidationError("code must be 4-12 ASCII letters or digits")
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO links(code,url,created_at,expires_at) VALUES(?,?,?,?)",
                    (code, url, utc_now(), expires_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("code is already in use") from exc
        return {"code": code, "url": url, "short_url": f"{self.base_url}/{code}", "expires_at": expires_at}

    def validate_url(self, url: str) -> str:
        if not isinstance(url, str) or not url or len(url) > self.max_url_length:
            raise ValidationError(f"url must be a non-empty string of at most {self.max_url_length} characters")
        if any(ord(char) < 32 for char in url):
            raise ValidationError("url must not contain control characters")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValidationError("url must be an absolute http or https URL")
        if any(char.isspace() for char in parsed.hostname):
            raise ValidationError("url hostname must not contain whitespace")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValidationError("url contains an invalid port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValidationError("url port must be between 1 and 65535")
        if parsed.username or parsed.password:
            raise ValidationError("URLs containing credentials are not accepted")
        return url

    def resolve(self, code: str, referrer: str | None = None, user_agent: str | None = None) -> str:
        with self.database.connect() as connection:
            row = connection.execute("SELECT url,expires_at FROM links WHERE code=?", (code,)).fetchone()
            if row is None:
                raise NotFoundError("short link not found")
            if row["expires_at"]:
                expiry = datetime.fromisoformat(row["expires_at"])
                if expiry <= datetime.now(timezone.utc):
                    raise NotFoundError("short link has expired")
            connection.execute("UPDATE links SET click_count=click_count+1 WHERE code=?", (code,))
            connection.execute(
                "INSERT INTO clicks(code,clicked_at,referrer,user_agent) VALUES(?,?,?,?)",
                (code, utc_now(), _bounded(referrer, 2048), _bounded(user_agent, 512)),
            )
            return row["url"]

    def analytics(self, code: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT code,url,created_at,expires_at,click_count FROM links WHERE code=?", (code,)
            ).fetchone()
            if row is None:
                raise NotFoundError("short link not found")
            daily = connection.execute(
                "SELECT substr(clicked_at,1,10) AS day, count(*) AS clicks FROM clicks "
                "WHERE code=? GROUP BY day ORDER BY day DESC LIMIT 30", (code,)
            ).fetchall()
            return {**dict(row), "daily_clicks": [dict(item) for item in daily]}


def _bounded(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None
