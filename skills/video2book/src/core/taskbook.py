#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Taskbook Export Domain Service.

Renders and exports task books for:
1. Block transcription (subtitles/BLKxx_*_转录任务书.md)
2. Module long-form articles (articles/模块XX_*_TASK.md)
3. Topic planning & notebook tasks
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from src.core.workspace import TaskWorkspace, module_article_path, module_article_stem, module_task_path
from src.generator.prompt_templates import resolve_article_prompt

TRANSCRIBE_INSTRUCTION = (
    "请忠实转录音频全文为纯文本逐字稿：\n"
    "1. 忠实完整：完整转录讲师的原声讲解、口述推导与对话，严禁大意摘要、节选跳过、二次总结或提前截断；\n"
    "2. 术语准确：准确识别领域专业术语、英文标识符、指令名、API、变量与缩写；\n"
    "3. 代码公式：讲师口述推导的数学公式、代码逻辑与配置参数如实记录；\n"
    "4. 纯净正文：严禁输出「总结」、「概览」、「大纲」等任何模型元语言，严禁自作主张提炼概括；\n"
    "5. 格式规整：不需要也不得标注时间戳（严禁臆测添加 [HH:MM:SS]），按自然语意与话题分段成通顺的段落正文。"
)

TRANSCRIBE_TIMESTAMP_INSTRUCTION = TRANSCRIBE_INSTRUCTION


def resolve_article_type(article_type: str) -> Dict[str, str]:
    """解析长文提示词风格（供派发门禁与 manifest 记录复用）。"""
    return resolve_article_prompt(article_type)


def export_block_transcribe_task(
    ws: TaskWorkspace,
    block: Dict[str, Any],
    titles: Optional[Dict[str, int]] = None,
    course_title: str = "",
) -> Path:
    """导出块级转录任务书（subtitles/BLK01_P08-P12_转录任务书.md）。"""
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
        f"> 📌 **执行指引（直接执行，无需探索）**：本任务输入与输出路径均已在第 1 节完全指定。直接读取指定输入文件，完成转录并保存到目标路径；无需也不要检索、扫描项目其他文件或仓库代码。\n"
        f"> 状态：need-agent-transcript | **只做转录这一件事**，不要写长文\n"
        f"> 执行者：由**专职转录子智能体**承担（建议 2 个角色各领一半块队列、连续消费）\n"
        f"> 完成后只回报一行 `BLK{block_id:02d} | 逐字稿路径 | 字节数 | 执行者`，**不回传正文**\n"
        + (f"> 块标题（语义组合）：{block_title}\n" if block_title else "")
        + f"> 块时长 {duration_min:.1f} 分钟 / 覆盖 {len(pages)} 集；块内时间表见第 1 节\n\n"
        f"## 1. 任务输入\n\n"
        f"- 课程全称：{course_title}\n"
        f"- 块音频（本地绝对路径）：`{block_audio}`\n"
        f"- 覆盖分集：{'、'.join(episode_list)}\n"
        f"- 原始逐字稿落盘路径：`{block_transcript}`\n\n"
        f"### 块内时间表（供知识点定位核对，由合并时的 ffprobe 实测时长推出）\n\n"
        f"{table}\n\n"
        f"---\n\n"
        f"## 2. 执行指引\n\n"
        f"1. **转录整块**：优先调用 `omni-media-ext:read_media`（传入 `output_file`=`{block_transcript}` 直写落盘，零上下文开销）或 `omni-media:read_audio`：\n"
        f"   - `file_path` = 第 1 节的块音频绝对路径；\n"
        f"   - `mode` = `\"transcribe\"`；\n"
        f"   - `duration_minutes` = {max(1.0, round(duration_min, 1))}；\n"
        f"   - `instruction` = 第 2.1 节纯文本转录要求（**必须原样传入**）；\n"
        f"   - `output_file` = 第 1 节的「原始逐字稿落盘路径」（传入此参数时 MCP 会原子直写磁盘，无需在上下文中回传或手动落盘）；\n"
        f"2. **落盘原始逐字稿**：若未传入 `output_file` 或工具不支持，把完整转录正文写入第 1 节的「原始逐字稿落盘路径」；\n"
        f"3. **核对完整性后回报**：确认逐字稿已成功落盘且非空，然后按抬头格式回报单行即可。\n\n"
        f"### 2.1 纯文本转录要求（原样传给 `instruction`）\n\n"
        f"```text\n{TRANSCRIBE_INSTRUCTION}\n```\n\n"
        f"---\n\n"
        f"## 3. 纪律\n\n"
        f"- 本任务**只产出逐字稿**：不写长文、不动 `articles/`、不派发任何写作任务；\n"
        f"- 逐字稿是唯一事实来源，严禁脑补。\n"
    )
    task_file.write_text(content, encoding="utf-8")
    return task_file


def export_block_article_task(
    ws: TaskWorkspace,
    block: Dict[str, Any],
    transcript_file: Any = None,
    course_title: str = "",
    article_type: str = "",
    page_titles: Optional[Dict[int, str]] = None,
) -> Path:
    """导出模块长文任务书（一个块一篇，替代原来的「一集一篇」）。"""
    from src.core.audio_merger import AudioMerger

    resolved = resolve_article_prompt(article_type)
    block_id = int(block.get("block_id") or 0)
    span = str(block.get("span") or AudioMerger.block_span(block))
    block_title = str(block.get("title") or "").strip() or span
    stem = module_article_stem(block)

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
        f"> 📌 **执行指引（直接执行，无需探索）**：本任务输入与输出路径均已在第 1 节完全指定。直接读取指定输入文件，完成撰写并保存到目标路径；无需也不要检索、扫描项目其他文件或仓库代码。\n"
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
