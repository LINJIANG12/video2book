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

**串台只靠时间轴抓**：同一次实测里，服务端会把**别的稿件**的完整字幕按本集时长发回来
（实测串台来源：LOL 赛事实况、iPhone 16 评测、蔡徐坤消费主义评论、短剧对白、导航语音、
分体水冷、惠普维修…）。这类文件的长度、cue 密度全部正常，故以**时长上界**
（`SUBTITLE_OVERRUN_MAX`：ai 轨末条远超本集时长即判串台）拦截。

原先还叠了一条**标题术语身份判据**（`subtitle_identity`：本集标题术语是否出现在正文里）。
实测它在真实课程上假杀率高达 **90%**——`CN_SUBWINDOW_WIDTH=3` 把术语切碎（「哈夫曼树」→「夫曼树」、
「树习题-TTA.1 题意理解」→「树习」/「意理解」），而 AI 字幕写的是「哈弗曼」「tree traversals
again」「便利」；20 次拒收里 18 次是本集内容。且它拦不住同为时间轴对齐的串台。该判据已整条删除：
`subtitle_identity` / `course_terms` / `course_overlap` 保留为纯函数供诊断与测试，不再参与接受判定。

实测服务端对同一 cid 每次返回的内容都不同（同一页反复分别拿到别家视频、空 URL、正确字幕），
因此**越界类失败也按 `IDENTITY_RETRY_MAX` 轮重取**，重取有实效。
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .bili_web import BROWSER_HEADERS
from .fetcher import _请求元数据
from .wbi import WbiSigner

# 播放器信息接口（wbi 签名版）；**登录后** `data.subtitle.subtitles` 才非空
PLAYER_V2_API = "https://api.bilibili.com/x/player/wbi/v2"

# 免签名老端点：返回同一份 `data.subtitle.subtitles`，但不做 wbi 签名。
# 用途：部分网络出口 IP 会被 wbi 端点持续 412 风控，而老端点正常；此时降级到这里。
PLAYER_V2_API_LEGACY = "https://api.bilibili.com/x/player/v2"

# 端点选择：auto（默认，wbi 优先、失败降级老端点）/ legacy（直连老端点）/ wbi（只用 wbi）。
# wbi 端点被封的出口上，每集都要先吃完 wbi 的 412 退避重试才降级，代价可观；
# 确认该出口 wbi 必挂时用 BVB_SUBTITLE_ENDPOINT=legacy 走快路径。
ENV_SUBTITLE_ENDPOINT = "BVB_SUBTITLE_ENDPOINT"

# AI 字幕的语言前缀（实测 AI 为 `ai-zh`，人工为 `zh-Hans` / `zh-CN`）
_AI_LAN_PREFIX = "ai-"

# 字幕覆盖度下限：末条字幕时间 / 分集时长低于此值即视为字幕被截断，不采用
SUBTITLE_COVERAGE_MIN = 0.9

# 时长上界（**只对 ai 轨**）：末条超过分集时长此比例即判串台——串台文件长度不受本集约束，
# 实测「分体水冷」2937s/973s = 302%、「惠普维修」357s/248s = 144% 都栽在这条上。
# 人工 CC 可能有片尾冗余，不设上界（只保留下限截断判据）。
SUBTITLE_OVERRUN_MAX = 1.1
# 轻微越界（>1.02 且 ≤ 上界）：仍采用，但标 `trustworthy=False` 并在抬头留痕。
SUBTITLE_OVERRUN_WARN = 1.02

# 身份类故障（URL 不符 / 时长越界）的重试预算，与"残缺正文"类的 `BVB_SUBTITLE_ATTEMPTS` 同值。
# 实测单次抽中**正确**轨只有约 28%（29 次抽样命中的 8 次），坏抽签里 URL 失配约占 45%、
# 空 URL 约占 24%——三者的共同点是「再抽一次就是全新一张签」。3 轮只覆盖 62.7%，
# 剩下的会被误判成"本集无字幕"并白烧一次听音兜底。10 轮覆盖 96.3%，最坏退避仅 22.5 秒。
IDENTITY_RETRY_MAX = 10

# 身份判据的故障原因前缀（`_身份故障` 依此分类）
REASON_TITLE_MISMATCH = "内容与分集标题不符"
REASON_TIME_OVERRUN = "时长越界"
# URL 身份不符：AI 字幕文件名（`/prod/<aid><cid><hash>`）与本次请求的 aid+cid 对不上。
# 这是**服务端串台**的精确指纹——实测 21 个样本零误判（8 次判等全是本集内容、
# 13 次判不等全是别家内容），且能拦住时长贴合（覆盖率 98.3%）的错件，
# 那是两条时长判据放行、唯一能靠它拦下的形态。
REASON_URL_MISMATCH = "URL 身份不符"
# 响应里一条中文轨都没有。实测显示：空轨与串台是同一服务端调度故障的两种随机表现，
# 并非定论。它与身份族统一共享完整的重试上限，避免过早收手造成假杀。
REASON_NO_TRACK = "无中文字幕轨"

