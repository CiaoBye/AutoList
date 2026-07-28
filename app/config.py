from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any


REDACTED_SECRET = str()


@dataclass
class Settings:
    data_dir: str = os.getenv("DATA_DIR", "/data")
    mp_base_url: str = os.getenv("MP_BASE_URL", "").rstrip("/")
    mp_api_key: str = os.getenv("MP_API_KEY", "")
    mp_timeout_seconds: float = float(os.getenv("MP_TIMEOUT_SECONDS", "30"))
    emby_base_url: str = os.getenv("EMBY_BASE_URL", "").rstrip("/")
    emby_api_key: str = os.getenv("EMBY_API_KEY", "")
    tmdb_api_key: str = os.getenv("TMDB_API_KEY", "")
    tmdb_language: str = os.getenv("TMDB_LANGUAGE", "zh-CN")
    mdblist_api_key: str = os.getenv("MDBLIST_API_KEY", "")
    cookiecloud_key: str = os.getenv("COOKIECLOUD_KEY", "")
    cookiecloud_password: str = os.getenv("COOKIECLOUD_PASSWORD", "")
    cookiecloud_forward_moviepilot: bool = os.getenv("COOKIECLOUD_FORWARD_MOVIEPILOT", "true").lower() == "true"
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
        for key in ("tmdb_proxy_enabled", "pt_proxy_enabled", "cookiecloud_forward_moviepilot", "dashboard_random_posters"):
            if key in values and values[key] is not None:
                setattr(self, key, bool(values[key]))
        if values.get("mp_timeout_seconds") is not None:
            self.mp_timeout_seconds = float(values["mp_timeout_seconds"])

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
            "cookiecloud_forward_moviepilot": self.cookiecloud_forward_moviepilot,
            "cookiecloud_endpoint": "/cookiecloud",
            "outbound_proxy_configured": bool(self.outbound_proxy_url),
            "outbound_proxy_url": self.outbound_proxy_url,
            "tmdb_proxy_enabled": self.tmdb_proxy_enabled,
            "pt_proxy_enabled": self.pt_proxy_enabled,
            "ai_base_url": self.ai_base_url,
            "ai_api_key": "",
            "ai_api_key_configured": bool(self.ai_api_key),
            "ai_model": self.ai_model,
            "tr_base_url": self.tr_base_url,
            "tr_username": self.tr_username,
            "tr_password": REDACTED_SECRET,
            "tr_password_configured": bool(self.tr_password),
            "dashboard_random_posters": self.dashboard_random_posters,
        }


settings = Settings()


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


def save_runtime_settings(values: dict[str, Any]) -> Settings:
    settings.apply(values)
    path = runtime_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in asdict(settings).items() if key != "data_dir"}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    return settings
