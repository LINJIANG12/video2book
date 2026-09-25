#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站字幕 → 块级逐字稿（可选链路：用平台字幕充当逐字稿，替代听音转录）。

只取**中文**字幕：人工上传的 CC 字幕优先，AI 自动字幕兜底（判定口径见 `_is_ai`）。
产物抬头显式标注「来源为字幕、非听音转录，质量可能有差距」，供写作角色知情。

正确性护栏：一个块内只要有任一成员分集没有中文字幕，就**整块不产出**（返回 `ok=False`
并列出缺失分集），留给听音转录兜底——绝不静默漏掉一部分内容。同理，字幕只覆盖开头一段
（末条字幕时间远早于分集时长）也视为不可用：与音频截断是同一类静默漏内容的故障。

瞬时故障必须重试：字幕 CDN 对**同一 URL** 会时好时坏地返回**残缺正文**——HTTP 200、
JSON 合法，只是内容被截断到开头几分钟（实测同一分集连取 6 次，仅 1 次完整），
`subtitle_url` 字段也会偶发空串。`fetcher._请求元数据` 的重试只看状态码与网络异常，
抓不到这种"成功但内容少"，因此重试与覆盖度判定都收在内容层（见 `fetch_episode_subtitle`）。
否则一次瞬时抖动就会把整块误判成"没字幕"，白扔掉 40~60 分钟的听音兜底成本。
"""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from .bili_web import BROWSER_HEADERS
from .fetcher import _请求元数据
from .wbi import WbiSigner

# 播放器信息接口（wbi 签名版）；**登录后** `data.subtitle.subtitles` 才非空
PLAYER_V2_API = "https://api.bilibili.com/x/player/wbi/v2"

# AI 字幕的语言前缀（实测 AI 为 `ai-zh`，人工为 `zh-Hans` / `zh-CN`）
_AI_LAN_PREFIX = "ai-"

# 字幕覆盖度下限：末条字幕时间 / 分集时长低于此值即视为字幕被截断，不采用
SUBTITLE_COVERAGE_MIN = 0.9

# 内容级重试：同一分集被 CDN 截断时重试几轮（线性退避，轮次 × 该基数）。
# 默认 4 轮：实测单次成功率约 1/6 ~ 1/2，4 轮足够把"整块降级听音"变成"照常走字幕链路"。
ENV_SUBTITLE_ATTEMPTS = "BVB_SUBTITLE_ATTEMPTS"
DEFAULT_SUBTITLE_ATTEMPTS = 4
SUBTITLE_RETRY_BACKOFF_SEC = 1.5


def _重试次数() -> int:
    """字幕取回的重试轮数；环境变量写坏时退回默认（与音频物化层的失败重试口径一致）。"""
    原始 = str(os.environ.get(ENV_SUBTITLE_ATTEMPTS, "") or "").strip()
    try:
        return max(1, int(原始))
    except (TypeError, ValueError):
        return DEFAULT_SUBTITLE_ATTEMPTS


def _is_ai(条目: Dict[str, Any]) -> bool:
    """是否 AI 自动字幕。三条判据任一命中即可。

    为什么不用 `ai_type` / `ai_status`：实测人工与 AI 两种字幕的这两个字段都是 0，
    唯一可靠的区分是 `lan` 前缀、`type` 编号与字幕 URL 路径。
    """
    if str(条目.get("lan") or "").lower().startswith(_AI_LAN_PREFIX):
        return True
    try:
        if int(条目.get("type") or 0) == 1:
            return True
    except (TypeError, ValueError):
        pass
    return "ai_subtitle" in str(条目.get("subtitle_url") or "")


def _is_chinese(条目: Dict[str, Any]) -> bool:
    """是否中文字幕（含 `ai-zh`）。"""
    lan = str(条目.get("lan") or "").lower()
    if lan.startswith(_AI_LAN_PREFIX):
        lan = lan[len(_AI_LAN_PREFIX):]
    return lan.startswith("zh") or "中文" in str(条目.get("lan_doc") or "")


def pick_chinese_subtitle(字幕组: Any) -> Optional[Dict[str, Any]]:
    """只取中文；人工优先于 AI，简体优先于其它中文变体。无中文返回 None。"""
    候选 = [s for s in (字幕组 or []) if isinstance(s, dict) and _is_chinese(s)]
    if not 候选:
        return None

    def 排序键(条目: Dict[str, Any]) -> tuple:
        lan = str(条目.get("lan") or "").lower()
        简体 = 0 if ("hans" in lan or lan in ("zh", "zh-cn")) else 1
        return (1 if _is_ai(条目) else 0, 简体)

    return sorted(候选, key=排序键)[0]


def _绝对地址(地址: Any) -> str:
    文本 = str(地址 or "").strip()
    return "https:" + 文本 if 文本.startswith("//") else 文本


def _请求头(sessdata: Optional[str]) -> Dict[str, str]:
    请求头 = dict(BROWSER_HEADERS)
    if sessdata:
        请求头["Cookie"] = f"SESSDATA={sessdata}"
    return 请求头


def _正文到线索(正文: Any) -> List[Dict[str, Any]]:
    条目 = 正文.get("body") if isinstance(正文, dict) else 正文
    if not isinstance(条目, list):
        return []
    线索: List[Dict[str, Any]] = []
    for 条 in 条目:
        if not isinstance(条, dict):
            continue
        内容 = str(条.get("content") or "").strip()
        if not 内容:
            continue
        try:
            起始 = float(条.get("from") or 0.0)
        except (TypeError, ValueError):
            起始 = 0.0
        线索.append({"from": 起始, "content": 内容})
    return 线索


def resolve_course_bvid(parts: Any) -> str:
    """课程级稿件号：分集自带就用分集自带的，否则从分集 url 里解析。

    `multi_page` 稿件的分集**不带** `bvid`（只有 `ugc_season` 合集才逐集带），
    此时必须回落到课程级稿件号，否则字幕接口无从查起。解析不了（本地课程等）返回 ""。
    """
    条目组 = [p for p in (parts or []) if isinstance(p, dict)]
    for part in 条目组:
        bvid = str(part.get("bvid") or "").strip()
        if bvid:
            return bvid
    from .parser import BilibiliParser  # 函数内导入，避免 core 内模块级循环
    for part in 条目组:
        bvid = BilibiliParser.extract_bvid(str(part.get("url") or "")) or ""
        if bvid:
            return bvid
    return ""


def subtitle_coverage(cues: Any, duration_sec: float) -> float:
    """字幕覆盖度 = 末条字幕时间 / 分集时长。

    时长未知（<=0）或没有线索时返回 1.0——无法判断就不拦截。否则返回 0~1 的比值：
    远低于 1 说明字幕只覆盖了开头一段（截断），直接采用会静默漏掉后半程内容。
    """
    if not cues or duration_sec <= 0:
        return 1.0
    时间 = [float(条.get("from") or 0.0) for 条 in cues if isinstance(条, dict)]
    if not 时间:
        return 1.0
    return max(时间) / duration_sec


def _取字幕一次(
    bvid: str,
    cid: int,
    sessdata: Optional[str],
    keys_file: Optional[Any],
    duration_sec: float,
):
    """单次取中文字幕，返回 `(字幕 or None, 瞬时原因)`。

    瞬时原因为空串表示**不是**可重试故障：字幕非空即成功；字幕为 `None` 且原因为空
    说明该集确实没有中文字幕（重试也不会有）。原因为非空的三种瞬时故障——地址为空、
    正文为空、覆盖度不足——都由 `fetch_episode_subtitle` 退避重试。
    """
    参数 = WbiSigner.enc_wbi({"bvid": bvid, "cid": cid}, sessdata=sessdata, keys_file=keys_file)
    地址 = f"{PLAYER_V2_API}?{urllib.parse.urlencode(参数)}"
    数据 = _请求元数据(地址, _请求头(sessdata), 超时=15)
    编码 = 数据.get("code")
    if 编码 != 0:
        raise RuntimeError(
            f"[风控]字幕接口异常：code={编码}，msg={数据.get('message')}。"
            "建议动作：先 `python src/cli.py login --sessdata \"<SESSDATA>\"` 配好登录态再重试"
            "（B 站字幕清单登录后才可见）。"
        )
    字幕组 = ((数据.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    选中 = pick_chinese_subtitle(字幕组)
    if not 选中:
        return None, ""
    正文地址 = _绝对地址(选中.get("subtitle_url"))
    if not 正文地址:
        # 实测：接口偶发给出 `subtitle_url` 为空串的中文轨；直接请求会因"无主机名"抛异常
        # 中断整条命令，必须当成可重试的瞬时故障。
        return None, "地址为空"
    正文 = _请求元数据(正文地址, _请求头(sessdata), 超时=20)
    线索 = _正文到线索(正文)
    if not 线索:
        return None, "正文为空"
    覆盖 = subtitle_coverage(线索, duration_sec)
    if 覆盖 < SUBTITLE_COVERAGE_MIN:
        return None, (f"覆盖不足（末条 {覆盖 * duration_sec:.0f}s / "
                      f"时长 {duration_sec:.0f}s = {覆盖:.0%}）")
    return {
        "is_ai": _is_ai(选中),
        "lan": str(选中.get("lan") or ""),
        "lan_doc": str(选中.get("lan_doc") or ""),
        "cues": 线索,
        "coverage": 覆盖,
    }, ""


def fetch_episode_subtitle(
    bvid: str,
    cid: int,
    sessdata: Optional[str] = None,
    keys_file: Optional[Any] = None,
    duration_sec: float = 0.0,
    次数: Optional[int] = None,
    诊断: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """取单集的**中文字幕**；没有中文字幕（或重试后仍不可用）返回 None。

    返回 `{is_ai, lan, lan_doc, cues, coverage, attempts}`，`cues[].from` 为**集内秒数**。
    `次数` 缺省取 `BVB_SUBTITLE_ATTEMPTS`（默认 4），线性退避 `SUBTITLE_RETRY_BACKOFF_SEC`。

    只对**瞬时故障**重试（CDN 残缺正文 / 地址为空 / 覆盖度不足）；确实没有中文字幕
    直接返回，不浪费轮次。`诊断` 传入字典会被回填 `reason`/`attempts`/`coverage`，
    供调用方把"没有字幕"与"重试后仍不完整"两种结局分开告知。
    """
    上限 = max(1, int(次数)) if 次数 else _重试次数()
    瞬时 = ""
    for 轮 in range(1, 上限 + 1):
        字幕, 瞬时 = _取字幕一次(bvid, cid, sessdata, keys_file, duration_sec)
        if 字幕 is not None:
            字幕["attempts"] = 轮
            if 诊断 is not None:
                诊断.update(reason="", attempts=轮, coverage=字幕["coverage"])
            return 字幕
        if not 瞬时:
            if 诊断 is not None:
                诊断.update(reason="", attempts=轮, coverage=0.0)
            return None
        if 轮 < 上限:
            等待 = SUBTITLE_RETRY_BACKOFF_SEC * 轮
            print(f"    [重试]字幕{瞬时}（第 {轮} 次）→ {等待:.1f}s 后重试")
            time.sleep(等待)
    if 诊断 is not None:
        诊断.update(reason=瞬时, attempts=上限, coverage=0.0)
    return None


def _格式时间(秒: float) -> str:
    总 = max(0, int(秒))
    分, 秒 = divmod(总, 60)
    时, 分 = divmod(分, 60)
    return f"{时:02d}:{分:02d}:{秒:02d}" if 时 else f"{分:02d}:{秒:02d}"


def _来源标签(kinds: Set[str]) -> str:
    if kinds == {"ai"}:
        return "AI 自动生成"
    if kinds == {"human"}:
        return "人工上传"
    if kinds == {"ai", "human"}:
        return "人工上传 + AI 自动生成"
    return "未知"


def _块内跨度(block: Dict[str, Any], segments: List[Dict[str, Any]]) -> str:
    span = str(block.get("span") or "").strip()
    if span:
        return span
    labels = [str(s.get("label") or "") for s in segments if s.get("label")]
    if not labels:
        return "?"
    return labels[0] if len(labels) == 1 else f"{labels[0]}-{labels[-1]}"


def assemble_block_transcript(
    block: Dict[str, Any], subtitles_by_page: Dict[int, Optional[Dict[str, Any]]]
) -> Dict[str, Any]:
    """把块内各分集中文字幕拼成**块内时间轴**的块级逐字稿正文。

    `subtitles_by_page`：集号 → `fetch_episode_subtitle` 的结果（或 None 表示该集无中文字幕）。
    时间戳按「块内相对秒」重算：`块内起点 + (集内秒 − 该腿在源集内的起点)`，因此超长集的
    上/下腿各只取自己那一段，不会互相覆盖。任一成员分集缺中文字幕即 `ok=False`
    （`text=""` + `missing_pages`），由听音转录兜底。
    """
    segments = [s for s in (block.get("segments") or []) if isinstance(s, dict)]
    if not segments:
        return {"ok": False, "reason": "块清单缺少 segments", "missing_pages": [], "text": "", "kind_label": ""}
    单元 = {
        (int(u.get("page") or 0), str(u.get("label") or "")): u
        for u in (block.get("units") or []) if isinstance(u, dict)
    }

    缺失: List[int] = []
    片段: List[str] = []
    来源: Set[str] = set()
    for 段 in segments:
        page = int(段.get("page") or 0)
        label = str(段.get("label") or f"P{page:02d}")
        字幕 = subtitles_by_page.get(page)
        if not 字幕 or not 字幕.get("cues"):
            缺失.append(page)
            continue
        来源.add("ai" if 字幕.get("is_ai") else "human")
        源起点 = float((单元.get((page, label)) or {}).get("source_offset_sec") or 0.0)
        源终点 = 源起点 + float(段.get("duration_sec") or 0.0)
        偏移 = float(段.get("start_sec") or 0.0) - 源起点
        行 = [
            f"[{_格式时间(偏移 + float(c['from']))}] {c['content']}"
            for c in 字幕["cues"]
            if 源起点 - 0.5 <= float(c["from"]) < 源终点
        ]
        if not 行:
            缺失.append(page)
            continue
        片段.append("\n".join(行))

    if 缺失:
        return {
            "ok": False,
            "reason": "缺中文字幕的分集",
            "missing_pages": sorted(set(缺失)),
            "text": "",
            "kind_label": _来源标签(来源),
        }

    block_id = int(block.get("block_id") or 0)
    span = _块内跨度(block, segments)
    标签 = _来源标签(来源)
    抬头 = (
        f"# BLK{block_id:02d} {span} 块级逐字稿\n\n"
        f"> 来源：**B 站字幕（中文·{标签}）**——非听音转录，由平台字幕直接拼装。\n"
        f"> 字幕由平台生成或上传，可能存在识别错误、断句与专业术语偏差，"
        f"质量与听音转录可能有差距；据此撰写长文时对存疑处保持谨慎。\n\n"
    )
    return {
        "ok": True,
        "reason": "",
        "missing_pages": [],
        "kind_label": 标签,
        "text": 抬头 + "\n\n".join(片段) + "\n",
    }