# AI 字幕文件名前缀：`.../bfs/ai_subtitle/prod/<aid><cid><hash>?...`
# 实测正确样本：数据结构课 aid=55114968 cid=96352452 → `5511496896352452…`（16 位十进制）；
# MySQL 课 aid=1605901665 cid=1586219112 → `160590166515862191129…`。
# **必须用前缀匹配、且 aid 与 cid 两段都拼**：只比 aid 时，aid 为 `55114968x` 的视频
# 会产生同前缀碰撞；拼上 cid 后实际不可能碰撞。
# 注意人工 CC 轨不走 `/prod/` 路径，本判据对它不适用（见 `_取字幕一次`）。
AI_SUBTITLE_PATH_RE = re.compile(r"/prod/(\d+)")

# 行首时间戳：字幕稿的特征（听音稿被明令禁止标注时间戳），用于 `transcript_source` 判来源。
时间戳行_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?\]", re.MULTILINE)
# 来源行的加粗标签：`> 来源：**B 站字幕…**` 里的 `B 站字幕…`——整行搜关键词会判反。
来源标签_RE = re.compile(r"\*\*(.+?)\*\*")

# 内容级重试：同一分集被 CDN 截断 / 服务端串台 / 空 URL / 偶发无轨时重试几轮（线性退避，轮次 × 该基数）。
# 实测单次抽中正确轨约 28%，且**每次抽签独立**——重试不是等故障恢复，是重新抽。
# 10 轮把单集覆盖率从 3 轮的 62.7% 提到 96.3%。环境变量只能调低（批量任务想更快时用）。
ENV_SUBTITLE_ATTEMPTS = "BVB_SUBTITLE_ATTEMPTS"
DEFAULT_SUBTITLE_ATTEMPTS = 10
MAX_SUBTITLE_ATTEMPTS = 10
# 无轨重试上限：与总上限统一为 10，不再提前截断。
NO_TRACK_RETRY_MAX = 10
# 抽签重试退避：同一分集被 CDN 截断 / 服务端串台 / 空 URL 时每轮抽签之间的间隔。
# 实测串台是 B 站负载均衡每次随机丢一张签，换签不需要长久等待；且底层 `_请求元数据`
# 本身已有 1.5 秒安全限速保护，故外层退避基数定为 0.5 秒（10 轮累计仅 22.5 秒）。
SUBTITLE_RETRY_BACKOFF_SEC = 0.5


def _重试次数() -> int:
    """字幕取回的重试轮数，取值范围 `[1, MAX_SUBTITLE_ATTEMPTS]`。

    环境变量写坏时退回默认；写成大于上限的值时**钳到上限**——上限是取回耗时的硬约束，
    不允许靠环境变量绕开。
    """
    原始 = str(os.environ.get(ENV_SUBTITLE_ATTEMPTS, "") or "").strip()
    try:
        return max(1, min(MAX_SUBTITLE_ATTEMPTS, int(原始)))
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


def chinese_subtitle_candidates(字幕组: Any) -> List[Dict[str, Any]]:
    """全部中文字幕候选，按「人工优先、简体优先」排序（无中文返回空表）。"""
    候选 = [s for s in (字幕组 or []) if isinstance(s, dict) and _is_chinese(s)]

    def 排序键(条目: Dict[str, Any]) -> tuple:
        lan = str(条目.get("lan") or "").lower()
        简体 = 0 if ("hans" in lan or lan in ("zh", "zh-cn")) else 1
        return (1 if _is_ai(条目) else 0, 简体)

    return sorted(候选, key=排序键)


def pick_chinese_subtitle(字幕组: Any) -> Optional[Dict[str, Any]]:
    """只取中文；人工优先于 AI，简体优先于其它中文变体。无中文返回 None。"""
    候选 = chinese_subtitle_candidates(字幕组)
    return 候选[0] if 候选 else None


def _绝对地址(地址: Any) -> str:
    文本 = str(地址 or "").strip()
    return "https:" + 文本 if 文本.startswith("//") else 文本


def _请求头(sessdata: Optional[str]) -> Dict[str, str]:
    请求头 = dict(BROWSER_HEADERS)
    if sessdata:
        请求头["Cookie"] = f"SESSDATA={sessdata}"
    return 请求头


