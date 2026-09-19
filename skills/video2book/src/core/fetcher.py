"""音频流获取与下载器。

能力：
- 经签名播放地址接口获取音频直链并按码率择优
- 自带防盗链请求头下载并用转封装零损耗落盘
- 元数据接口集中限速、退避重试与熔断保护
"""

import http.cookiejar
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .bili_web import BROWSER_HEADERS as 浏览器请求头
from .bili_web import is_retryable_status
from .wbi import WbiSigner
from .proc import run_quiet


# 集中限速状态（元数据接口调用间隔不小于最小间隔，线程安全）
_限速锁 = threading.Lock()
_上次调用时间: float = 0.0
_最小间隔 = 1.5

# 熔断状态（连续失败计数，线程安全）
_熔断锁 = threading.Lock()
_连续失败次数: int = 0
_熔断阈值 = 5
_最大重试次数 = 3

# 会话复用（全局饼干罐与打开器，凭证仅来自调用方参数）
_饼干罐 = http.cookiejar.CookieJar()
_会话打开器: Optional[Any] = None
_会话锁 = threading.Lock()


def _获取会话打开器() -> Any:
    """获取全局复用的会话打开器（线程安全，复用饼干罐）。"""
    global _会话打开器
    with _会话锁:
        if _会话打开器 is None:
            处理器 = urllib.request.HTTPCookieProcessor(_饼干罐)
            _会话打开器 = urllib.request.build_opener(处理器)
        return _会话打开器


def _等待集中限速() -> None:
    """集中令牌桶等待（本地桶加签名器共享桶双重约束）。"""
    WbiSigner.wait_rate_limit()
    global _上次调用时间
    with _限速锁:
        现在 = time.monotonic()
        距离 = 现在 - _上次调用时间
        if 距离 < _最小间隔:
            time.sleep(_最小间隔 - 距离)
        _上次调用时间 = time.monotonic()


def _检查熔断() -> None:
    """连续失败达阈值时直接停手并给出指引。"""
    with _熔断锁:
        次数 = _连续失败次数
    if 次数 >= _熔断阈值:
        raise RuntimeError(
            f"[风控]熔断停手：元数据接口已连续失败{次数}次，暂停继续请求以免加重风控。"
            "建议动作：检查登录凭证是否有效、降低并发并等待一段时间后重试；"
            "确认网络正常后再手动恢复。"
        )


def _记录成功() -> None:
    """成功后清零连续失败计数。"""
    global _连续失败次数
    with _熔断锁:
        _连续失败次数 = 0


def _记录失败() -> int:
    """失败后递增计数并返回当前值。"""
    global _连续失败次数
    with _熔断锁:
        _连续失败次数 += 1
        return _连续失败次数


def _解析等待秒数(响应头: Any, 轮次: int) -> float:
    """解析服务端建议等待时间，缺失时按指数退避计算。"""
    try:
        原始 = None
        if 响应头 is not None:
            if hasattr(响应头, "get"):
                原始 = 响应头.get("Retry-After") or 响应头.get("retry-after")
            elif isinstance(响应头, dict):
                原始 = 响应头.get("Retry-After") or 响应头.get("retry-after")
        if 原始 is not None:
            return max(1.0, float(str(原始).strip().split(",")[0]))
    except Exception:
        pass
    # 指数退避：2、4、8 秒
    return float(2 ** max(1, 轮次))


def _请求元数据(地址: str, 请求头: Dict[str, str], 超时: int = 15) -> Dict[str, Any]:
    """带限速、重试与熔断的元数据请求（处理风控与服务端异常）。"""
    _检查熔断()
    打开器 = _获取会话打开器()
    最后错误: Optional[Exception] = None
    for 轮次 in range(1, _最大重试次数 + 2):
        _等待集中限速()
        try:
            请求 = urllib.request.Request(地址, headers=请求头)
            with 打开器.open(请求, timeout=超时) as 响应:
                文本 = 响应.read().decode("utf-8")
                数据 = json.loads(文本)
            _记录成功()
            if not isinstance(数据, dict):
                raise RuntimeError("接口返回非预期结构")
            return 数据
        except urllib.error.HTTPError as 错误:
            最后错误 = 错误
            状态 = 错误.code
            响应头 = getattr(错误, "headers", None)
            可重试 = is_retryable_status(状态)
            if 可重试 and 轮次 <= _最大重试次数:
                等待 = _解析等待秒数(响应头, 轮次)
                print(f"[重试]元数据接口状态异常（{状态}），{等待:.1f}秒后重试（第{轮次}次）")
                time.sleep(等待)
                continue
            次数 = _记录失败()
            if 状态 == 412:
                raise RuntimeError(
                    "[风控]元数据接口被风控拦截（412）。"
                    f"建议动作：携带有效登录凭证后降低频率重试；当前连续失败{次数}次。"
                ) from 错误
            if 500 <= 状态 <= 599:
                raise RuntimeError(
                    f"[网络]元数据接口服务端异常（{状态}）。"
                    f"建议动作：退避等待后重试；当前连续失败{次数}次。"
                ) from 错误
            raise RuntimeError(
                f"[网络]元数据接口请求失败（{状态}）。"
                "建议动作：检查网络后重试。"
            ) from 错误
        except RuntimeError:
            raise
        except Exception as 错误:
            最后错误 = 错误
            if 轮次 <= _最大重试次数:
                等待 = float(2 ** 轮次)
                print(f"[重试]元数据接口网络异常：{错误}，{等待:.1f}秒后重试（第{轮次}次）")
                time.sleep(等待)
                continue
            次数 = _记录失败()
            raise RuntimeError(
                f"[网络]元数据接口请求失败：{错误}。"
                f"建议动作：检查网络连接后重试；当前连续失败{次数}次。"
            ) from 错误
    次数 = _记录失败()
    raise RuntimeError(
        f"[网络]元数据接口多次重试仍失败：{最后错误}。"
        f"建议动作：稍后重试；当前连续失败{次数}次，若达{_熔断阈值}次将熔断停手。"
    ) from 最后错误


