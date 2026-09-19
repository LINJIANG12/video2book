"""哔哩签名器（动态密钥获取与请求加签）。

能力：
- 从导航接口获取图片密钥与子密钥并推导混合密钥
- 请求参数规范排序与摘要签名（返回签名与时间戳）
- 内存加文件两级缓存（文件路径由调用方传入）
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import reduce
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths as _paths
from .bili_web import NAV_HEADERS


class WbiSigner:
    """哔哩签名器。"""

    导航接口 = "https://api.bilibili.com/x/web-interface/nav"
    NAV_API = "https://api.bilibili.com/x/web-interface/nav"

    # 置换编码表（来自网页端）
    MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52
    ]

    # 默认密钥文件名（调用方可传入自定义路径覆盖；路径由 paths.products_root() 决定）
    默认密钥文件 = ".wbi_keys.json"
    DEFAULT_KEYS_FILE = ".wbi_keys.json"

    # 内存缓存有效期（秒）与文件缓存有效期（秒）
    内存有效期 = 3600
    文件有效期 = 86400

    _cached_mixin_key: Optional[str] = None
    _cache_expire_time: float = 0.0
    _cached_img_key: Optional[str] = None
    _cached_sub_key: Optional[str] = None

    # 调用锁（保护内存缓存读写）
    _缓存锁 = threading.Lock()
    # 限速锁与上次调用时间戳（元数据接口集中限速用）
    _限速锁 = threading.Lock()
    _上次调用时间: float = 0.0
    # 最小调用间隔（秒）
    最小间隔 = 1.5
    MIN_INTERVAL = 1.5

    @classmethod
    def _解析密钥文件路径(cls, keys_file: Optional[Any] = None) -> Path:
        """解析密钥文件路径（未传入时使用**产物根**下的默认路径）。

        默认路径锚定产物根（默认 `<home>/output/.wbi_keys.json`）而非当前所在目录，
        否则在别处执行命令会在那里凭空多出一个 output/ 目录；显式传入的相对路径
        按容器根（home）解析，兼容拆分前的 `output/.wbi_keys.json` 写法。
        """
        if keys_file is None:
            return _paths.products_root() / cls.DEFAULT_KEYS_FILE
        路径 = Path(str(keys_file))
        return 路径 if 路径.is_absolute() else _paths.home_root() / 路径

    @classmethod
    def _读取文件缓存(cls, 路径: Path) -> Optional[Tuple[str, str, float]]:
        """读取文件缓存，返回图片密钥、子密钥与过期时间（无效返回空）。"""
        try:
            if not 路径.exists():
                return None
            with open(路径, "r", encoding="utf-8") as 读:
                数据 = json.load(读)
            图片密钥 = str(数据.get("img_key") or "")
            子密钥 = str(数据.get("sub_key") or "")
            过期时间 = float(数据.get("expire_at") or 0)
            if len(图片密钥) != 32 or len(子密钥) != 32:
                return None
            if time.time() >= 过期时间:
                return None
            return 图片密钥, 子密钥, 过期时间
        except Exception:
            return None

    @classmethod
    def _写入文件缓存(cls, 路径: Path, 图片密钥: str, 子密钥: str, 过期时间: float) -> None:
        """原子写入文件缓存（仅保存密钥与有效期，不保存用户凭证）。"""
        临时 = 路径.with_suffix(f".tmp.{os.getpid()}")
        try:
            路径.parent.mkdir(parents=True, exist_ok=True)
            with open(临时, "w", encoding="utf-8") as 写:
                json.dump(
                    {"img_key": 图片密钥, "sub_key": 子密钥, "expire_at": 过期时间},
                    写,
                    ensure_ascii=False,
                    indent=2,
                )
                写.flush()
            os.replace(临时, 路径)
        except Exception:
            try:
                if 临时.exists():
                    临时.unlink()
            except Exception:
                pass

    @classmethod
    def wait_rate_limit(cls) -> None:
        """集中限速等待（保证元数据接口调用间隔不小于最小间隔，线程安全）。"""
        with cls._限速锁:
            现在 = time.monotonic()
            距离 = 现在 - cls._上次调用时间
            if 距离 < cls.MIN_INTERVAL:
                time.sleep(cls.MIN_INTERVAL - 距离)
            cls._上次调用时间 = time.monotonic()

    @classmethod
    def get_mixin_key(cls, orig: str) -> str:
        """按置换表打乱原始拼接密钥并取前三十二位。"""
        return reduce(lambda s, i: s + orig[i], cls.MIXIN_KEY_ENC_TAB, "")[:32]

    @classmethod
    def get_wbi_keys(cls, sessdata: Optional[str] = None) -> Tuple[str, str]:
        """从导航接口获取图片密钥与子密钥。"""
        cls.wait_rate_limit()
        头 = dict(NAV_HEADERS)
        if sessdata:
            头["Cookie"] = f"SESSDATA={sessdata}"

        请求 = urllib.request.Request(cls.NAV_API, headers=头)
        try:
            with urllib.request.urlopen(请求, timeout=10) as 响应:
                数据 = json.loads(响应.read().decode("utf-8"))
        except urllib.error.HTTPError as 错误:
            if 错误.code == 412:
                raise RuntimeError(
                    "[风控]导航接口被风控拦截（412）。"
                    "建议动作：携带有效登录凭证后降低频率重试，或稍后再试。"
                ) from 错误
            if 500 <= 错误.code <= 599:
                raise RuntimeError(
                    f"[网络]导航接口服务端异常（{错误.code}）。"
                    "建议动作：等待后退避重试。"
                ) from 错误
            raise RuntimeError(
                f"[网络]导航接口请求失败（{错误.code}）。"
                "建议动作：检查网络后重试。"
            ) from 错误
        except Exception as 错误:
            raise RuntimeError(
                f"[网络]导航接口请求失败：{错误}。"
                "建议动作：检查网络连接后重试。"
            ) from 错误

        if not isinstance(数据, dict):
            raise RuntimeError(
                "[风控]导航接口返回异常：非 JSON 数据。"
                "建议动作：确认凭证有效后稍后重试。"
            )
        编码 = 数据.get("code")
        信息 = 数据.get("message")
        if 编码 == -412:
            raise RuntimeError(
                "[风控]导航接口返回风控拦截（-412）。"
                "建议动作：降低请求频率并携带有效登录凭证后重试。"
            )
        # code=-101（账号未登录）不致命：签名公钥 wbi_img 照常下发，仅登录态相关字段缺失。
        # 只要能取出有效密钥就继续；取不出才报错。
        if 编码 not in (0, -101):
            raise RuntimeError(
                f"[风控]导航接口返回异常：code={编码}，msg={信息}。"
                "建议动作：确认凭证有效后稍后重试。"
            )

        图片区 = (数据.get("data") or {}).get("wbi_img") or {}
        图地址 = 图片区.get("img_url", "")
        子地址 = 图片区.get("sub_url", "")

        # 密钥取地址基名去掉扩展名部分
        图片密钥 = 图地址.split("/")[-1].split(".")[0] if 图地址 else ""
        子密钥 = 子地址.split("/")[-1].split(".")[0] if 子地址 else ""
        if len(图片密钥) != 32 or len(子密钥) != 32:
            raise RuntimeError(
                "[签名]导航接口未返回有效签名密钥。"
                "建议动作：稍后重试；若持续为空请检查是否被风控拦截。"
            )
        return 图片密钥, 子密钥

    @classmethod
    def obtain_mixin_key(
        cls,
        sessdata: Optional[str] = None,
        keys_file: Optional[Any] = None,
    ) -> str:
        """获取混合密钥（内存加文件两级缓存，有效期内复用，过期重取）。

        无有效密钥时明确报错，不使用任何假密钥兜底。
        """
        路径 = cls._解析密钥文件路径(keys_file)
        现在 = time.time()

        # 先查内存缓存
        with cls._缓存锁:
            if cls._cached_mixin_key and 现在 < cls._cache_expire_time:
                return cls._cached_mixin_key

        # 再查文件缓存
        文件命中 = cls._读取文件缓存(路径)
        if 文件命中 is not None:
            图片密钥, 子密钥, 过期时间 = 文件命中
            混合密钥 = cls.get_mixin_key(图片密钥 + 子密钥)
            with cls._缓存锁:
                cls._cached_mixin_key = 混合密钥
                cls._cached_img_key = 图片密钥
                cls._cached_sub_key = 子密钥
                # 内存有效期取文件剩余有效期与内存上限的较小值
                cls._cache_expire_time = min(过期时间, time.time() + cls.内存有效期)
            return 混合密钥

        # 缓存均未命中则请求导航接口
        try:
            图片密钥, 子密钥 = cls.get_wbi_keys(sessdata=sessdata)
        except RuntimeError:
            raise
        except Exception as 错误:
            raise RuntimeError(
                f"[签名]获取签名密钥失败：{错误}。"
                "建议动作：检查网络后重试；频繁失败请降低频率。"
            ) from 错误

        混合密钥 = cls.get_mixin_key(图片密钥 + 子密钥)
        过期时间 = time.time() + cls.文件有效期
        with cls._缓存锁:
            cls._cached_mixin_key = 混合密钥
            cls._cached_img_key = 图片密钥
            cls._cached_sub_key = 子密钥
            cls._cache_expire_time = time.time() + cls.内存有效期
        cls._写入文件缓存(路径, 图片密钥, 子密钥, 过期时间)
        return 混合密钥

    @classmethod
    def enc_wbi(
        cls,
        params: Dict[str, Any],
        sessdata: Optional[str] = None,
        keys_file: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """对请求参数加签并追加签名与时间戳。"""
        try:
            混合密钥 = cls.obtain_mixin_key(sessdata=sessdata, keys_file=keys_file)
        except RuntimeError:
            raise
        except Exception as 错误:
            raise RuntimeError(
                f"[签名]签名失败：{错误}。"
                "建议动作：删除过期密钥文件后重试。"
            ) from 错误
        当前 = dict(params)
        当前["wts"] = int(time.time())

        # 按键名字典序排序
        排序键 = sorted(当前.keys())
        # 过滤值中的特殊字符
        过滤对 = []
        for 键 in 排序键:
            值 = str(当前[键])
            清洗 = "".join([字 for 字 in 值 if 字 not in "!'()*"])
            过滤对.append(f"{urllib.parse.quote_plus(str(键))}={urllib.parse.quote_plus(清洗)}")

        查询串 = "&".join(过滤对)
        签名 = hashlib.md5((查询串 + 混合密钥).encode("utf-8")).hexdigest()
        当前["w_rid"] = 签名
        return 当前