def _正文到线索(正文: Any) -> List[Dict[str, Any]]:
    """字幕 JSON → 线索表。`to` 必须留着：串台判定要看末条真实终点，丢了就只能退回 `from`。"""
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
        try:
            终点 = float(条.get("to") or 0.0)
        except (TypeError, ValueError):
            终点 = 0.0
        线索.append({"from": 起始, "to": 终点, "content": 内容})
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
    """字幕覆盖度 = 末条字幕终点 / 分集时长。

    终点优先取 `to`（末条字幕的结束时刻才是真实覆盖终点），缺 `to` 时退回 `from`。
    时长未知（<=0）或没有线索时返回 1.0——无法判断就不拦截。否则返回 0~1 的比值：
    远低于 1 说明字幕只覆盖了开头一段（截断），直接采用会静默漏掉后半程内容；
    远高于 `SUBTITLE_OVERRUN_MAX` 说明这份字幕的时间轴不属于本集（见 `subtitle_time_band`）。
    """
    if not cues or duration_sec <= 0:
        return 1.0
    时间 = [
        float(条.get("to") or 条.get("from") or 0.0)
        for 条 in cues if isinstance(条, dict)
    ]
    if not 时间:
        return 1.0
    return max(时间) / duration_sec


def subtitle_time_band(coverage: float, is_ai: bool) -> str:
    """覆盖度落在哪一带：`short`（截断，重取）/ `ok` / `warn`（轻微越界，采用但标注）/ `overrun`（判串台，重取）。

    上界只对 ai 轨生效：串台字幕是平台 AI 轨生成的，长度不受本集约束；
    人工 CC 可能带片尾冗余，只保留下限判据，不因此判串台。
    """
    if coverage < SUBTITLE_COVERAGE_MIN:
        return "short"
    if not is_ai:
        return "ok"
    if coverage > SUBTITLE_OVERRUN_MAX:
        return "overrun"
    return "warn" if coverage > SUBTITLE_OVERRUN_WARN else "ok"


def _身份故障(原因: str) -> bool:
    """是否身份类故障（URL 不是本集 / 时长越界 / 标题不符）：这类按 `IDENTITY_RETRY_MAX` 预算重试。"""
    文本 = str(原因 or "")
    return (文本.startswith(REASON_URL_MISMATCH)
            or 文本.startswith(REASON_TITLE_MISMATCH)
            or 文本.startswith(REASON_TIME_OVERRUN))


def url_identity(url: str, aid: Any, cid: Any) -> Optional[bool]:
    """AI 字幕文件的 URL 是否属于本集：`True` 相符 / `False` 不符 / `None` 判据不适用。

    B 站 AI 字幕文件的路径是 `/bfs/ai_subtitle/prod/<aid><cid><hash>`。服务端偶发把
    **别的视频**的字幕文件挂到本集的轨道上，此时 `aid`/`cid` 两段都对不上——
    这是唯一能在同一次响应内、零额外请求识破串台的信号（实测 21 样本零误判）。

    **判据不适用（返回 None）的两种情形**，调用方必须放行而不能拦：
    - URL 不含 `/prod/`：人工 CC 轨与老式路径不走这套命名；
    - 拿不到 `aid`：缺失字段不该阻塞整条链路。

    返回 `True` 只表示"文件名前缀属于本集"，不表示正文完整——时长判据仍须照跑
    （正确文件也可能被 CDN 截断）。
    """
    文本 = str(url or "")
    匹配 = AI_SUBTITLE_PATH_RE.search(文本)
    if not 匹配:
        return None
    try:
        aid_text = str(int(aid))
        cid_text = str(int(cid))
    except (TypeError, ValueError):
        return None
    if not aid_text or not cid_text:
        return None
    return 匹配.group(1).startswith(aid_text + cid_text)


def transcript_source(path: Any) -> str:
    """已落盘逐字稿的**来源**：`"subtitle"` / `"audio"` / `"mixed"` / `"unknown"`。

    为什么靠推断而不是读字段：听音逐字稿是**派发子 agent 手写落盘**的
    （`taskbook.py` 的转录指令 + `prompts.py` 的派发文案），代码侧的
    `write_block_transcript` 唯一调用点在字幕服务——听音链路根本不经过代码，
    所以历史上那 91 份听音稿连抬头都没有。但有个稳定信号可用：

    - **字幕稿必有行首 `[MM:SS]`**，且 `assemble_block_transcript` 会写 `> 来源：` 抬头；
    - **听音稿必无时间戳**——`TRANSCRIBE_INSTRUCTION` 明令「不需要也不得标注时间戳」。

    两个信号任一命中即判字幕稿；都没有则判听音稿（宁可判听音，因为判听音的后果是
    "拒绝降级"，安全方向）。文件缺失/为空返回 `unknown`。

    `"mixed"`：抬头标签同时含「听音」与「字幕」（`merge_block_transcript` 产出的
    逐集混合稿）。它与 `"audio"` 走同一保守分支——都**拒绝被字幕稿覆盖**，因为混合稿
    里的听音段无法用字幕重建。

    用途：`--force` 重取时据此**拒绝用字幕稿覆盖听音稿**——实测发生过 45721 字节的
    听音稿（繁体、成段通顺）被字幕碎片（简体、逐句时间戳）覆盖，且不可逆。
    """
    路径 = Path(str(path))
    try:
        if not 路径.is_file() or 路径.stat().st_size <= 0:
            return "unknown"
        头部 = 路径.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return "unknown"
    if not 头部.strip():
        return "unknown"
    for 行 in 头部.split("\n"):
        if 行.lstrip().startswith(("> 来源：", "> 来源:", ">来源：", ">来源:")):
            # 判定落在**加粗的来源标签**上（`**B 站字幕…**` / `**听音转录**` / `**混合（…）**`），
            # 而不是整行关键词——字幕稿的抬头里恰好写着「**非**听音转录，由平台字幕拼装」，
            # 整行搜关键词会把两种来源判反。
            标签 = 来源标签_RE.search(行)
            文本 = 标签.group(1) if 标签 else 行
            含听音 = "听音" in 文本
            含字幕 = "字幕" in 文本
            if 含听音 and 含字幕:
                return "mixed"
            if 含听音:
                return "audio"
            if 含字幕:
                return "subtitle"
            return "audio"      # 有来源行但认不出标签：按听音处理（安全方向）
    if 时间戳行_RE.search(头部):
        return "subtitle"
    return "audio"