def _atomic_replace(src: Path, dst: Path, retries: int = 3, delay: float = 0.2) -> None:
    """Windows-safe atomic file replacement with brief retry on transient file locks."""
    for i in range(retries):
        try:
            if dst.exists():
                src.replace(dst)
            else:
                src.rename(dst)
            return
        except PermissionError:
            time.sleep(delay * (i + 1))
        except OSError:
            time.sleep(delay)
    # Final attempt fallback
    if src.exists():
        shutil.move(str(src), str(dst))


class AudioFetcher:
    """音频流获取与极速落盘器。"""

    播放接口 = "https://api.bilibili.com/x/player/wbi/playurl"
    PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"

    DEFAULT_HEADERS = dict(浏览器请求头)
    BROWSER_HEADERS = dict(浏览器请求头)

    # 音频质量映射（标准音频编号）
    AUDIO_QUALITY_MAP = {
        30280: "192kbps (高码率)",
        30232: "132kbps (普通码率)",
        30216: "64kbps (低码率)",
        30250: "杜比全景声 (Dolby Atmos)",
        30251: "Hi-Res 无损",
    }

    @classmethod
    def get_audio_stream_info(
        cls,
        bvid: str,
        cid: int,
        sessdata: Optional[str] = None,
        prefer_quality: str = "low",
        wbi_keys_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """以连播模式请求播放地址并返回候选音频流。"""
        原始参数 = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,   # 连播格式标识
            "fnver": 0,
            "fourk": 1,
        }
        try:
            签名参数 = WbiSigner.enc_wbi(原始参数, sessdata=sessdata, keys_file=wbi_keys_file)
        except RuntimeError:
            raise
        except Exception as 错误:
            raise RuntimeError(
                f"[签名]播放地址签名失败：{错误}。"
                "建议动作：删除过期密钥文件后重试。"
            ) from 错误
        查询串 = urllib.parse.urlencode(签名参数)
        地址 = f"{cls.PLAYURL_API}?{查询串}"

        请求头 = dict(cls.DEFAULT_HEADERS)
        if sessdata:
            # 会话凭证仅来自调用方参数，不落盘、不读环境变量
            请求头["Cookie"] = f"SESSDATA={sessdata}"

        数据 = _请求元数据(地址, 请求头, 超时=15)

        if not 数据 or 数据.get("code") != 0:
            信息 = 数据.get("message") if 数据 else "无响应"
            编码 = 数据.get("code") if 数据 else -1
            if 编码 == -412:
                raise RuntimeError(
                    "[风控]播放地址接口被风控拦截（-412）。"
                    "建议动作：携带有效登录凭证、降低频率后重试。"
                )
            raise RuntimeError(f"[风控]播放地址接口异常：code={编码}，msg={信息}。建议动作：确认凭证有效后重试。")

        结果 = 数据.get("data", {})
        连播 = 结果.get("dash")
        if not 连播:
            raise RuntimeError("[风控]无连播音频流（可能为旧版直连流）。建议动作：更换稿件或稍后重试。")

        音频列表 = 连播.get("audio", [])
        if not 音频列表:
            raise RuntimeError("[风控]连播清单中无音频流。建议动作：确认稿件有声轨后重试。")

        # 按带宽或编号排序
        def 排序键(条目: Dict[str, Any]) -> int:
            return 条目.get("bandwidth", 0) or 条目.get("id", 0)

        # 按偏好选择（默认低码率以适配语音场景）
        偏好 = (prefer_quality or "low").lower()
        if 偏好 == "low":
            候选 = [a for a in 音频列表 if a.get("id") == 30216]
            if 候选:
                选中 = 候选[0]
            else:
                选中 = sorted(音频列表, key=排序键, reverse=False)[0]
        elif 偏好 == "medium":
            候选 = [a for a in 音频列表 if a.get("id") == 30232]
            if 候选:
                选中 = 候选[0]
            else:
                选中 = sorted(音频列表, key=排序键, reverse=False)[0]
        else: # 高码率
            选中 = sorted(音频列表, key=排序键, reverse=True)[0]

        # 取直链（优先主地址，退回备用地址）
        直链 = 选中.get("base_url") or 选中.get("baseUrl")
        if not 直链 and 选中.get("backup_url"):
            直链 = 选中["backup_url"][0]

        质量编号 = 选中.get("id", 0)
        质量描述 = cls.AUDIO_QUALITY_MAP.get(质量编号, f"Audio ID: {质量编号}")

        按序 = sorted(音频列表, key=排序键, reverse=True)

        return {
            "bvid": bvid,
            "cid": cid,
            "best_stream_url": 直链,
            "quality_id": 质量编号,
            "quality_desc": 质量描述,
            "bandwidth": 选中.get("bandwidth"),
            "codecs": 选中.get("codecs"),
            "all_audio_streams": [
                {
                    "id": a.get("id"),
                    "desc": cls.AUDIO_QUALITY_MAP.get(a.get("id", 0), f"ID {a.get('id')}"),
                    "bandwidth": a.get("bandwidth"),
                    "url": a.get("base_url") or a.get("baseUrl"),
                }
                for a in 按序
            ],
        }

    @classmethod
    def download_audio(
        cls,
        stream_url: str,
        output_filepath: str,
        repackage_m4a: bool = True,
        max_bytes: Optional[int] = None,
        sessdata: Optional[str] = None,
    ) -> str:
        """带容错双轨机制的音频下载：优先流式直出 16kHz 人声单声道，失败自动降级分块拉取。"""
        输出 = Path(output_filepath).resolve()
        输出.parent.mkdir(parents=True, exist_ok=True)
        目标 = 输出.with_suffix(".m4a")

        # 幂等性保障：若有效音频已存在（>100KB），直接复用，0 秒重复耗时
        if 目标.exists() and 目标.stat().st_size > 100 * 1024:
            return str(目标)

        临时 = 输出.with_suffix(".tmp.m4a")
        if 临时.exists():
            try:
                临时.unlink()
            except OSError:
                pass

        转码器 = shutil.which("ffmpeg")

        # 1. 优先通道：ffmpeg 携带防盗链 Header 与重连机制，一步直出 16kHz 单声道 32k AAC
        if 转码器 and repackage_m4a and not max_bytes:
            headers = dict(cls.DEFAULT_HEADERS)
            if sessdata:
                headers["Cookie"] = f"SESSDATA={sessdata}"
            header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

            命令 = [
                转码器,
                "-y",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-headers", header_str,
                "-i", stream_url,
                "-vn",
                "-acodec", "aac",
                "-ar", "16000",
                "-ac", "1",
                "-b:a", "32k",
                str(临时),
            ]
            try:
                结果 = run_quiet(命令, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
                if 结果.returncode == 0 and 临时.exists() and 临时.stat().st_size > 10 * 1024:
                    _atomic_replace(临时, 目标)
                    return str(目标)
            except Exception:
                # 异常不中断，自动无缝切入下方兜底通道
                pass
            finally:
                if 临时.exists() and not 目标.exists():
                    try:
                        临时.unlink()
                    except OSError:
                        pass

        # 2. 自动兜底通道：分块流控 HTTP 下载 -> 本地 ffmpeg 极速转码
        临时_m4s = 输出.with_suffix(".raw.m4s")
        req_headers = dict(cls.DEFAULT_HEADERS)
        if sessdata:
            req_headers["Cookie"] = f"SESSDATA={sessdata}"
        请求 = urllib.request.Request(stream_url, headers=req_headers)

        try:
            已下 = 0
            打开器 = _获取会话打开器()
            with 打开器.open(请求, timeout=30) as 响应, open(临时_m4s, "wb") as 写出:
                块大小 = 128 * 1024
                while True:
                    块 = 响应.read(块大小)
                    if not 块:
                        break
                    写出.write(块)
                    已下 += len(块)
                    if max_bytes and 已下 >= max_bytes:
                        break
        except Exception as 错误:
            if 临时_m4s.exists():
                try:
                    临时_m4s.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"[网络]音频下载失败：{错误}。建议动作：检查网络后重试。") from 错误

        # 本地转码为 16kHz 单声道
        if 转码器 and repackage_m4a:
            命令 = [
                转码器,
                "-y",
                "-i", str(临时_m4s),
                "-vn",
                "-acodec", "aac",
                "-ar", "16000",
                "-ac", "1",
                "-b:a", "32k",
                str(临时),
            ]
            try:
                结果 = run_quiet(命令, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
                if 结果.returncode == 0 and 临时.exists() and 临时.stat().st_size > 0:
                    if 临时_m4s.exists():
                        try:
                            临时_m4s.unlink()
                        except OSError:
                            pass
                    _atomic_replace(临时, 目标)
                    return str(目标)
            except Exception:
                pass

        # 若无转码器或失败，原子重命名原始文件
        if 临时_m4s.exists():
            _atomic_replace(临时_m4s, 目标)
        return str(目标)

