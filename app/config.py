from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import fcntl
except ImportError:  # pragma: no cover - production image is Unix-based
    fcntl = None  # type: ignore[assignment]


REDACTED_SECRET = str()
APP_VERSION = "1.20"
ACCESS_TOKEN_MIN_LENGTH = 32
ACCESS_TOKEN_MAX_LENGTH = 256
PUBLIC_URL_SENSITIVE_QUERY_KEYS = {
    "api-key", "api_key", "apikey", "access_token", "authorization", "cookie", "password", "passwd",
    "passkey", "refresh_token", "secret", "token", "key",
}


def public_endpoint_url(value: str) -> str:
    """Expose a configured endpoint while never returning URL credentials."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
            return ""
        port = parsed.port
    except ValueError:
        return ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = f"{host}:{port}" if port else host
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.strip().lower() not in PUBLIC_URL_SENSITIVE_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(netloc=netloc, query=urlencode(query, doseq=True), fragment=""))


def _truthy_environment(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}

# 仅从环境变量读取，不写入 runtime-settings.json，避免被设置页覆盖。
def access_token() -> str:
    return os.getenv("AUTOLIST_ACCESS_TOKEN", "").strip()


def access_token_required() -> bool:
    return bool(access_token())


def access_token_validation_error(value: str | None = None) -> str | None:
    """Validate the configured token without ever returning the token itself.

    Token strength is enforced by default whenever authentication is enabled.
    A trusted-LAN deployment that still needs a legacy token can explicitly set
    ``AUTOLIST_REQUIRE_STRONG_TOKEN=false`` during migration. Keeping
    validation separate from ``access_token_required`` means the health
    endpoint can report a safe diagnostic without exposing the token itself.
    """
    token = access_token() if value is None else str(value).strip()
    if not token:
        return "未配置访问令牌"
    if len(token) < ACCESS_TOKEN_MIN_LENGTH:
        return f"访问令牌至少需要 {ACCESS_TOKEN_MIN_LENGTH} 个字符"
    if len(token) > ACCESS_TOKEN_MAX_LENGTH:
        return f"访问令牌不能超过 {ACCESS_TOKEN_MAX_LENGTH} 个字符"
    if len(set(token)) < 8:
        return "访问令牌字符多样性不足"
    return None


def access_token_is_strong(value: str | None = None) -> bool:
    return access_token_validation_error(value) is None


def access_token_strength_enforced() -> bool:
    """Return whether weak configured tokens should block protected requests.

    Authentication is fail-closed by default. The explicit ``false`` value is
    retained only as a deliberate compatibility escape hatch for trusted LAN
    migrations; it should not be used for an internet-facing deployment.
    """
    configured = os.getenv("AUTOLIST_REQUIRE_STRONG_TOKEN")
    if configured is not None and configured.strip():
        return _truthy_environment("AUTOLIST_REQUIRE_STRONG_TOKEN")
    return access_token_required()


def access_token_strength() -> str:
    if not access_token_required():
        return "missing"
    return "strong" if access_token_is_strong() else "weak"


def _env_float(name: str, default: str) -> float:
    """Read a float env var with a safe fallback for invalid input."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