# 进程级粘性标记：一旦 wbi 端点在 auto 模式下遭遇 412 风控，后续分集直接直连老端点，
# 不再每集重复尝试并白吃 14 秒风控退避（145 集立省 33+ 分钟）。
_wbi_degraded: bool = False
_wbi_lock = threading.Lock()


def reset_wbi_degraded() -> None:
    """重置 wbi 降级标记（测试或凭证刷新后用）。"""
    global _wbi_degraded
    with _wbi_lock:
        _wbi_degraded = False


def _取播放器数据(
    bvid: str,
    cid: int,
    sessdata: Optional[str],
    keys_file: Optional[Any],
) -> Dict[str, Any]:
    """取播放器信息（含字幕清单），按 `BVB_SUBTITLE_ENDPOINT` 选端点。

    `auto`（默认）先打 wbi 端点。若遭遇 412 风控，立即粘性降级到免签名老端点，
    后续分集无需再次试错，避免 145 集每集白等 14 秒。
    """
    global _wbi_degraded
    裸参数 = urllib.parse.urlencode({"bvid": bvid, "cid": cid})
    模式 = (os.environ.get(ENV_SUBTITLE_ENDPOINT) or "auto").strip().lower()

    with _wbi_lock:
        degraded = _wbi_degraded

    if 模式 == "legacy" or (degraded and 模式 != "wbi"):
        return _请求元数据(f"{PLAYER_V2_API_LEGACY}?{裸参数}", _请求头(sessdata), 超时=15)
    if 模式 == "wbi":
        参数 = WbiSigner.enc_wbi({"bvid": bvid, "cid": cid}, sessdata=sessdata, keys_file=keys_file)
        return _请求元数据(f"{PLAYER_V2_API}?{urllib.parse.urlencode(参数)}", _请求头(sessdata), 超时=15)

    # auto 模式：WBI 探测时禁用底层 3 轮指数退避重试（重试=False），
    # 遇到 412 瞬间捕获并降级，首集耗时 0.2s 而非 14s。
    try:
        参数 = WbiSigner.enc_wbi({"bvid": bvid, "cid": cid}, sessdata=sessdata, keys_file=keys_file)
        return _请求元数据(f"{PLAYER_V2_API}?{urllib.parse.urlencode(参数)}", _请求头(sessdata), 超时=15, 重试=False)
    except Exception as 错误:
        with _wbi_lock:
            if not _wbi_degraded:
                _wbi_degraded = True
                print(f"    [降级]wbi 字幕端点不可用（{错误}），后续分集自动直连免签名老端点")
        return _请求元数据(f"{PLAYER_V2_API_LEGACY}?{裸参数}", _请求头(sessdata), 超时=15)


