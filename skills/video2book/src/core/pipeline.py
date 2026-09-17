#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pipeline Coordination Domain Service（流水线领域调度服务）。

职责：
1. 承载原 CLI 中 `cmd_pipeline` 的两阶段编排逻辑：
   阶段一「音频收齐」→ 阶段二「转录任务派发与落盘」→ 阶段三「知识块聚合」。
2. 收口跨命令共享的领域辅助函数（目标解析、412 富化、范围解析、任务书导出），
   供 CLI 的 audio / transcribe / note / cluster-notes 等命令复用。
3. 通过 PipelineGateError 表达硬门禁终止（等价于原实现中的 sys.exit 非零退出），
   CLI 层仅需捕获并转换为进程退出码。

设计约束：
- 不依赖 argparse 命名空间，参数全部显式传入，可独立单元测试；
- 输出行为与原 CLI 实现逐行一致，保证用户可感知行为零变化。
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.core.parser import BilibiliParser
from src.core.local_media import LocalMediaParser
from src.core.fetcher import AudioFetcher
from src.core.workspace import TaskWorkspace, sanitize_filename
from src.core import fsutil
from src.core import paths as _paths
from src.generator.block_synthesizer import BlockSynthesizer
from src.generator.prompt_templates import ArticlePromptTypeError

# 代码根（skill/）——仅用于断点续跑提示等展示；产物路径一律走 paths.products_root()
PROJECT_ROOT = _paths.code_root()

# 分集作品类型：非视频作品（抖音图文/图集 note）不参与听音与长文生成。
# 见 references/non-video-works.md。
KIND_VIDEO = "video"
KIND_IMAGE_ALBUM = "image_album"


def part_kind(part: Dict[str, Any]) -> str:
    """分集的作品类型；缺失一律按视频处理。

    为什么必须容错：`parts.json` 是**跨版本长期缓存**——B 站 / YouTube / 本地媒体
    三种来源，以及本改动之前建立的所有抖音工作区，条目里都没有 `media_kind`。
    用 `part["media_kind"]` 会 KeyError 让整条流水线崩在阶段一。
    """
    return str(part.get("media_kind") or KIND_VIDEO)


def _resolve_status_file() -> Path:
    """状态文件（记录上次 412/熔断）路径：产物根下唯一一份。

    三域分离后不再探测当前工作目录：在任意目录执行命令都写同一个状态文件，
    不会在别处凭空生成一个 output/。仅保留「读取旧 cwd/output 状态文件」的兼容探测。
    """
    current = _paths.products_root() / ".cli_status.json"
    if current.exists():
        return current
    legacy_cwd = Path.cwd() / "output" / ".cli_status.json"
    if legacy_cwd.exists():
        return legacy_cwd
    return current

_STATUS_FILE = _resolve_status_file()

# 412 断点续跑默认提示命令
_RESUME_HINT = 'video2book pipeline "<链接>" --all --sessdata YOUR_SESSDATA'


class PipelineGateError(RuntimeError):
    """流水线硬门禁终止信号（CLI 层捕获后转换为 sys.exit(exit_code)）。"""

    def __init__(self, exit_code: int, message: str = ""):
        super().__init__(message or f"pipeline gate terminated (exit {exit_code})")
        self.exit_code = exit_code


def is_412(err: Any) -> bool:
    """判断异常是否为 412 风控拦截。"""
    return "412" in str(err)


