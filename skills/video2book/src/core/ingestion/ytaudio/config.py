"""运行配置：Cookie / 代理 / PO Token / 限速 / 过滤规则等。

优先级：命令行参数 > 环境变量 > 配置文件(config.json) > 内置默认值。
配置文件默认读取 ``yt/config.json``（可用 ``--config`` 指定）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 工作区根与产物根绑定
try:
    from src.core import paths as _paths
    PKG_ROOT = _paths.home_root()
    DEFAULT_OUTPUT_DIR = _paths.products_root()
except Exception:
    PKG_ROOT = Path(__file__).resolve().parents[4]
    DEFAULT_OUTPUT_DIR = PKG_ROOT / "output"



@dataclass
class Config:
    """全部可调参数。"""

    # --- 身份与反爬 ---
    cookiefile: str = ""            # Netscape 格式 cookies.txt（相对 yt/ 目录或绝对路径）
    cookies_from_browser: str = ""  # 如 chrome / edge / firefox，直接从浏览器读取登录态
    proxy: str = ""
    po_token: str = ""              # 形如 "web+TOKEN"，多个用英文逗号分隔
    visitor_data: str = ""          # 浏览器 visitorData
    player_client: str = "web_safari,tv,web_embedded"  # Innertube 客户端链
    user_agent: str = DEFAULT_UA

    # --- 输出 ---
    output_dir: str = ""
    audio_format: str = "m4a"       # m4a / mp3 / opus / flac / wav
    audio_quality: str = "192"      # mp3/ogg 用码率；m4a 等也接受
    keep_video: bool = False        # 提取音频后是否保留原始音频容器之外的文件
    flat: bool = False              # True=全部平铺，不按播放列表分目录

    # --- 抓取范围与过滤 ---
    limit: int = 0                  # 只取最新的 N 个视频（0=全部）
    max_videos: int = 0             # 频道列表最多解析多少条（0=不限）
    max_playlists: int = 0          # 最多扫描多少个播放列表（0=不限）
    min_duration: int = 0           # 短于该秒数的视频丢弃（0=不启用）
    exclude_shorts: bool = True     # 剔除 Shorts 短视频
    exclude_live: bool = True       # 剔除直播/回放
    enrich_playlists: bool = False  # 是否把「仅在播放列表里」的视频也纳入

    # --- 节流与重试（反爬） ---
    sleep_requests: float = 1.0     # 提取阶段每次请求之间的间隔（秒）
    sleep_interval: float = 2.0     # 下载之间的最小间隔（秒）
    max_sleep_interval: float = 6.0 # 下载之间的最大间隔（秒）
    retries: int = 10
    fragment_retries: int = 10
    extractor_retries: int = 5
    concurrent_fragments: int = 4

    # --- 运行时 ---
    verbose: bool = False
    config_path: str = field(default="", repr=False)

    @property
    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir).expanduser().resolve() if self.output_dir \
            else DEFAULT_OUTPUT_DIR

    def resolved_cookiefile(self) -> Optional[Path]:
        if not self.cookiefile:
            return None
        path = Path(self.cookiefile).expanduser()
        if not path.is_absolute():
            path = PKG_ROOT / path
        return path if path.exists() else None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


_INT_FIELDS = {
    "limit", "max_videos", "max_playlists", "min_duration",
    "retries", "fragment_retries", "extractor_retries", "concurrent_fragments",
}
_FLOAT_FIELDS = {"sleep_requests", "sleep_interval", "max_sleep_interval"}
_BOOL_FIELDS = {
    "keep_video", "flat", "exclude_shorts", "exclude_live",
    "enrich_playlists", "verbose",
}


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _BOOL_FIELDS:
        return _as_bool(value)
    if name in _INT_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if name in _FLOAT_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return str(value)


def load_config(config_path: Optional[str] = None, **overrides: Any) -> Config:
    """加载配置。"""
    cfg = Config()

    path = Path(config_path) if config_path else (PKG_ROOT / "config.json")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"配置文件解析失败：{path}（{exc}）") from exc
        for key, value in data.items():
            if hasattr(cfg, key) and key != "config_path":
                coerced = _coerce(key, value)
                if coerced is not None:
                    setattr(cfg, key, coerced)
        cfg.config_path = str(path)

    env_map = {
        "YTAUDIO_COOKIEFILE": "cookiefile",
        "YTAUDIO_COOKIES_FROM_BROWSER": "cookies_from_browser",
        "YTAUDIO_PROXY": "proxy",
        "YTAUDIO_PO_TOKEN": "po_token",
        "YTAUDIO_VISITOR_DATA": "visitor_data",
        "YTAUDIO_PLAYER_CLIENT": "player_client",
        "YTAUDIO_OUTPUT_DIR": "output_dir",
        "YTAUDIO_AUDIO_FORMAT": "audio_format",
        "YTAUDIO_LIMIT": "limit",
    }
    for env_key, name in env_map.items():
        if env_key in os.environ:
            coerced = _coerce(name, os.environ[env_key])
            if coerced is not None:
                setattr(cfg, name, coerced)

    for key, value in overrides.items():
        if value is None:
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    if cfg.audio_format not in ("m4a", "mp3", "opus", "flac", "wav", "ogg"):
        cfg.audio_format = "m4a"
    if cfg.limit < 0:
        cfg.limit = 0
    if cfg.concurrent_fragments < 1:
        cfg.concurrent_fragments = 1
    if cfg.max_sleep_interval < cfg.sleep_interval:
        cfg.max_sleep_interval = cfg.sleep_interval

    return cfg


def save_config_value(key: str, value: Any, config_path: Optional[str] = None) -> Path:
    """把单个字段写入配置文件（保留其它字段）。"""
    path = Path(config_path) if config_path else (PKG_ROOT / "config.json")
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