def _取字幕一次(
    bvid: str,
    cid: int,
    sessdata: Optional[str],
    keys_file: Optional[Any],
    duration_sec: float,
    title: str = "",
):
    """单次取中文字幕，返回 `(字幕 or None, 瞬时原因)`。

    瞬时原因为空串表示**不是**可重试故障：字幕非空即成功；字幕为 `None` 且原因为空
    说明该集确实没有中文字幕（重试也不会有）。非空原因包含身份族（URL 不符 / 时长越界）
    与瞬时族（无轨 / 地址为空 / 正文为空 / 覆盖不足 / 网络异常），均由
    `fetch_episode_subtitle` 在总重试预算内退避重取。

    候选轨按「人工优先、简体优先」逐个验；多条候选轨若均不可用，按诊断优先级
    保留最有价值的失败原因（身份故障 > 覆盖不足 > 网络异常 > 空值）。
    """
    数据 = _取播放器数据(bvid, cid, sessdata, keys_file)
    编码 = 数据.get("code")
    if 编码 != 0:
        raise RuntimeError(
            f"[风控]字幕接口异常：code={编码}，msg={数据.get('message')}。"
            "建议动作：先 `python src/cli.py login --sessdata \"<SESSDATA>\"` 配好登录态再重试"
            "（B 站字幕清单登录后才可见）。"
        )
    载荷 = 数据.get("data") or {}
    # 响应身份断言：接口层误发（实测未观察到，5 页 6 次取回 data.bvid/cid 恒等）也要挡住，
    # 否则会把别的稿件的字幕当成这一集的。
    响应cid = 载荷.get("cid")
    if 响应cid and int(响应cid) != int(cid):
        return None, f"响应身份不符（cid={响应cid}≠{cid}）"
    响应bvid = str(载荷.get("bvid") or "")
    if 响应bvid and 响应bvid.upper() != str(bvid).upper():
        return None, f"响应身份不符（bvid={响应bvid}≠{bvid}）"
    # 上一条断言拦不住串台：实测 29/29 次响应的 cid/bvid 都正确回显，错的只是
    # `subtitle_url` 指向的文件。真正的判据在下面对每条候选轨做 URL 前缀比对。
    响应aid = 载荷.get("aid")

    字幕组 = (载荷.get("subtitle") or {}).get("subtitles") or []
    候选组 = chinese_subtitle_candidates(字幕组)
    if not 候选组:
        return None, REASON_NO_TRACK

    最后原因 = ""
    失配样本 = ""

    def _更新原因(新原因: str) -> None:
        nonlocal 最后原因
        if not 最后原因 or _身份故障(新原因) or not _身份故障(最后原因):
            最后原因 = 新原因

    for 选中 in 候选组:
        正文地址 = _绝对地址(选中.get("subtitle_url"))
        if not 正文地址:
            # 实测：接口偶发给出 `subtitle_url` 为空串的中文轨（同页三次里两次空串）；
            # 直接请求会因"无主机名"抛异常中断整条命令，必须当成可重试的瞬时故障。
            _更新原因("地址为空")
            continue
        # URL 身份锚点：AI 字幕文件名内嵌 `aid+cid`，与响应回显的 aid/cid 对不上即串台。
        # 放在取正文之前——拦下的样本连正文都不用下载，且能拦住时长贴合的错件。
        url_ok = url_identity(正文地址, 响应aid, 响应cid or cid)
        if url_ok is False:
            前缀 = (AI_SUBTITLE_PATH_RE.search(正文地址) or [None, ""])[1]
            _更新原因(f"{REASON_URL_MISMATCH}（文件名 {前缀[:20]} 而非 {响应aid}{响应cid or cid}）")
            失配样本 = 失配样本 or 前缀[:20]
            continue
        try:
            线索 = _正文到线索(_请求元数据(正文地址, _请求头(sessdata), 超时=20))
        except Exception as 错误:
            _更新原因(f"正文下载异常（{错误}）")
            continue
        if not 线索:
            _更新原因("正文为空")
            continue
        is_ai = _is_ai(选中)
        覆盖 = subtitle_coverage(线索, duration_sec)
        时段 = subtitle_time_band(覆盖, is_ai)
        末条 = max(float(条.get("to") or 条.get("from") or 0.0) for 条 in 线索)
        if 时段 == "short":
            _更新原因(f"覆盖不足（末条 {末条:.0f}s / 时长 {duration_sec:.0f}s = {覆盖:.0%}）")
            continue
        if 时段 == "overrun":
            # 串台文件的长度不受本集约束（实测 2937s/973s、357s/248s），上界只对 ai 轨生效
            _更新原因(f"{REASON_TIME_OVERRUN}（末条 {末条:.0f}s / 时长 {duration_sec:.0f}s = {覆盖:.0%}）")
            continue
        return {
            "is_ai": is_ai,
            "lan": str(选中.get("lan") or ""),
            "lan_doc": str(选中.get("lan_doc") or ""),
            "cues": 线索,
            "coverage": 覆盖,
            # 轻微越界（>2%）仍采用，但标不可信：平台冗余与串台在同一条谱上，留痕给人工判断
            "trustworthy": 时段 != "warn",
            "audit": {
                "band": 时段,
                "last_cue_sec": round(末条, 2),
                # None = 该轨不适用 URL 锚点（人工 CC 或拿不到 aid），
                # 单纯看一眼产物的可信度来源，不参与任何判定
                "url_identity": url_ok,
            },
        }, ""
    return None, 最后原因