@dataclass
class Settings:
    data_dir: str = os.getenv("DATA_DIR", "/data")
    mp_base_url: str = os.getenv("MP_BASE_URL", "").rstrip("/")
    mp_api_key: str = os.getenv("MP_API_KEY", "")
    mp_timeout_seconds: float = _env_float("MP_TIMEOUT_SECONDS", "30")
    emby_base_url: str = os.getenv("EMBY_BASE_URL", "").rstrip("/")
    emby_api_key: str = os.getenv("EMBY_API_KEY", "")
    tmdb_api_key: str = os.getenv("TMDB_API_KEY", "")
    tmdb_language: str = os.getenv("TMDB_LANGUAGE", "zh-CN")
    mdblist_api_key: str = os.getenv("MDBLIST_API_KEY", "")
    cookiecloud_key: str = os.getenv("COOKIECLOUD_KEY", "")
    cookiecloud_password: str = os.getenv("COOKIECLOUD_PASSWORD", "")
    outbound_proxy_url: str = os.getenv("OUTBOUND_PROXY_URL", "")
    tmdb_proxy_enabled: bool = os.getenv("TMDB_PROXY_ENABLED", "false").lower() == "true"
    pt_proxy_enabled: bool = os.getenv("PT_PROXY_ENABLED", "false").lower() == "true"
    ai_base_url: str = os.getenv("AI_BASE_URL", "").rstrip("/")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "")
    tr_base_url: str = os.getenv("TR_BASE_URL", "").rstrip("/")
    tr_username: str = os.getenv("TR_USERNAME", "")
    tr_password: str = os.getenv("TR_PASSWORD", "")
    dashboard_random_posters: bool = os.getenv("DASHBOARD_RANDOM_POSTERS", "false").lower() == "true"
    # 下载目录与分类不在 AutoList 保存：统一交由 MoviePilot 的媒体分类规则处理。

    def apply(self, values: dict[str, Any]) -> None:
        for key in (
            "mp_base_url", "mp_api_key", "emby_base_url", "emby_api_key", "tmdb_api_key",
            "tmdb_language", "mdblist_api_key", "cookiecloud_key", "cookiecloud_password", "outbound_proxy_url", "ai_base_url", "ai_api_key", "ai_model", "tr_base_url",
            "tr_username", "tr_password",
        ):
            if key in values and values[key] is not None:
                value = str(values[key]).strip()
                setattr(self, key, value.rstrip("/") if key.endswith("base_url") else value)
        for key in ("tmdb_proxy_enabled", "pt_proxy_enabled", "dashboard_random_posters"):
            if key in values and values[key] is not None:
                setattr(self, key, bool(values[key]))
        if values.get("mp_timeout_seconds") is not None:
            try:
                parsed_timeout = float(values["mp_timeout_seconds"])
            except (TypeError, ValueError):
                parsed_timeout = self.mp_timeout_seconds  # 非法值保留现值
            self.mp_timeout_seconds = parsed_timeout

    def public_values(self) -> dict[str, Any]:
        return {
            "mp_base_url": self.mp_base_url,
            "mp_api_key": "",
            "mp_api_key_configured": bool(self.mp_api_key),
            "mp_timeout_seconds": self.mp_timeout_seconds,
            "emby_base_url": self.emby_base_url,
            "emby_api_key": "",
            "emby_api_key_configured": bool(self.emby_api_key),
            "tmdb_api_key": "",
            "tmdb_api_key_configured": bool(self.tmdb_api_key),
            "tmdb_language": self.tmdb_language,
            "mdblist_api_key": "",
            "mdblist_api_key_configured": bool(self.mdblist_api_key),
            "cookiecloud_key": "",
            "cookiecloud_key_configured": bool(self.cookiecloud_key),
            "cookiecloud_password": REDACTED_SECRET,
            "cookiecloud_password_configured": bool(self.cookiecloud_password),
            "cookiecloud_endpoint": "/cookiecloud",
            "outbound_proxy_configured": bool(self.outbound_proxy_url),
            "outbound_proxy_url_configured": bool(self.outbound_proxy_url),
            "outbound_proxy_url": public_endpoint_url(self.outbound_proxy_url),
            "pt_proxy_enabled": self.pt_proxy_enabled,
            "ai_base_url": self.ai_base_url,
            "ai_api_key": "",
            "ai_api_key_configured": bool(self.ai_api_key),
            "ai_model": self.ai_model,
            "tr_base_url": self.tr_base_url,
            "tr_username": "",
            "tr_username_configured": bool(self.tr_username),
            "tr_password": REDACTED_SECRET,
            "tr_password_configured": bool(self.tr_password),
            "dashboard_random_posters": self.dashboard_random_posters,
            "access_token_required": access_token_required(),
            "access_token_strength": access_token_strength(),
            "access_token_strength_enforced": access_token_strength_enforced(),
        }


settings = Settings()

_runtime_settings_thread_lock = threading.RLock()


def runtime_settings_path() -> Path:
    return Path(settings.data_dir) / "runtime-settings.json"


def load_runtime_settings() -> Settings:
    path = runtime_settings_path()
    if not path.exists():
        return settings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(data, dict):
        settings.apply(data)
    return settings


@contextmanager
def _runtime_settings_lock(path: Path):
    """Serialize runtime-settings updates within and across worker processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with _runtime_settings_thread_lock:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            lock_path.chmod(0o600)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def save_runtime_settings(values: dict[str, Any]) -> Settings:
    path = runtime_settings_path()
    with _runtime_settings_lock(path):
        # Reload the latest file while holding the lock so separate Uvicorn
        # workers do not overwrite one another's partial updates with stale
        # in-memory Settings values.
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = None
            if isinstance(current, dict):
                settings.apply(current)
        settings.apply(values)
        payload = {key: value for key, value in asdict(settings).items() if key != "data_dir"}
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                file_descriptor = -1
                json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        path.chmod(0o600)
    return settings