def record_412_status(err: Any) -> None:
    """记录 412/熔断状态到状态文件供 info 读取。"""
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATUS_FILE.write_text(
            json.dumps({"last_412": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "error": str(err)[:500]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def format_412(err: Any, resume_hint: Optional[str] = None) -> str:
    """412 三要素文案：定性 + 可复制动作 + 预期。"""
    hint = resume_hint or _RESUME_HINT
    return (
        f"[412 风控拦截] {err}\n"
        "①定性：B 站风控拦截（请求过频/缺登录态），非视频删除。\n"
        "②可复制动作：浏览器登录 bilibili.com → F12 → 应用/存储 → Cookie → 复制 SESSDATA，"
        f"然后运行：python src/cli.py pipeline \"<链接>\" --all --sessdata YOUR_SESSDATA\n"
        f"③预期：等待 30-60 分钟后再试；跑 python src/cli.py info 验证状态；断点续跑：{hint}"
    )


def enrich_network_error(err: Any, resume_hint: Optional[str] = None) -> str:
    """区分 412 与普通网络错误，412 走三要素文案并落盘状态。"""
    if is_412(err):
        record_412_status(err)
        return format_412(err, resume_hint)
    return f"[网络/接口异常] {err}（非 412：建议检查网络后重试；频繁失败可补 --sessdata 后重跑）"


def get_audio_stream(
    bvid: str,
    cid: Any,
    sessdata: Optional[str] = None,
    prefer_quality: str = "low",
    resume_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """音频流获取统一入口，412 富化后抛出。"""
    try:
        return AudioFetcher.get_audio_stream_info(bvid, cid, sessdata=sessdata, prefer_quality=prefer_quality)
    except Exception as err:
        raise RuntimeError(enrich_network_error(err, resume_hint)) from err


def resolve_article_type(article_type: str) -> Dict[str, str]:
    """解析长文提示词风格（供派发门禁与 manifest 记录复用）。"""
    from src.generator.prompt_templates import resolve_article_prompt

    return resolve_article_prompt(article_type)


def export_block_article_task(
    ws: TaskWorkspace,
    block: Dict[str, Any],
    transcript_file: Any = None,
    course_title: str = "",
    article_type: str = "",
    page_titles: Optional[Dict[int, str]] = None,
) -> Path:
    """导出**模块长文任务书**（一个块一篇，替代原来的「一集一篇」）。

    语料是本块的**块级逐字稿**（唯一事实来源）：写作角色读它写出一篇成体系的模块长文，
    不再切回分集、也不再逐集成篇。标题取 `block["title"]`（块内分集名的语义组合），
    产物与块音频同名同源：`articles/模块XX_<语义组合标题>_精读长文.md`。
    """
    from src.core.audio_merger import AudioMerger
    from src.core.workspace import module_article_path, module_article_stem, module_task_path
    from src.generator.prompt_templates import resolve_article_prompt

    resolved = resolve_article_prompt(article_type)
    block_id = int(block.get("block_id") or 0)
    span = str(block.get("span") or AudioMerger.block_span(block))
    block_title = str(block.get("title") or "").strip() or span
    stem = module_article_stem(block)          # 命名唯一来源：src/core/workspace.py

    task_file = module_task_path(ws.articles_dir, block)
    target_article = module_article_path(ws.articles_dir, block)
    transcript_path = Path(transcript_file).resolve() if transcript_file else None
    transcript_ready = bool(
        transcript_path and transcript_path.exists() and transcript_path.stat().st_size > 0
    )
    block_audio = Path(ws.root_dir) / str(block.get("audio") or "")
    duration_min = float(block.get("duration_min") or 0.0)
    pages = [int(p) for p in (block.get("episodes") or [])]
    titles = page_titles or {}
    episodes = "、".join(f"P{p:02d} {titles.get(p, '')}".strip() for p in pages)

    content_payload = (
        f"本块走逐字稿链路：长文必须依据下方逐字稿撰写，不得引入逐字稿之外的内容。\n\n"
        f"本块逐字稿（唯一事实来源）：`{transcript_path}`\n"
        f"块覆盖分集：{episodes}"
    )
    article_prompt = (
        resolved["prompt"]
        .replace("{title}", course_title or stem)
        .replace("{part_title}", f"{stem}（{span}）")
        .replace("{content}", content_payload)
    )
    content = (
        f"# {stem} {block_title} 模块长文任务书（MODULE_ARTICLE_TASK）\n\n"
        f"> 状态：need-agent-article | **一个块一篇模块长文**：读本块逐字稿写成一篇文章\n"
        f"> 　　　　前置条件：第 1 节那份逐字稿必须已存在且非空；缺失说明该块还没转录\n"
        f"> 长文风格：{resolved['label']}（{resolved['key']}）\n"
        f"> 执行者要求：由**子智能体**承担（一个块一个）；完成后只回报一行\n"
        f"> 　　　　　　`BLK{block_id:02d} | 文件路径 | 字节数 | 执行者`，**不回传正文**\n"
        f"> 块标题（由块内分集名语义组合而来）：{block_title}\n"
        f"> 块时长 {duration_min:.1f} 分钟 / 覆盖 {len(pages)} 集；块内范围 {span}\n"
        f"> 语料状态：逐字稿{'已就绪' if transcript_ready else '**未就绪**'}\n\n"
        f"## 1. 任务输入\n\n"
        f"- 课程全称：{course_title}\n"
        f"- 块覆盖分集：{episodes}\n"
        f"- 本块逐字稿（**唯一事实来源**）：`{transcript_path}`\n"
        f"- 所属块音频（备查，不必再听）：`{block_audio}`\n"
        f"- 目标长文落盘路径：`{target_article}`\n\n"
        f"### 前置条件（先判再写）\n\n"
        f"`{transcript_path}` 存在且非空 → 正常撰写。\n\n"
        f"不存在或为空 → **立即停止**，不要凭分集标题或常识编造内容，也不要去别处找音频补听；\n"
        f"直接回报 `BLK{block_id:02d} 逐字稿未就绪` 即可（转录完成后重新取载荷再派发）。\n\n"
        f"---\n\n"
        f"## 2. 撰写指引（模块长文）\n\n"
        f"1. **读逐字稿**：通读第 1 节那份逐字稿（含其中的代码与命令）；\n"
        f"2. **成文**：把这一整块讲的内容写成一篇文章——不是把几集拼在一起，也不是逐集机械分小节：\n"
        f"   - 逐字稿是唯一事实来源：没讲到的不补写；讲师只在幻灯片上展示、音频里没念出的代码与表格不要替他写；\n"
        f"   - 保留讲师讲法：由头、例题、比喻、临场告诫都留住；口语的啰嗦压掉，写成通顺的书面讲解；\n"
        f"   - 标题按块内知识脉络自拟（短、好检索），**不要写序号**（阅读器会自动编号）；\n"
        f"3. **落盘**：写入上方目标路径（严格保留，后续整编不得删除）。\n\n"
        f"---\n\n"
        f"## 3. 文章撰写提示词\n\n"
        f"{article_prompt}\n"
    )
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(content, encoding="utf-8")
    return task_file


# 转录时必须追加的时间戳要求。它是「块级逐字稿能机械切回分集」的前提：没有行首时间戳，
# 切分器只能降级为 unsplit，写作角色就得自己照时间表猜段落归属。
TRANSCRIBE_TIMESTAMP_INSTRUCTION = (
    "请在逐字稿正文里为每个自然段标注该段起始时间，格式为行首的 [HH:MM:SS]，例如：\n"
    "[00:12:35] 下面我们看 mov 指令的用法……\n"
    "要求：① 每个自然段都要标，不要只在开头标一次；② 时间戳必须对应音频的真实位置；"
    "③ 不要只标话题转折处。这份时间戳用于把整块音频的逐字稿切回单集，缺了就无法自动切分。"
)


def export_block_transcribe_task(
    ws: TaskWorkspace,
    block: Dict[str, Any],
    titles: Optional[Dict[str, int]] = None,
    course_title: str = "",
) -> Path:
    """导出**块级转录任务书**（`subtitles/BLK01_P08-P12_转录任务书.md`）。

    为什么是块级：块本身就是「少调用几次取音接口」的产物，一次转录覆盖块内全部集。这份
    任务书只交代三件事——转录哪个块、逐字稿落在哪、转录完如何核对完整性并回报。写作侧按块
    成文（一个块一篇模块长文），因此不再要求切回分集；按集切分降级为可选动作。

    与已被移除的「逐集转录任务书」的区别：那条按集派发，一门 200 集的课就是 200 次取音
    调用；本入口按块派发。旧入口仍由 `selfcheck` 守着不许回加。
    """
    from src.core.audio_merger import AudioMerger
    from src.core.transcript_splitter import TranscriptSplitter

    block_id = int(block.get("block_id") or 0)
    pages = [int(p) for p in (block.get("episodes") or [])]
    segments = block.get("segments") or []
    titles = titles or {}

    block_audio = Path(ws.root_dir) / str(block.get("audio") or "")
    block_transcript = TranscriptSplitter.block_path(ws, block)
    duration_min = float(block.get("duration_min") or 0.0)
    span = AudioMerger.block_span(block) if segments or pages else "?"
    block_title = str(block.get("title") or "").strip()

    task_file = Path(ws.subtitles_dir) / f"BLK{block_id:02d}_{span}_转录任务书.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)

    table_rows = []
    episode_list = []
    for seg in segments:
        page = int(seg.get("page") or 0)
        label = str(seg.get("label") or f"P{page:02d}")
        name = titles.get(page) or f"P{page:02d}"
        episode_list.append(f"{label} {name}")
        table_rows.append(
            f"| {label} | {seg.get('start') or ''} | {seg.get('end') or ''} | {seg.get('duration_sec') or 0:.0f}s |"
        )
    table = "| 集号 | 块内起始 | 块内结束 | 时长 |\n| :--- | :--- | :--- | ---: |\n" + "\n".join(table_rows)

    content = (
        f"# BLK{block_id:02d} {span} 块级转录任务书（TRANSCRIBE_TASK）\n\n"
        f"> 状态：need-agent-transcript | **只做转录这一件事**，不要写长文\n"
        f"> 执行者：由**专职转录子智能体**承担（建议 2 个角色各领一半块队列、连续消费）\n"
        f"> 完成后只回报一行 `BLK{block_id:02d} | 逐字稿路径 | 字节数 | 时间戳份数`，**不回传正文**\n"
        + (f"> 块标题（语义组合）：{block_title}\n" if block_title else "")
        + f"> 块时长 {duration_min:.1f} 分钟 / 覆盖 {len(pages)} 集；块内时间表见第 1 节\n\n"
        f"## 1. 任务输入\n\n"
        f"- 课程全称：{course_title}\n"
        f"- 块音频（本地绝对路径）：`{block_audio}`\n"
        f"- 覆盖分集：{'、'.join(episode_list)}\n"
        f"- 原始逐字稿落盘路径：`{block_transcript}`\n\n"
        f"### 块内时间表（核对覆盖与后续按集补切都用它，由合并时的 ffprobe 实测时长推出）\n\n"
        f"{table}\n\n"
        f"---\n\n"
        f"## 2. 执行指引\n\n"
        f"1. **转录整块**：调用 `omni-media-ext:read_media`：\n"
        f"   - `file_path` = 第 1 节的块音频绝对路径；\n"
        f"   - `mode` = `\"transcribe\"`；\n"
        f"   - `endpoint` = 配置里**逐字最忠实**的 ASR 端点（本机为 `gemini-proxy-asr`）；\n"
        f"   - `duration_minutes` = {max(1.0, round(duration_min, 1))}（一次读完）；\n"
        f"   - `instruction` = 第 2.1 节那段时间戳要求（**必须原样传入**）；\n"
        f"   - 返回文本里 `OMNI_STATUS` 的 `is_finished=false` 时，用 `start_time=next_start_time`"
        f"继续读下一卷，并按顺序拼接各卷正文；\n"
        f"2. **落盘原始逐字稿**：把完整转录正文写入第 1 节的「原始逐字稿落盘路径」"
        f"（`OMNI_STATUS` 注释行可丢弃，正文原样保留，不要自己摘要或改写）；\n"
        f"3. **核对完整性后回报**：确认正文覆盖到块尾（末时间戳接近块时长，或字数与块时长相称），"
        f"然后按抬头格式回报一行即可。\n"
        f"   - 长文**按块撰写**（一个块一篇模块长文），因此**不需要**把逐字稿切回分集；\n"
        f"   - 只有事后想按集查阅时，才可选执行 split-transcript（有时间戳时是机械切分；"
        f"报告 `unsplit` 说明本次逐字稿无时间戳，不影响按块写作）。\n\n"
        f"### 2.1 时间戳要求（原样传给 `instruction`）\n\n"
        f"```text\n{TRANSCRIBE_TIMESTAMP_INSTRUCTION}\n```\n\n"
        f"---\n\n"
        f"## 3. 纪律\n\n"
        f"- 本任务**只产出逐字稿**：不写长文、不动 `articles/`、不派发任何写作任务；\n"
        f"- 不得凭块内集标题推测内容：逐字稿必须来自这次取音；\n"
        f"- 只负责分到自己的那批块，做完即回报，不要顺手去改别人的块。\n"
    )
    task_file.write_text(content, encoding="utf-8")
    return task_file


def _offline_candidate_dirs(
    out_base: Path,
    bvid: str,
    custom_task: Optional[str] = None,
    season_id: Optional[Any] = None,
) -> List[Path]:
    """接口受阻时按 BV 号找回本地工作区目录（离线自愈的定位入口）。

    三级定位，越靠前越可信：

    1. `--task` 显式指定的目录；
    2. 目录名含完整 BV 号 / BV 号前缀——工作区名可能被 80 字符上限截断（如 `…_BV1P7b5z`）；
    3. `parts.json` 中任一集的 BV 号或 ``season_id`` 命中——独立 BV 合集的工作区名只带
       首集 BV，传合集内其它单集链接时必须靠拓扑缓存反查。

    只保留**确实有料**的目录（有 `parts.json`，或 `audio/` 下已有分集音频）。
    """
    def _has_content(path: Path) -> bool:
        # 「有料」= 有分集清单，或 audio/ 下已有下载好的分集音频
        return (path / "parts.json").exists() or bool(_parts_from_audio(path))

    cands: List[Path] = []
    if custom_task:
        cands.append(out_base / TaskWorkspace.sanitize_name(custom_task))
    if out_base.exists():
        # 这里遍历的是**产物根第一层**：`Path.is_dir()` 遇到 Windows「不受信任的装入点」
        # 会抛 OSError，让离线自愈整体失败。走 fsutil 的安全判定，坏条目跳过即可。
        found = [p for p in out_base.glob(f"*{bvid}*") if fsutil.is_dir(p)]
        if not found and len(bvid) > 6:
            found = [p for p in out_base.glob(f"*{bvid[:6]}*") if fsutil.is_dir(p) and _has_content(p)]
        cands.extend(found)
        if bvid or season_id:
            known = {str(p.resolve()).lower() for p in cands}
            for candidate in fsutil.iter_child_dirs(out_base):
                if str(candidate.resolve()).lower() in known or not _has_content(candidate):
                    continue
                parts_file = candidate / "parts.json"
                if not parts_file.exists():
                    continue
                try:
                    cached = json.loads(parts_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(cached, list):
                    continue
                for item in cached:
                    if not isinstance(item, dict):
                        continue
                    item_bvid = str(item.get("bvid") or "")
                    item_season = str(item.get("season_id") or "")
                    if ((bvid and item_bvid.lower() == bvid.lower())
                            or (season_id and item_season == str(season_id))):
                        cands.append(candidate)
                        break
    # 目录名里连 BV 号（或其前缀）都没有的工作区**无法**由 BV 号唯一确定：实测同一输出根下
    # 确有两个名字都不含 BV 号的 80 字符截断目录（NLP 课与另一门），任何按名字的猜法都会在
    # 它们之间摇摆。这类工作区请显式用 `--task "<工作区目录名>"` 指定——那条路是确定的。
    # 这里刻意不猜：猜错会静默读写到别的课的产物上，比自愈失败更糟。
    return cands


def _workspace_title(dir_path: Path, manifest: Optional[Dict[str, Any]] = None) -> str:
    """离线自愈时的课程标题：**目录名优先**，manifest 的 title 只作兜底。

    这里的标题会交给 `TaskWorkspace.create` 再推导一次工作区目录，所以它必须能还原出
    同一个目录——目录名是唯一满足这一点的事实。若改用 manifest 里可能被用户改短的 title，
    推导出的工作区就会指向别处（轻则空跑，重则 exit 2），而真正的成品就在旁边。
    """
    derived = dir_path.name.split("_")[0].strip()
    if derived:
        return derived
    return str((manifest or {}).get("title") or "").strip() or dir_path.name


def resolve_target_info(
    target: str,
    sessdata: Optional[str] = None,
    custom_task: Optional[str] = None,
    base_dir: Optional[Any] = None,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """多态解析本地媒体、B站、YouTube 或抖音元数据；网络失败时用本地缓存离线自愈。"""
    from src.core.ingestion import get_coordinator

    coordinator = get_coordinator()
    return coordinator.resolve_target_info(
        target,
        sessdata=sessdata,
        custom_task=custom_task,
        base_dir=base_dir,
        limit=limit,
        **kwargs,
    )


def resolve_scope_parts(info: Dict[str, Any], ws: Any) -> List[Dict[str, Any]]:
    """集号基准：**工作区 `parts.json` 优先**，缺失才用在线解析结果。

    为什么不能直接用 `info["parts"]`：在线解析永远返回课程**全集**。用户 `--range 9-87`
    建的工作区里只有 P09–P87，拿 185 集当基准会让规划任务书列错集号、让规划校验把
    用户自己的区间判成非法，甚至把整个阶段二卡死——集号是工作区的事实，工具无权放大它。

    在线解析只用于**首次建工作区**（`pipeline`），此后一律以工作区为准。
    """
    local = [
        p for p in (ws.load_parts() or [])
        if isinstance(p, dict) and p.get("page") is not None
    ]
    online = [p for p in (info.get("parts") or []) if isinstance(p, dict)]
    if not local:
        return online
    if online and {int(p["page"]) for p in online} != {int(p["page"]) for p in local}:
        print(
            f"[i] 集号基准取工作区 parts.json（{len(local)} 集，"
            f"P{min(int(p['page']) for p in local):02d}–P{max(int(p['page']) for p in local):02d}）；"
            f"在线全集为 {len(online)} 集——以工作区为准"
        )
    return sorted(local, key=lambda p: int(p["page"]))


def resolve_course_title(info: Dict[str, Any], ws: Any) -> str:
    """课程标题：manifest → parts.json 所属工作区名 → 在线解析（离线也拿得到）。

    标题会写进规划提示词与任务书抬头，取错了会让 Agent 对着别的课名做规划。
    """
    try:
        manifest_title = str((ws.load_manifest() or {}).get("title") or "").strip()
        if manifest_title:
            return manifest_title
    except Exception:
        pass
    online_title = str(info.get("title") or "").strip()
    if online_title:
        return online_title
    return ws.root_dir.name


def parse_range_string(range_str: str, max_val: int) -> List[int]:
    """解析范围字符串（'1-10' / '1,3,5' / '5-'）为 1 起始的分集页码列表。"""
    pages: Set[int] = set()
    if max_val <= 0:
        return []
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                s_str, e_str = part.split("-", 1)
                start = int(s_str.strip()) if s_str.strip() else 1
                end = int(e_str.strip()) if e_str.strip() else max_val
                if start <= max_val and end >= 1:
                    for i in range(max(1, start), min(max_val, end) + 1):
                        pages.add(i)
            else:
                p = int(part)
                if 1 <= p <= max_val:
                    pages.add(p)
        except ValueError:
            continue
    return sorted(pages)


def classify_audio_error(err: Any) -> str:
    """失败分类：412 风控 / 缺登录态 / 网络超时 / 其他。"""
    msg = str(err)
    if "412" in msg:
        return "412风控拦截"
    low = msg.lower()
    if any(k in msg for k in ("SESSDATA", "sessdata", "401", "403", "登录", "Cookie", "cookie")):
        return "缺登录态/权限"
    if any(k in low for k in ("timeout", "timed out", "connection", "network", "dns", "reset", "超时", "网络", "连接")):
        return "网络超时"
    return "其他下载异常"


_EPISODE_AUDIO_RE = re.compile(r"^P(\d+)[_.](.+)\.(?:m4a|mp3|wav|aac|flac)$", re.IGNORECASE)


def _parts_from_audio(dir_path: Path) -> List[Dict[str, Any]]:
    """从 `audio/` 的分集文件名反推集号与标题（离线自愈的最后一档）。

    为什么不再从 `articles/` 反推：长文已改为一篇对一块（`模块XX_*_精读长文.md`），
    文件名里没有集号，反推不出分集拓扑——旧实现凭 `PXX_*_精读文章.md` 猜集号，
    在新链路下只会得到空表。音频文件名（`P01_标题.m4a`）是盘上仍带集号的载体，
    且阶段一下载完就位，比长文更靠前。
    """
    audio_dir = dir_path / "audio"
    if not audio_dir.exists():
        return []
    parts: Dict[int, Dict[str, Any]] = {}
    for entry in sorted(audio_dir.glob("P*")):
        if not entry.is_file():
            continue
        matched = _EPISODE_AUDIO_RE.match(entry.name)
        if not matched:
            continue
        page = int(matched.group(1))
        parts.setdefault(page, {"page": page, "title": matched.group(2).strip()})
    return [parts[k] for k in sorted(parts)]


class PipelineCoordinator:
    """两阶段流水线调度器：音频收齐 → 转录任务派发（+可选知识块聚合）。"""

    def run(
        self,
        url: str,
        sessdata: Optional[str] = None,
        task: Optional[str] = None,
        base_dir: Optional[Any] = None,
        page: Optional[int] = None,
        range_str: Optional[str] = None,
        process_all: bool = False,
        force: bool = False,
        prefetch_workers: int = 12,
        skip_failed: bool = False,
        quality: str = "low",
        article_type: str = "",
        block_minutes: float = 0.0,
    ) -> Dict[str, Any]:
        """执行完整流水线；硬门禁失败时抛出 PipelineGateError（由 CLI 转换为退出码）。

        阶段一固定走**块级转录链路**：音频收齐后按集装箱成块、导出块级转录任务书，长文由
        写作角色读块级逐字稿撰写。块时长目标取 `block_minutes`，为 0 时用 `AudioMerger`
        的默认值（环境变量 `BVB_AUDIO_BLOCK_MINUTES`，默认 50 分钟，落 40–60 带中段）。
        """
        from concurrent.futures import ThreadPoolExecutor

        # 工作区参数必须一并传入：离线自愈按 base_dir/task 找 parts.json 缓存，
        # 漏传会退化成当前目录下的默认 output/，导致 --base-dir 指定时自愈失效。
        info = resolve_target_info(url, sessdata=sessdata, custom_task=task, base_dir=base_dir)
        bvid = info["bvid"]

        ws = TaskWorkspace.create(
            title=info["title"],
            bvid=bvid,
            custom_name=task,
            base_dir=base_dir,
            info_name=info.get("workspace_name"),
        )
        print("=" * 65)
        print(f"[*] 全流程处理流水线启动 (Task Workspace: {ws.root_dir.name})")
        print("=" * 65)

        print("[*] 阶段一策略: 音频装箱（块即模块）→ 块级转录 → 一块一篇模块长文")

        if process_all or range_str:
            all_parts = info["parts"]
            if range_str:
                target_indices = parse_range_string(range_str, len(all_parts))
                selected_parts = [all_parts[i - 1] for i in target_indices]
            else:
                selected_parts = all_parts
        elif info["has_multi_pages"]:
            req_page = page if page is not None else (info.get("url_page") or 1)
            p_idx = max(1, min(req_page, len(info["parts"])))
            selected_parts = [info["parts"][p_idx - 1]]
        else:
            selected_parts = [{
                "page": 1,
                "title": info["title"],
                "cid": info["cid"],
                "duration": info["duration"],
                "filepath": info.get("source_path", ""),
            }]

        total_episodes = len(selected_parts)
        print(f"[*] 待处理分集总数: {total_episodes}")

        # 断点续派过滤：manifest 中 status==success 的 page 跳过（force 时不过滤）
        if not force:
            try:
                _done_pages = {
                    d.get("page") for d in ws.load_manifest(absolute=True).get("details", [])
                    if d.get("status") == "success"
                }
            except Exception:
                _done_pages = set()
            if _done_pages:
                kept = []
                for p in selected_parts:
                    if p.get("page") in _done_pages:
                        print(f"[skip] P{p.get('page'):02d} {p.get('title')} 已完成，跳过")
                    else:
                        kept.append(p)
                selected_parts = kept
                total_episodes = len(selected_parts)
                print(f"[*] 断点续派后待处理: {total_episodes}")
        else:
            print("[*] --force 已指定，不过滤已完成分集")

        prefetch_workers = max(1, int(prefetch_workers or 1))
        print(f"[*] 并发配置: 音频预取 {prefetch_workers} 线程")

        def _audio_paths(p: Dict[str, Any]):
            clean_p_title = sanitize_filename(p["title"])
            return ws.audio_dir / f"P{p['page']:02d}_{clean_p_title}.m4a", clean_p_title

        def _ensure_audio_once(p: Dict[str, Any]) -> Path:
            # 单次音频收齐尝试，命中缓存直接返回
            audio_file, _ = _audio_paths(p)
            if audio_file.exists() and audio_file.stat().st_size >= 10240 and not force:
                return audio_file

            source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
            print(f"    [prefetch] P{p['page']:02d} 获取音频 ({source_type})...")

            from src.core.ingestion import get_coordinator
            coordinator = get_coordinator()
            coordinator.fetch_episode_audio(
                info,
                p,
                audio_file,
                force=force,
                sessdata=sessdata,
                quality=quality,
            )
            print(f"    [prefetch] P{p['page']:02d} 音频就绪: {audio_file.name}")
            return audio_file

        def _ensure_audio_with_retry(p: Dict[str, Any]) -> Path:
            # 单集下载失败退避重试 3 次（共 4 次尝试），退避 1/2/4 秒
            last_err: Optional[Exception] = None
            for attempt in range(4):
                try:
                    return _ensure_audio_once(p)
                except Exception as err:
                    last_err = err
                    if attempt < 3:
                        print(f"    [retry] P{p['page']:02d} 第{attempt + 1}次失败，退避重试 ({classify_audio_error(err)}): {str(err)[:120]}")
                        try:
                            time.sleep(2 ** attempt)
                        except Exception:
                            pass
            raise last_err  # type: ignore[misc]

        # ===== 阶段一「音频收齐」：并发下载/提取全部选中集音频 =====
        print("=" * 65)
        print("[*] 阶段一：音频收齐（全部选中集并发下载/提取）")
        print("=" * 65)
        # 非视频作品（抖音图文/图集 note）先在这里预筛掉：它们只有图片卡片+BGM，
        # 没有任何口播，下载与听音都得不到内容，照常派发只会诱导撰写环节编造。
        # 预筛放在并发池**之前**，因此它们不会进入重试退避路径、也不产生失败记录。
        non_video_entries: List[Dict[str, Any]] = [
            {
                "page": p.get("page"), "title": p.get("title"), "cid": p.get("cid"),
                "media_kind": part_kind(p), "skip_reason": "non_video", "status": "skipped",
            }
            for p in selected_parts if part_kind(p) != KIND_VIDEO
        ]
        if non_video_entries:
            print(f"[*] 检出 {len(non_video_entries)} 集非视频作品（图文作品，无口播）："
                  "不下载音频、不派发文章任务书")
            for _nv in non_video_entries:
                print(f"    - P{_nv['page']:02d} [{_nv['media_kind']}] {_nv['title']}")
        _prefetch_parts = [p for p in selected_parts if part_kind(p) == KIND_VIDEO]
        audio_failed: List[Dict[str, Any]] = []
        audio_ready: Dict[int, Path] = {}
        with ThreadPoolExecutor(max_workers=prefetch_workers) as prefetch_pool:
            fut_map = {p["page"]: prefetch_pool.submit(_ensure_audio_with_retry, p) for p in _prefetch_parts}
            for p in _prefetch_parts:
                try:
                    audio_ready[p["page"]] = fut_map[p["page"]].result()
                except Exception as err:
                    audio_failed.append({
                        "page": p["page"], "title": p["title"], "cid": p["cid"],
                        "error": str(err), "category": classify_audio_error(err), "status": "failed",
                    })
        # 阶段一结束后写检查点 parts.json + manifest
        # 局部运行（--page/--range）只处理选中分集：必须与既有拓扑**合并**而非覆盖，
        # 否则会把分集拓扑缓存截断成子集（离线自愈与 sync 对账都会据此误判规模）。
        try:
            _clean_parts = [{k: v for k, v in p.items() if not k.startswith("_")} for p in selected_parts]
            ws.save_parts(TaskWorkspace.merge_parts(ws.load_parts(), _clean_parts))
        except Exception:
            pass
        # 统计口径只算真正下载过的集；局部运行若全是图文集（_prefetch_parts 为空），
        # 不要用 0 覆盖上一次的音频阶段统计。
        if _prefetch_parts or not non_video_entries:
            ws.save_manifest({
                "audio_stage": {"total": len(_prefetch_parts), "ready": len(audio_ready), "failed": len(audio_failed)},
                "audio_failed_episodes": [dict(d) for d in audio_failed],
            })
        skipped_entries: List[Dict[str, Any]] = []
        if audio_failed:
            if skip_failed:
                # 显式 opt-in 豁免：记入 manifest 跳过名单，不进转录
                skipped_entries = list(audio_failed)
                audio_failed = []
                print(f"[*] --skip-failed 已指定，豁免 {len(skipped_entries)} 集（不进转录）")
                ws.save_manifest({
                    "skipped_episodes": skipped_entries,
                    "skipped_pages": [d.get("page") for d in skipped_entries],
                })
            else:
                # 任一集最终失败则严格终止报告（硬切分门禁，未进入转录）
                print("\n" + "=" * 65, file=sys.stderr)
                print("[✗] 阶段一终止：音频收齐失败（硬切分门禁，未进入转录）", file=sys.stderr)
                print(f"失败集清单（共 {len(audio_failed)} 集）：", file=sys.stderr)
                for f_ep in audio_failed:
                    print(f"    - P{f_ep['page']:02d} {f_ep['title']} [{f_ep.get('category')}]：{str(f_ep['error'])[:160]}", file=sys.stderr)
                print("失败分类统计：", file=sys.stderr)
                _cats: Dict[str, int] = {}
                for f_ep in audio_failed:
                    _cats[f_ep.get("category", "其他下载异常")] = _cats.get(f_ep.get("category", "其他下载异常"), 0) + 1
                for _c, _n in _cats.items():
                    print(f"    - {_c} × {_n}", file=sys.stderr)
                print("三选项：①删集重跑（缩小 --range 剔除失败集后重跑）②补--sessdata（浏览器复制 SESSDATA 后重跑）③人工补音频：把该集音频放到 <task>/audio/PXX_<标题>.m4a 后重跑（块会按新音频重装，该集随即进入块级转录队列）", file=sys.stderr)
                print("=" * 65, file=sys.stderr)
                raise PipelineGateError(2)

        # ===== 阶段二「模块长文任务书派发」：一块一篇，读块级逐字稿撰写，直接产出 articles/ =====
        # 排除两类不派发的分集：① 音频下载失败被 --skip-failed 豁免的；
        # ② 非视频作品（图文/图集 note，本就没有口播，见 references/non-video-works.md）。
        # 必须在这里排除，否则它们会被阶段二音频门禁当成「音频缺失」而误报，
        # 并让 _all_success 永远为 False（它们不可能产出长文）。
        _skip_pages = ({d.get("page") for d in skipped_entries}
                       | {d.get("page") for d in non_video_entries})
        effective_parts = [p for p in selected_parts if p.get("page") not in _skip_pages]
        # 阶段二入口校验音频 100% 就绪，否则拒绝并指去向
        _missing = []
        for p in effective_parts:
            _af, _ = _audio_paths(p)
            if not (_af.exists() and _af.stat().st_size >= 10240):
                _missing.append(p)
        if _missing:
            print("\n" + "=" * 65, file=sys.stderr)
            print("[✗] 阶段二拒绝启动：音频未 100% 就绪（请回阶段一排查音频目录）", file=sys.stderr)
            for _m in _missing:
                _af, _ = _audio_paths(_m)
                print(f"    - P{_m['page']:02d} {_m['title']} 缺失/过小：{_af}", file=sys.stderr)
            print(f"去向：检查 {ws.audio_dir} 与 parts.json，补齐后重跑 pipeline（断点续派自动跳过已完成集）", file=sys.stderr)
            print("=" * 65, file=sys.stderr)
            raise PipelineGateError(2)

        # ===== 阶段一点五「音频装箱合并」：把连续的几集拼成块（每块 40–60 分钟）=====
        # 块不只是「少调用几次取音接口」的容器：**块就是知识模块**。转录按块走，长文按块写
        # （一块一篇模块长文），教材按块整编，笔记按块归并——整条链路的下游都以块为粒度。
        # 因此装箱是阶段一的**固定步骤**：没有块清单就没有模块边界，写作链路无从谈起。
        blocks: List[Dict[str, Any]] = []
        block_by_page: Dict[int, Dict[str, Any]] = {}
        titles_by_page = {int(p["page"]): sanitize_filename(p["title"]) for p in effective_parts}
        from src.core.audio_merger import AudioMerger

        # 装箱宇宙 = **工作区的全部分集**（parts.json ∩ 视频集），不是本次选中的那批：
        # 块清单是模块边界的全局事实源，--range/--page 局部运行若以选中集重装箱，
        # 会把范围外的块当成孤儿清场（块音频被删、范围外任务书被作废、已完成页翻回待办）。
        _pack_pages = [
            int(p["page"]) for p in (ws.load_parts() or [])
            if isinstance(p, dict) and p.get("page") is not None and part_kind(p) == KIND_VIDEO
        ] or [int(p["page"]) for p in effective_parts]

        print("=" * 65)
        print("[*] 阶段一点五：音频装箱合并（块级转录的前置步骤）")
        print("=" * 65)
        Path(ws.subtitles_dir).mkdir(parents=True, exist_ok=True)
        try:
            merged = AudioMerger.merge(
                ws,
                _pack_pages,
                target_minutes=(block_minutes or None),
            )
        except Exception as err:
            print("\n" + "=" * 65, file=sys.stderr)
            print(f"[✗] 音频装箱合并失败，终止任务：{err}", file=sys.stderr)
            print("去向：确认 ffmpeg/ffprobe 在 PATH、audio/ 可写后重跑本命令；"
                  "缺音频的集先补齐音频再重跑（块级转录以块音频为唯一取音对象）", file=sys.stderr)
            print("=" * 65, file=sys.stderr)
            raise PipelineGateError(3) from err

        for _line in merged["diag"]:
            print(f"    {_line}")
        if merged["missing"]:
            print(f"    [!] 以下集缺音频、未进任何块：{merged['missing']}")
        blocks = merged["blocks"]
        for _line in AudioMerger.describe(blocks, merged["limits"]):
            print(f"    {_line}")
        for _block in blocks:
            for _page in _block["episodes"]:
                block_by_page[int(_page)] = _block

        ws.save_manifest({"audio_blocks": {
            "target_minutes": merged["limits"]["target"],
            "ceiling_minutes": merged["limits"]["ceiling"],
            "block_count": len(blocks),
            "episode_count": sum(len(b["episodes"]) for b in blocks),
            "noop": bool(merged["noop"]),
            "blocks_manifest": merged.get("manifest", ""),
        }})

        # 块边界随目标时长变化：旧编号的任务书留在盘上就是「可被派发的幽灵任务」
        _stale = AudioMerger.prune_orphans(ws, blocks)
        if _stale["removed_tasks"]:
            print(f"    [i] 已作废 {len(_stale['removed_tasks'])} 份与本次装箱不符的旧转录任务书")
        if _stale["orphan_audio"]:
            print(f"    [!] 以下块音频不再属于本次装箱（**未删**，确认无用后可手工清理）："
                  f"{_stale['orphan_audio']}")
        if _stale["orphan_transcripts"]:
            print(f"    [!] 以下块级逐字稿不再属于本次装箱（**未删**，分集逐字稿可能仍引用它）："
                  f"{_stale['orphan_transcripts']}")

        print(f"[*] 导出块级转录任务书（{len(blocks)} 份）→ {Path(ws.subtitles_dir).name}/")
        for _block in blocks:
            _task = export_block_transcribe_task(
                ws, _block, titles=titles_by_page, course_title=info["title"]
            )
            print(f"    [agent] BLK{_block['block_id']:02d} "
                  f"{AudioMerger.block_span(_block)}: {_task.name}")
        if merged["noop"]:
            print("[i] 本次装箱无收益（每块仅一集）：块音频直接指向该集原音频，转录仍按块派发")

        print("=" * 65)
        print("=" * 65)
        print(f"[*] 阶段二：派发**模块长文**任务书（共 {len(blocks)} 块，一个块一篇，读块逐字稿撰写）")
        print(f"[*] 长文提示词风格：{article_type or '未指定（将在派发时终止并给出风格菜单）'}")
        print("=" * 65)

        from src.core.transcript_splitter import TranscriptSplitter
        from src.core.workspace import find_module_article, module_article_path

        manifest_entries: List[Dict[str, Any]] = []
        page_titles = {
            int(p["page"]): sanitize_filename(str(p.get("title") or "")) for p in effective_parts
        }
        for idx, block in enumerate(blocks, 1):
            block_id = int(block.get("block_id") or 0)
            span = str(block.get("span") or AudioMerger.block_span(block))
            block_title = str(block.get("title") or "").strip() or span
            transcript_path = TranscriptSplitter.block_path(ws, block)
            target_article = module_article_path(ws.articles_dir, block)
            print(
                f"\n[{idx:02d}/{len(blocks):02d}] BLK{block_id:02d} {span} {block_title}"
            )

            existing_article = None
            if not force:
                # 与队列/对账/门禁同一套宽容定位（find_module_article），不再各写一套 glob
                existing_article = find_module_article(ws.articles_dir, block)
            if existing_article is not None:
                print(f"    [cached] 模块长文已存在，跳过派发: {existing_article.name}")
                manifest_entries.append({
                    "page": int((block.get("episodes") or [0])[0]),
                    "block_id": block_id, "span": span, "block_title": block_title,
                    "episodes": block.get("episodes") or [], "article": str(existing_article),
                    "asr_engine": "agent-native", "doc_engine": "agent-native", "status": "success",
                })
                continue

            # 长文风格门禁（二次防线）：CLI 层（cmd_pipeline）已在入口前确认风格并 exit 4；
            # 此处再校验一次，保证直接调用领域服务的调用方也拿不到未命中预设的提示词。
            try:
                _resolved_type = resolve_article_type(article_type)
            except ArticlePromptTypeError as err:
                print("\n" + err.report, file=sys.stderr)
                print("去向：主 Agent 先依课程标题与分集标题判定类型，再用 --article-type 重跑本命令。", file=sys.stderr)
                raise PipelineGateError(4, f"长文提示词风格门禁终止：{err.reason}") from err

            try:
                task_file = export_block_article_task(
                    ws, block, transcript_path,
                    course_title=info["title"], article_type=article_type, page_titles=page_titles,
                )
            except Exception as err:
                print("\n" + "=" * 65, file=sys.stderr)
                print(f"[✗] 模块长文任务书导出失败，终止任务：BLK{block_id:02d}：{err}", file=sys.stderr)
                print("①排障重跑：检查 articles 目录写权限与磁盘空间后重跑 pipeline", file=sys.stderr)
                print("②中止：已收齐音频与块清单保留在 audio/ 可稍后重跑", file=sys.stderr)
                print("=" * 65, file=sys.stderr)
                raise PipelineGateError(3)

            print(f"    [agent] 已导出模块长文任务书，待 Agent 读块逐字稿撰写: {task_file.name}")
            manifest_entries.append({
                "page": int((block.get("episodes") or [0])[0]),
                "block_id": block_id, "span": span, "block_title": block_title,
                "episodes": block.get("episodes") or [],
                "audio": str(Path(ws.root_dir) / str(block.get("audio") or "")),
                "task_prompt": str(task_file), "article": str(target_article),
                "transcript": str(transcript_path),
                "article_type": _resolved_type["key"],
                "asr_engine": "agent-native", "doc_engine": "agent-native",
                "status": "need-agent-article",
            })

        # ===== 阶段三「笔记归并派发」：块清单在手，归并由宿主 Agent 产出后才派发 =====
        note_plan: List[Dict[str, Any]] = []
        note_results: List[Dict[str, Any]] = []
        note_dispatched = False
        if process_all:
            # 集号基准取工作区；再过滤掉非视频作品——它们没有长文，留着会让
            # 归并的覆盖基准里出现「有集号没内容」的空洞。
            # 过滤放在**调用点**而不是 resolve_scope_parts 内部：那个函数的契约是
            # 「集号基准来源」，不是内容筛选（它有多个调用方，语义各不相同）。
            scope_parts = [p for p in resolve_scope_parts(info, ws) if part_kind(p) == KIND_VIDEO]
            # 语料就绪门禁：块即模块，模块长文（articles/模块XX_*_精读长文.md）才是笔记的语料。
            # 一篇都没有就挂起，防止透支生成空壳大笔记；这不是故障，是流水线的正常等料状态。
            from src.core.workspace import find_module_article

            ready_blocks = [
                b for b in blocks
                if find_module_article(ws.articles_dir, b) is not None
            ]
            if not ready_blocks:
                print("\n" + "=" * 65)
                print("[*] 阶段三后置：模块长文任务书已就绪，但尚无任何模块长文落盘。")
                print(f"[*] 待宿主 Agent 将长文写入 {ws.articles_dir} 后，重跑 pipeline --all 将自动归并。")
                print("=" * 65)
            else:
                print("\n" + "=" * 65)
                print(f"[*] 阶段三：笔记归并（{len(ready_blocks)}/{len(blocks)} 块已有模块长文）")
                print("=" * 65)

                # 归并 + 笔记派发的唯一实现；缺归并时用「一块一篇」兜底继续，绝不终止
                outcome = BlockSynthesizer.dispatch_notes(
                    ws,
                    scope_parts,
                    course_title=resolve_course_title(info, ws),
                )
                note_plan = outcome["notes"]
                note_results = outcome["results"]
                # planned/salvaged 才算「归并已成事」：unmerged 只是「一块一篇」的兜底粒度，
                # 把它当终态会让 pipeline_completed 在归并规划还欠着时就被点亮。
                note_dispatched = outcome["note_status"] in ("planned", "salvaged") and bool(note_plan)
                if outcome["note_status"] == "unmerged":
                    print("[*] 阶段三后置：已导出归并任务书，待宿主 Agent 产出 note_plan.json 后重跑。")
                    print(f"[*] 任务书: {ws.root_dir / 'note_plan_TASK.md'}")
        elif info["has_multi_pages"]:
            print("[*] 分区间运行：归并后置，待 --all 全量语料齐后统一派发")

        # ===== Manifest 按 page 合并落盘（工作区原生相对路径化） =====
        _existing = ws.load_manifest(absolute=True)
        _merged = {d.get("page"): d for d in _existing.get("details", []) if isinstance(d, dict)}
        for d in manifest_entries:
            _merged[d.get("page")] = d
        _failed_merged = {d.get("page"): d for d in _existing.get("failed_episodes", []) if isinstance(d, dict)}

        # 仅当所有有效分集转录成功（且无历史失败分集）时才标记全流程完毕
        # 说明：本趟的失败集在阶段一/阶段二就以 PipelineGateError 终止，不存在「本趟失败清单」，
        # 因此此处只继承 manifest 里的历史失败记录。
        _all_success = (
            len(_merged) >= len(effective_parts)
            and all(d.get("status") == "success" for d in _merged.values())
            and not _failed_merged
        )
        _existing["pipeline_completed"] = bool(_all_success and (not process_all or note_dispatched))
        _existing["processed_episodes"] = sum(1 for d in _merged.values() if d.get("status") == "success")
        _existing["details"] = [_merged[k] for k in sorted(_merged)]
        _existing["failed_episodes"] = [_failed_merged[k] for k in sorted(_failed_merged)]
        # 跳过名单 = 两类之和：① --skip-failed 豁免的音频失败集（故障）② 非视频作品（非故障）。
        # 合并进 skipped_pages 是有意的：`state_sync` 用 total - len(skipped_pages) 算有效总数，
        # 非视频集一并扣减，对账才不会把它们当成「待补的欠账」。
        # 另存 non_video_episodes 以便区分语义（旧工作区无此字段，读取方一律 .get 兜底）。
        _all_skipped = list(skipped_entries) + list(non_video_entries)
        _existing["skipped_episodes"] = _all_skipped
        _existing["skipped_pages"] = [d.get("page") for d in _all_skipped]
        _existing["non_video_episodes"] = [dict(d) for d in non_video_entries]
        if process_all and note_dispatched:
            _existing["note_plan"] = note_plan
            _existing["note_results"] = note_results
        ws.save_manifest(_existing)
        print("\n" + "=" * 65)
        print(f"[✓] 工具层流水线执行完毕！语料与任务书已归档至: {ws.root_dir}")
        print("【★ 宿主 Agent 接管指南】：")
        print(f"  1. 模块长文任务书（一个块一篇，读块级逐字稿撰写）: {ws.articles_dir}/*_TASK.md")
        if process_all:
            print(f"  2. 笔记任务书（块归并后的一篇一子智能体）: {ws.notes_dir}/*_TASK.md")
        print("  3. 请主程序以 5 个并发通道（Task 子代理或并行会话）直接领跑任务书，执行真正的认知写作！")
        print("=" * 65)

        # ===== 任务书回收：成品已落盘的模块长文/笔记任务书即时清场（每类保留 1 份范本） =====
        try:
            from .task_cleanup import cleanup_completed_tasks as _cleanup_tasks
            _reclaim = _cleanup_tasks(ws, keep_per_category=1)
            if _reclaim["deleted"]:
                print(f"[*] 已回收 {len(_reclaim['deleted'])} 份已完成任务书（每类保留 1 份范本供查阅提示词）")
        except Exception as _reclaim_err:  # 回收失败不得影响主流程
            print(f"[!] 任务书回收已跳过：{_reclaim_err}", file=sys.stderr)

        # ===== 账本对账：以磁盘产成为唯一真相回填 manifest（消除账本与产物脱节） =====
        try:
            from .state_sync import reconcile_workspace_manifest as _reconcile
            _sync = _reconcile(ws)
            print(f"[*] 账本对账：分集 {_sync['success']}/{_sync['total']} 集达标 | "
                  f"待办 {_sync['pending']} | 模块笔记 {_sync['notes']} 份 | "
                  f"教材 {_sync['textbooks']} 部 | "
                  f"pipeline_completed={_sync['pipeline_completed']}")
        except Exception as _sync_err:
            print(f"[!] 账本对账已跳过：{_sync_err}", file=sys.stderr)

        return {
            "workspace": ws,
            "manifest": _existing,
            "note_plan": note_plan,
            "note_results": note_results,
            "failed_entries": list(_failed_merged.values()),
        }