def _归类故障原因(原因: str) -> str:
    """提取核心原因分类供全局统计（去除长括号、URL、文件名等细节）。"""
    文本 = str(原因 or "").strip()
    if not 文本:
        return "未知原因"
    if 文本.startswith(REASON_URL_MISMATCH):
        return REASON_URL_MISMATCH
    if 文本.startswith(REASON_NO_TRACK):
        return REASON_NO_TRACK
    if 文本.startswith(REASON_TIME_OVERRUN):
        return REASON_TIME_OVERRUN
    if 文本.startswith("覆盖不足"):
        return "覆盖不足"
    if "下载异常" in 文本 or "网络" in 文本 or "接口" in 文本:
        return "网络/接口异常"
    if 文本.startswith("正文为空"):
        return "正文为空"
    if 文本.startswith("地址为空"):
        return "地址为空"
    if 文本.startswith("响应身份不符"):
        return "响应身份不符"
    return 文本.split("（")[0].split("(")[0].strip()


def fetch_episode_subtitle(
    bvid: str,
    cid: int,
    sessdata: Optional[str] = None,
    keys_file: Optional[Any] = None,
    duration_sec: float = 0.0,
    title: str = "",
    课程术语: Any = None,
    次数: Optional[int] = None,
    诊断: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """取单集的**中文字幕**；没有中文字幕（或重试后仍不可用）返回 None。

    返回 `{is_ai, lan, lan_doc, cues, coverage, trustworthy, audit, attempts}`，
    `cues[].from` / `cues[].to` 为**集内秒数**。`次数` 缺省取 `BVB_SUBTITLE_ATTEMPTS`
    （默认 10、上限 10），线性退避 `SUBTITLE_RETRY_BACKOFF_SEC`。

    **每次抽签独立**（实测单次命中正确轨约 28%），重试是"重新抽"而非"等故障恢复"。
    空轨与串台均为服务端负载均衡/异步分发的随机表现，共享统一的重试预算。
    `诊断` 传入字典会被回填全程统计：`reason`、`attempts`、`coverage`、`kind`、
    `dominant_reason`、`failure_stats`、`last_reason`。
    `title` 与 `课程术语` 保留在签名里只为兼容既有调用方，不再参与任何判定。
    """
    上限 = max(1, int(次数)) if 次数 else _重试次数()
    身份上限 = min(上限, IDENTITY_RETRY_MAX)
    瞬时 = ""
    轮 = 1
    失败统计: Dict[str, int] = {}
    原因历史: List[str] = []

    for 轮 in range(1, 上限 + 1):
        try:
            字幕, 瞬时 = _取字幕一次(bvid, cid, sessdata, keys_file, duration_sec, title)
        except Exception as err:
            字幕, 瞬时 = None, f"接口请求异常（{err}）"

        if 字幕 is not None:
            字幕["attempts"] = 轮
            if 诊断 is not None:
                诊断.update(reason="", attempts=轮, coverage=字幕["coverage"], kind="",
                            dominant_reason="", failure_stats=失败统计, last_reason="")
            return 字幕
        if not 瞬时:
            # 调用方替身（测试）用它表示"定论性无字幕"。生产路径不再产生空原因。
            if 诊断 is not None:
                诊断.update(reason="", attempts=轮, coverage=0.0, kind="",
                            dominant_reason="", failure_stats={}, last_reason="")
            return None

        归类 = _归类故障原因(瞬时)
        失败统计[归类] = 失败统计.get(归类, 0) + 1
        原因历史.append(瞬时)

        if _身份故障(瞬时) and 轮 >= 身份上限:
            print(f"    [拒绝]字幕{瞬时}——身份类故障 {轮} 轮用尽，该块转音频兜底")
            break

        if 轮 < 上限:
            等待 = SUBTITLE_RETRY_BACKOFF_SEC * 轮
            print(f"    [重试]字幕{瞬时}（第 {轮} 次）→ {等待:.1f}s 后重试")
            time.sleep(等待)

    if 诊断 is not None:
        主导原因 = max(失败统计.items(), key=lambda kv: kv[1])[0] if 失败统计 else 瞬时
        统计摘要 = "，".join(f"{k} {v}次" for k, v in 失败统计.items())
        全称原因 = f"{主导原因}（共{轮}轮：{统计摘要}）" if len(失败统计) > 1 else 瞬时
        诊断.update(
            reason=全称原因,
            attempts=轮,
            coverage=0.0,
            kind="identity" if any(_身份故障(r) for r in 原因历史) else "transient",
            dominant_reason=主导原因,
            failure_stats=失败统计,
            last_reason=瞬时,
        )
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


def _核验摘要(subtitles_by_page: Dict[int, Optional[Dict[str, Any]]], segments: List[Dict[str, Any]]) -> str:
    """抬头用的核验摘要：覆盖区间、重取轮次；轻微越界的页点名标不可信。

    标题术语身份判据已删除（假杀率 90%），故不再有"标题身份通过"一栏。
    没有 `audit`（例如调用方传入的替身数据）时返回空串，不往抬头塞空话。
    """
    页 = sorted({int(s.get("page") or 0) for s in segments if isinstance(s, dict)})
    有效 = {p: subtitles_by_page.get(p) for p in 页 if isinstance(subtitles_by_page.get(p), dict)}
    if not 有效 or not any((e.get("audit") or {}) for e in 有效.values()):
        return ""
    轮次 = [int(e.get("attempts") or 1) for e in 有效.values()]
    轮次段 = f"{min(轮次)}–{max(轮次)}"
    覆盖 = [float(e.get("coverage") or 0.0) for e in 有效.values()]
    不可信 = [f"P{p:02d}" for p, e in 有效.items() if e.get("trustworthy") is False]
    if 不可信:
        return (f"> ⚠ 核验：{'、'.join(不可信)} 末条超出分集时长（轻微越界 >"
                f"{SUBTITLE_OVERRUN_WARN:.0%}），已标注为不可信来源；取回 {轮次段} 轮。\n")
    return (f"> 核验：时间轴判据通过（覆盖 {min(覆盖):.1%}–{max(覆盖):.1%}）｜"
            f"取回 {轮次段} 轮。\n")


def _块内片段行(段: Mapping[str, Any], 单元: Mapping[Any, Any], 字幕: Mapping[str, Any]) -> List[str]:
    """把一集的字幕线索折成块内时间轴的行；落在本段源区间之外的行不取。"""
    page = int(段.get("page") or 0)
    label = str(段.get("label") or f"P{page:02d}")
    源起点 = float((单元.get((page, label)) or {}).get("source_offset_sec") or 0.0)
    源终点 = 源起点 + float(段.get("duration_sec") or 0.0)
    偏移 = float(段.get("start_sec") or 0.0) - 源起点
    return [
        f"[{_格式时间(偏移 + float(c['from']))}] {c['content']}"
        for c in 字幕["cues"]
        if 源起点 - 0.5 <= float(c["from"]) < 源终点
    ]


def assemble_block_transcript(
    block: Dict[str, Any],
    subtitles_by_page: Dict[int, Optional[Dict[str, Any]]],
    *,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """把块内各分集中文字幕拼成**块内时间轴**的块级逐字稿正文。

    `subtitles_by_page`：集号 → `fetch_episode_subtitle` 的结果（或 None 表示该集取不到字幕）。
    时间戳按「块内相对秒」重算：`块内起点 + (集内秒 − 该腿在源集内的起点)`，因此超长集的
    上/下腿各只取自己那一段，不会互相覆盖。

    **两类语义**（由 `allow_partial` 决定）：
    - `allow_partial=False`（默认，整块交付）：任一成员分集缺字幕即 `ok=False`（`text=""`
      + `missing_pages`），整块由听音转录兜底。这是历史行为，保持不变。
    - `allow_partial=True`（按集兜底）：已取到字幕的集照常拼装，缺的集在 `missing_pages`
      里列出、`ok` 仍为 True，`text` 是**只含已取到那些集**的正文。调用方据此把可用部分
      落暂存稿、只对缺的集派听音补录，最后用 `merge_block_transcript` 合并。

    两种模式都会返回 `page_sources`：集号 → `"subtitle"` / `"missing"`。
    """
    segments = [s for s in (block.get("segments") or []) if isinstance(s, dict)]
    if not segments:
        return {"ok": False, "reason": "块清单缺少 segments", "missing_pages": [],
                "text": "", "kind_label": "", "page_sources": {}}
    单元 = {
        (int(u.get("page") or 0), str(u.get("label") or "")): u
        for u in (block.get("units") or []) if isinstance(u, dict)
    }

    缺失: List[int] = []
    片段: List[str] = []
    来源: Set[str] = set()
    page_sources: Dict[int, str] = {}
    segment_text: Dict[str, str] = {}
    segment_page: Dict[str, int] = {}
    for 段 in segments:
        page = int(段.get("page") or 0)
        label = str(段.get("label") or f"P{page:02d}")
        segment_page[label] = page
        字幕 = subtitles_by_page.get(page)
        if not 字幕 or not 字幕.get("cues"):
            缺失.append(page)
            page_sources.setdefault(page, "missing")
            continue
        行 = _块内片段行(段, 单元, 字幕)
        if not 行:
            # 有字幕但与本段源区间无交集（时间轴串台/覆盖不全）：同样算这一集缺
            缺失.append(page)
            page_sources.setdefault(page, "missing")
            continue
        来源.add("ai" if 字幕.get("is_ai") else "human")
        page_sources[page] = "subtitle"
        segment_text[label] = "\n".join(行)
        片段.append(segment_text[label])

    if 缺失 and not allow_partial:
        return {
            "ok": False,
            "reason": "缺中文字幕的分集",
            "missing_pages": sorted(set(缺失)),
            "text": "",
            "kind_label": _来源标签(来源),
            "page_sources": page_sources,
            "segment_text": {},
            "segment_page": segment_page,
        }

    block_id = int(block.get("block_id") or 0)
    span = _块内跨度(block, segments)
    标签 = _来源标签(来源)
    缺集 = sorted(set(缺失))
    if 缺集:
        覆盖行 = (f"> ⚠ 本稿**不完整**：{'、'.join(f'P{p:02d}' for p in 缺集)} 未取到字幕，"
                  f"待听音补录后合并；其余分集为 B 站字幕。\n")
    else:
        覆盖行 = ""
    抬头 = (
        f"# BLK{block_id:02d} {span} 块级逐字稿\n\n"
        f"> 来源：**B 站字幕（中文·{标签}）**——非听音转录，由平台字幕直接拼装。\n"
        f"> 字幕由平台生成或上传，可能存在识别错误、断句与专业术语偏差，"
        f"质量与听音转录可能有差距；据此撰写长文时对存疑处保持谨慎。\n"
        f"{覆盖行}"
        f"{_核验摘要(subtitles_by_page, segments)}"
        "\n"
    )
    return {
        "ok": True,
        "reason": "",
        "missing_pages": 缺集,
        "kind_label": 标签,
        "page_sources": page_sources,
        "segment_text": segment_text,
        "segment_page": segment_page,
        "text": 抬头 + "\n\n".join(片段) + "\n" if 片段 else 抬头,
    }


def merge_block_transcript(
    block: Dict[str, Any],
    subtitle_result: Mapping[str, Any],
    audio_texts: Mapping[int, str],
) -> Dict[str, Any]:
    """把「部分字幕稿」与「缺集的听音补录稿」按块内时间轴合并成最终块级逐字稿。

    `subtitle_result`：`assemble_block_transcript(..., allow_partial=True)` 的返回值。
    `audio_texts`：集号 → 该集的听音转录正文（子智能体产出，**不含时间戳**）。

    合并顺序严格按 `segments` 的顺序，所以劈分腿（`P12上`/`P12下`）与整集腿混排时叙事
    顺序不会乱。某个缺集若被劈成多段，补录正文**只插在该集的第一段**，避免重复。

    抬头声明**逐集来源**——这是 `transcript_source` 判 `"mixed"` 的依据，也决定
    `--force` 重跑时这份稿会不会被字幕稿覆盖（混合稿不该被覆盖）。
    """
    segments = [s for s in (block.get("segments") or []) if isinstance(s, dict)]
    page_sources = dict(subtitle_result.get("page_sources") or {})
    segment_text = dict(subtitle_result.get("segment_text") or {})
    字幕页 = {p for p, k in page_sources.items() if k == "subtitle"}
    待补页 = list(subtitle_result.get("missing_pages") or [])
    听音页 = {p for p in 待补页 if str(audio_texts.get(p) or "").strip()}
    未齐 = sorted(p for p in 待补页 if p not in 听音页)

    片段: List[str] = []
    已插页: Set[int] = set()
    for 段 in segments:
        page = int(段.get("page") or 0)
        label = str(段.get("label") or f"P{page:02d}")
        if page in 字幕页:
            文本 = segment_text.get(label)
            if 文本:
                片段.append(文本)
                continue
        if page in 听音页 and page not in 已插页:
            # 劈分腿：整集补录正文只插一次，放在该集的第一段
            已插页.add(page)
            正文 = str(audio_texts.get(page) or "").strip()
            元数据 = ((段.get("source") or {}).get("metadata") or {})
            标题 = str(元数据.get("title") or "").strip()
            标头 = f"[听音转录 · P{page:02d}{(' ' + 标题) if 标题 else ''}]"
            片段.append(f"{标头}\n{正文}")

    block_id = int(block.get("block_id") or 0)
    span = _块内跨度(block, segments)
    字幕集 = "、".join(f"P{p:02d}" for p in sorted(字幕页)) or "无"
    听音集 = "、".join(f"P{p:02d}" for p in sorted(听音页)) or "无"
    抬头 = (
        f"# BLK{block_id:02d} {span} 块级逐字稿\n\n"
        f"> 来源：**混合（B 站字幕 + 听音转录）**——逐集来源见下，非单一来源稿。\n"
        f"> B 站字幕集：{字幕集}；听音转录集：{听音集}。\n"
        f"> 字幕由平台生成，可能存在识别错误与断句偏差；听音稿由讲师原声转录、不含时间戳。\n"
    )
    if 未齐:
        抬头 += f"> ⚠ 仍缺分集：{'、'.join(f'P{p:02d}' for p in 未齐)}，本稿尚不完整。\n"
    return {
        "ok": not 未齐,
        "text": 抬头 + "\n" + "\n\n".join(片段) + "\n",
        "missing_pages": 未齐,
        "page_sources": {**page_sources, **{p: "audio" for p in 听音页}},
    }
