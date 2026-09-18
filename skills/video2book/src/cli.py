#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-Line Interface for Video2Book (multi-platform video & knowledge extraction).

Commands:
  parse            - Parse URL/BVID, classify video type, and inspect sub-videos
  audio            - Fetch audio stream URL and download 16kHz mono m4a per episode
  pipeline         - Two-stage orchestration: gather audio, pack blocks, then dispatch task-files
  merge-audio      - (Re)pack per-episode audio into blocks and export block task files
  split-transcript - Optional: split a block transcript into per-episode transcripts
  cluster-notes    - Two-pass aggregation: blocks -> review notes (task files)
  cluster-articles - Consolidate module long-forms into modular textbooks
  dedup            - Synchronize duplicate audio assets to save LLM tokens
  login / logout   - Persist or clear the Bilibili SESSDATA credential
  info             - Show environment & toolchain readiness status
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Add parent directory to sys.path to allow running directly from anywhere
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core import fsutil
from src.core import paths as _paths
from src.core.console import enable_utf8_console
from src.core.local_media import LocalMediaParser
from src.core.fetcher import AudioFetcher
from src.core.workspace import TaskWorkspace, sanitize_filename
from src.core.credentials import (
    DouyinCookieStore,
    SessdataStore,
    douyin_store_path,
    resolve_douyin_cookie,
    resolve_sessdata,
    store_path,
)
from src.core.pipeline import (
    KIND_VIDEO,
    PipelineCoordinator,
    PipelineGateError,
    _STATUS_FILE,
    export_block_transcribe_task,
    get_audio_stream,
    parse_range_string,
    part_kind,
    resolve_course_title,
    resolve_scope_parts,
    resolve_target_info,
)
from src.generator.block_synthesizer import BlockSynthesizer

# 控制台硬化：固定 UTF-8 并逐行刷新。输出被管道捕获（宿主 Agent / CI 的常态）时，
# 若仍按 locale（中文 Windows = cp936）编码，`[✓]` 这类符号会让命令以 UnicodeEncodeError 崩掉。
enable_utf8_console()


BASE_DIR_HELP = (
    "Base output directory for task workspaces "
    "(default: the products root from src/core/paths.py — <cwd>/output when no container "
    f"marker is present, else <home>/output; override with ${_paths.ENV_OUTPUT_DIR} or ${_paths.ENV_HOME})"
)

# 四个会进入抖音抓取链路的子命令共用同一条说明：抖音对匿名访问有硬窗口，
# 不配置登录态 Cookie 时会**静默少抓**（实测某博主 216 条只放行 21 条）。
DOUYIN_COOKIE_HELP = (
    "抖音完整 Cookie 串（可选）。抖音对匿名访问施加作品列表硬窗口——实测某博主真实 216 条、"
    "匿名仅放行 21 条且第二页直接返回空，合集接口亦 403，免 cookie 无解。"
    "需要完整抓取时请传入或先执行 login --douyin-cookie；优先级高于本地存档与 $DYAUDIO_COOKIE。"
)


def _resolve_base_dir(value):
    """把 --base-dir 解析为绝对路径（空值即产物根；无容器标记时它跟随当前工作目录）。"""
    return str(_paths.resolve_base_dir(value))


def _save_manifest_rel(ws, data):
    """保存清单（TaskWorkspace 原生相对路径化，保留兼容入口）。"""
    ws.save_manifest(data)


def _to_relative_str(val):
    """绝对路径转仓库相对路径，非路径原样返回（复用 TaskWorkspace 统一实现）。"""
    if not isinstance(val, str) or not val:
        return val
    try:
        p = Path(val)
        if not p.is_absolute():
            return val
        return TaskWorkspace.to_relative(p)
    except Exception:
        return val


def _confirm_article_prompt_style(args) -> str:
    """确认使用哪种长文提示词风格（学习 / 旧版）。

    用户明确指定就照用；未指定时打印风格菜单，交互终端下请用户当场选择；
    仍然拿不到选择则终止任务（工具层不猜、不兜底）。
    """
    from src.generator.prompt_templates import render_article_prompt_menu

    chosen = (getattr(args, "article_type", "") or "").strip()
    if chosen:
        return chosen

    print("\n" + "=" * 65)
    print(render_article_prompt_menu())
    print("=" * 65)
    if sys.stdin.isatty():
        try:
            typed = input("请选择长文提示词风格 (learning / legacy，直接回车取消): ").strip()
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed:
            return typed

    print("[!] 未确认长文提示词风格，任务终止。请显式指定后重跑，例如：", file=sys.stderr)
    print('    python src/cli.py pipeline "<链接>" --all --article-type learning   # 学习（推荐）', file=sys.stderr)
    print('    python src/cli.py pipeline "<链接>" --all --article-type legacy     # 旧版（原稳定版）', file=sys.stderr)
    sys.exit(4)


def _owner_line(info) -> str:
    """渲染 UP 主一行：兼容正常元数据（dict）与离线自愈缓存（owner 为字符串/空）。

    离线自愈分支刻意不伪造 UP 主信息（owner 为空），若直接取 info['owner']['name']
    会抛 TypeError（string indices must be integers），把一条本来可用的离线路径打断。
    """
    owner = info.get("owner")
    if isinstance(owner, dict):
        name = str(owner.get("name") or "").strip() or "未知"
        mid = owner.get("mid", 0)
    else:
        name = str(owner or "").strip() or "未知（离线缓存）"
        mid = info.get("owner_mid", 0)
    return f"{name} (mid: {mid})"


def cmd_parse(args):
    info = resolve_target_info(
        args.url,
        sessdata=args.sessdata,
        custom_task=getattr(args, "task", None),
        base_dir=getattr(args, "base_dir", None),
    )
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
    mins = info.get("duration", 0) // 60
    secs = info.get("duration", 0) % 60

    print("=" * 65)
    print(f"【视频标题】: {info['title']}")
    if source_type == "local":
        print(f"【来源路径】: {info.get('source_path')}")
    else:
        author_label = "UP 主" if source_type == "bilibili" else ("频道" if source_type == "youtube" else "作者")
        print(f"【{author_label}】   : {_owner_line(info)}")
        id_label = "BV 号" if source_type == "bilibili" else "唯一标识"
        print(f"【{id_label}】   : {info.get('bvid')}")
    print(f"【来源平台】: {source_type.upper()}")
    print(f"【类型判定】: {info.get('type_desc')}")
    print(f"【总时长】  : {mins:02d}:{secs:02d}")
    if info.get("url_page"):
        print(f"【定位分P】: P{info['url_page']:02d} 《{info.get('selected_title')}》")
    print("=" * 65)

    if info.get("has_multi_pages"):
        print(f"\n▶ 分集列表 (共 {len(info['parts'])} P):")
        for p in info["parts"][:args.limit]:
            pmins = p.get("duration", 0) // 60
            psecs = p.get("duration", 0) % 60
            print(f"  P{p['page']:02d} [{pmins:02d}:{psecs:02d}] {p['title']}")
            if p.get("filepath"):
                print(f"      文件: {p['filepath']}")
            elif p.get("url"):
                print(f"      CID: {p.get('cid')} | 链接: {p['url']}")
        if len(info["parts"]) > args.limit:
            print(f"  ... 剩余 {len(info['parts']) - args.limit} 个分P已省略，可用 --limit 查看全量")

    if info.get("has_ugc_season"):
        s_info = info["season_info"]
        print(f"\n▶ 所属合集【{s_info['title']}】(共 {len(info['season_episodes'])} 个稿件):")
        for ep in info["season_episodes"][:args.limit]:
            print(f"  [{ep['section_title']}] 第{ep['episode_index']}集: {ep['title']}")
            print(f"      BV号: {ep['bvid']} | CID: {ep['cid']} | 链接: {ep['url']}")
        if len(info["season_episodes"]) > args.limit:
            print(f"  ... 剩余 {len(info['season_episodes']) - args.limit} 个稿件已省略")


def _persist_parts_cache(ws, entries) -> None:
    """把本命令见过的分集拓扑并进工作区 `parts.json`（局部运行**只补不缩**）。

    为什么要有这个：`parts.json` 是「工作区到底有哪几集」的唯一事实，`cluster-notes` /
    `cluster-articles` 都拿它当集号基准。只有 `pipeline` 写它的话，`audio` 单独跑出来的
    音频就成了「磁盘有、拓扑无」的孤儿，后续命令只能回退到在线全集取基准。
    """
    try:
        clean = [
            {k: v for k, v in e.items()
             if not k.startswith("_") and k in (
                 "page", "title", "cid", "duration", "media_kind",
                 "bvid", "aid", "season_id", "section_title", "episode_index", "url",
             )}
            for e in (entries or [])
            if isinstance(e, dict) and e.get("page") is not None
        ]
        if clean:
            ws.save_parts(TaskWorkspace.merge_parts(ws.load_parts(), clean))
    except Exception as err:
        print(f"[!] 分集拓扑缓存写入已跳过: {err}", file=sys.stderr)


def cmd_audio(args):
    info = resolve_target_info(args.url, sessdata=args.sessdata, custom_task=args.task, base_dir=args.base_dir)
    bvid = info["bvid"]

    # Initialize Task Workspace
    ws = TaskWorkspace.create(
        title=info["title"],
        bvid=bvid,
        custom_name=args.task,
        base_dir=args.base_dir,
    )
    target_audio_dir = Path(args.output).resolve() if args.output else ws.audio_dir
    target_audio_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] 任务工作区已就绪: {ws.root_dir.name}")
    print(f"    - 音频目录: {target_audio_dir}")

    # Determine multi-page batch mode
    is_batch = args.all or bool(args.range)
    if is_batch and info["has_multi_pages"]:
        all_parts = info["parts"]
        if args.range:
            target_indices = parse_range_string(args.range, len(all_parts))
            selected_parts = [all_parts[i - 1] for i in target_indices]
        else:
            selected_parts = all_parts

        total_parts = len(selected_parts)
        prefetch_workers = max(1, int(getattr(args, "prefetch_workers", 12) or 1))
        # 非视频作品（抖音图文/图集 note）没有可用音轨，取下来只有图片卡片+BGM。
        # 这里预筛掉，既省时间，也避免给后续环节留下「有音频但没人声」的假象。
        _non_video = [p for p in selected_parts if part_kind(p) != KIND_VIDEO]
        if _non_video:
            print(f"[*] 跳过 {len(_non_video)} 集非视频作品（图文作品，无口播）："
                  f"P{_non_video[0]['page']:02d} 等")
            selected_parts = [p for p in selected_parts if part_kind(p) == KIND_VIDEO]
            total_parts = len(selected_parts)

        if args.url_only:
            source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
            rows = []
            for p in selected_parts:
                if source_type == "bilibili":
                    stream_info = get_audio_stream(
                        p.get("bvid") or bvid,
                        p["cid"],
                        sessdata=args.sessdata,
                        prefer_quality=getattr(args, "quality", "low"),
                    )
                    rows.append({
                        "page": p["page"],
                        "bvid": p.get("bvid") or bvid,
                        "cid": p["cid"],
                        "title": p["title"],
                        "url": stream_info.get("best_stream_url"),
                        "quality": stream_info.get("quality_desc"),
                    })
                else:
                    rows.append({
                        "page": p["page"],
                        "title": p["title"],
                        "path": p.get("filepath") or info.get("source_path"),
                    })
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(row.get("url") or row.get("path") or "")
            return

        print("=" * 65)
        print(f"[*] 批量提取与无损转换任务启动 (共 {total_parts} 个分集，并发 {prefetch_workers} 线程)")
        print(f"[*] 目标音轨存储目录: {target_audio_dir}")
        print("=" * 65)

        def _download_worker(p):
            p_num = p["page"]
            clean_p_title = sanitize_filename(p["title"])
            target_file = target_audio_dir / f"P{p_num:02d}_{clean_p_title}.m4a"

            # Check if cached and non-empty
            if target_file.exists() and target_file.stat().st_size > 10240 and not args.force:
                size_mb = round(target_file.stat().st_size / (1024 * 1024), 2)
                print(f"[cached] P{p_num:02d} [{size_mb} MB] 已存在，跳过: {target_file.name}")
                return {
                    "page": p_num,
                    "title": p["title"],
                    "cid": p["cid"],
                    "duration": p["duration"],
                    "media_kind": part_kind(p),
                    "audio_file": str(target_file),
                    "size_bytes": target_file.stat().st_size,
                    "status": "cached",
                }

            source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
            print(f"[fetch] 正在提取 P{p_num:02d}: {p['title']} ({source_type})...")
            try:
                from src.core.ingestion import get_coordinator
                coordinator = get_coordinator()
                coordinator.fetch_episode_audio(
                    info,
                    p,
                    target_file,
                    force=args.force,
                    sessdata=args.sessdata,
                    quality=getattr(args, "quality", "low"),
                )
                saved_path = str(target_file)
                f_size = Path(saved_path).stat().st_size
                print(f"    [✓] P{p_num:02d} 音频就绪: {Path(saved_path).name} ({round(f_size / (1024 * 1024), 2)} MB)")
                return {
                    "page": p_num,
                    "title": p["title"],
                    "cid": p.get("cid"),
                    "duration": p.get("duration", 0),
                    "media_kind": part_kind(p),
                    "audio_file": saved_path,
                    "size_bytes": f_size,
                    "status": "downloaded",
                }
            except Exception as err:
                print(f"    [✗] 处理 P{p_num:02d} 发生异常: {err}", file=sys.stderr)
                return {
                    "page": p_num,
                    "title": p["title"],
                    "cid": p["cid"],
                    "error": str(err),
                    "status": "failed",
                }

        from concurrent.futures import ThreadPoolExecutor
        manifest_items = []
        with ThreadPoolExecutor(max_workers=prefetch_workers) as pool:
            futs = [pool.submit(_download_worker, p) for p in selected_parts]
            for f in futs:
                manifest_items.append(f.result())
        manifest_items.sort(key=lambda x: x["page"])

        # 分集拓扑落盘：不写的话，本命令下载的音频会变成「磁盘有、拓扑无」的孤儿，
        # 后续命令只能回退到在线全集取集号基准。
        _persist_parts_cache(ws, manifest_items)

        # 中文注释：保存时相对路径化
        _save_manifest_rel(ws, {
            "bvid": bvid,
            "title": info["title"],
            "total_selected": total_parts,
            "downloaded_count": sum(1 for m in manifest_items if m.get("status") in ("downloaded", "cached")),
            "episodes": manifest_items,
        })
        print("\n" + "=" * 65)
        print(f"[✓] 批量任务执行完成！成功同步 {sum(1 for m in manifest_items if m.get('status') in ('downloaded', 'cached'))}/{total_parts} 个分集")
        print(f"[✓] 任务元数据清单已写入: {ws.manifest_file}")
        print("=" * 65)
        return

    # Single Part Mode
    req_page = args.page if args.page is not None else (info.get("url_page") or 1)
    target_part = 1
    target_cid = info["cid"]
    target_title = info["title"]

    if info["has_multi_pages"]:
        target_part = max(1, min(req_page, len(info["parts"])))
        matched = info["parts"][target_part - 1]
        target_cid = matched["cid"]
        target_title = f"P{target_part:02d}_{matched['title']}"

    clean_title = sanitize_filename(target_title)
    target_m4a = target_audio_dir / f"{clean_title}.m4a"

    source_type = info.get("source_type") or ("local" if info.get("is_local") else "bilibili")
    matched_part = matched if info.get("has_multi_pages") else (info.get("parts") or [{}])[0]
    target_bvid = (
        matched.get("bvid")
        if info.get("has_multi_pages") and isinstance(matched, dict)
        else bvid
    ) or bvid

    if args.url_only:
        if source_type == "bilibili":
            stream_info = get_audio_stream(
                target_bvid,
                target_cid,
                sessdata=args.sessdata,
                prefer_quality=getattr(args, "quality", "low"),
            )
            if args.json:
                print(json.dumps({
                    "bvid": target_bvid,
                    "cid": target_cid,
                    "title": target_title,
                    "url": stream_info.get("best_stream_url"),
                    "quality": stream_info.get("quality_desc"),
                }, ensure_ascii=False, indent=2))
            else:
                print(stream_info.get("best_stream_url") or "")
        else:
            source_path = matched_part.get("filepath") or info.get("source_path") or ""
            if args.json:
                print(json.dumps({
                    "title": target_title,
                    "path": source_path,
                }, ensure_ascii=False, indent=2))
            else:
                print(source_path)
        return

    print(f"[*] 正在提取单集音频 ({source_type})...")
    from src.core.ingestion import get_coordinator
    coordinator = get_coordinator()
    coordinator.fetch_episode_audio(
        info,
        matched_part,
        target_m4a,
        force=args.force,
        sessdata=args.sessdata,
        quality=getattr(args, "quality", "low"),
    )
    saved_path = str(target_m4a)
    print(f"[✓] 音频下载/提取完成: {saved_path}")

    # 单集模式同样落分集拓扑（与既有拓扑合并，只补不缩）
    _persist_parts_cache(
        ws,
        [info["parts"][target_part - 1]] if info.get("has_multi_pages") else (info.get("parts") or [])[:1],
    )

    if args.json:
        result = {
            "bvid": target_bvid,
            "cid": target_cid,
            "title": target_title,
            "quality": stream_info["quality_desc"],
            "audio_file": saved_path,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_pipeline(args):
    """两阶段流水线：调度编排委托领域服务 PipelineCoordinator，CLI 仅负责参数解析与退出码转换。"""
    article_type = _confirm_article_prompt_style(args)
    coordinator = PipelineCoordinator()
    try:
        coordinator.run(
            url=args.url,
            sessdata=args.sessdata,
            task=args.task,
            base_dir=args.base_dir,
            page=args.page,
            range_str=args.range,
            process_all=args.all,
            force=args.force,
            prefetch_workers=args.prefetch_workers,
            skip_failed=args.skip_failed,
            quality=args.quality,
            article_type=article_type,
            block_minutes=getattr(args, "block_minutes", None) or 0.0,
        )
    except PipelineGateError as gate:
        sys.exit(gate.exit_code)


def _workspace_from_path(raw: str) -> TaskWorkspace:
    """按目录路径绑定一个**已存在**的工作区（`merge-audio` / `split-transcript` 共用）。

    为什么用路径而不是 URL：这两条命令都是**离线重跑**——块清单与音频已经在盘上，再走一次
    网络解析既慢又可能撞风控。给绝对路径最直接，也让命令与当前工作目录彻底解耦。
    """
    path = Path(str(raw)).expanduser()
    if not path.is_dir():
        print(f"[✗] 工作区目录不存在：{path}", file=sys.stderr)
        print("去向：传入 `<产物根>/<课程工作区>` 的绝对路径（目录里应有 parts.json 与 audio/）", file=sys.stderr)
        sys.exit(2)
    return TaskWorkspace.from_existing(path)


def _titles_by_page(ws: TaskWorkspace):
    """从 parts.json 取「集号 → 清洗后标题」，与音频/长文/逐字稿的命名口径保持一致。"""
    titles = {}
    for part in ws.load_parts() or []:
        page = part.get("page")
        if page is None:
            continue
        titles[int(page)] = sanitize_filename(str(part.get("title") or f"P{int(page):02d}"))
    return titles


def cmd_merge_audio(args):
    """单独重跑音频装箱合并并导出块级转录任务书（幂等，可反复执行）。"""
    from src.core.audio_merger import AudioMerger

    ws = _workspace_from_path(args.workspace)
    titles = _titles_by_page(ws)
    pages = [
        int(part["page"]) for part in (ws.load_parts() or [])
        if part.get("page") is not None and part_kind(part) == KIND_VIDEO
    ]
    if not pages:
        print(f"[✗] {ws.root_dir.name} 里没有可用分集（parts.json 为空或全为非视频作品）", file=sys.stderr)
        sys.exit(2)

    print("=" * 65)
    print(f"[*] 音频装箱合并：{ws.root_dir.name}")
    print("=" * 65)
    result = AudioMerger.merge(
        ws, pages, target_minutes=(args.block_minutes or None), force=bool(args.force)
    )
    for line in result["diag"]:
        print(f"    {line}")
    for line in AudioMerger.describe(result["blocks"], result["limits"]):
        print(f"    {line}")
    if not result["blocks"]:
        print("[✗] 没有生成任何块（音频缺失？先跑 pipeline 收音频）", file=sys.stderr)
        sys.exit(2)

    stale = AudioMerger.prune_orphans(ws, result["blocks"])
    if stale["removed_tasks"]:
        print(f"    [i] 已作废 {len(stale['removed_tasks'])} 份与本次装箱不符的旧转录任务书")
    if stale["orphan_audio"]:
        print(f"    [!] 以下块音频不再属于本次装箱（**未删**，确认无用后可手工清理）：{stale['orphan_audio']}")
    if stale["orphan_transcripts"]:
        print(f"    [!] 以下块级逐字稿不再属于本次装箱（**未删**，分集逐字稿可能仍引用它）："
              f"{stale['orphan_transcripts']}")

    course_title = ""
    try:
        course_title = str((ws.load_manifest() or {}).get("course_title") or "")
    except Exception:
        course_title = ""
    course_title = course_title or ws.root_dir.name
    for block in result["blocks"]:
        export_block_transcribe_task(ws, block, titles=titles, course_title=course_title)

    print(f"[✓] 块清单：{AudioMerger.manifest_path(ws)}")
    print(f"[✓] 块级转录任务书 {len(result['blocks'])} 份 → {Path(ws.subtitles_dir).name}/")
    print("[i] 下一步：转录角色照任务书用 read_media 出块级逐字稿；写作角色读它写模块长文（一个块一篇）")


def cmd_split_transcript(args):
    """把块级逐字稿按块时间表切成 `subtitles/PXX_*_逐字稿.md`（机械切分，幂等）。"""
    from src.core.audio_merger import AudioMerger
    from src.core.transcript_splitter import TranscriptSplitter

    ws = _workspace_from_path(args.workspace)
    manifest = AudioMerger.load_manifest(ws)
    if not manifest or not manifest.get("blocks"):
        print(f"[✗] 找不到块清单：{AudioMerger.manifest_path(ws)}", file=sys.stderr)
        print("去向：先跑 pipeline（或 merge-audio）生成块与转录任务书", file=sys.stderr)
        sys.exit(2)

    titles = _titles_by_page(ws)
    blocks = list(manifest["blocks"])
    if args.block is not None:
        blocks = [b for b in blocks if int(b.get("block_id") or 0) == int(args.block)]
        if not blocks:
            print(f"[✗] 块清单里没有 BLK{int(args.block):02d}", file=sys.stderr)
            sys.exit(2)

    print("=" * 65)
    print(f"[*] 切分块级逐字稿 → 分集逐字稿（{len(blocks)} 个块）")
    print("=" * 65)
    done = pending = unsplit = suspect = 0
    for block in blocks:
        label = f"BLK{int(block.get('block_id') or 0):02d} {AudioMerger.block_span(block)}"
        raw = TranscriptSplitter.block_path(ws, block)
        if not raw.exists() or raw.stat().st_size == 0:
            print(f"[skip] {label} 尚无块级逐字稿（{raw.name}）")
            pending += 1
            continue
        outcome = TranscriptSplitter.write_episode_transcripts(
            ws, block, raw.read_text(encoding="utf-8"), titles=titles,
        )
        print(f"[*] {label}: {outcome['status']}")
        for line in outcome["diag"]:
            print(f"    {line}")
        if outcome["status"] == "suspect":
            suspect += 1
        elif outcome["mode"] == "timestamp":
            done += 1
        else:
            unsplit += 1

    print(
        f"[✓] 切分完成：{done} 块成功 / {suspect} 块边界可疑 / "
        f"{unsplit} 块未切分 / {pending} 块待转录"
    )
    if pending:
        print(f"    [!] 待转录的块见 {Path(ws.subtitles_dir).name}/BLK*_转录任务书.md（转录完重跑本命令）")
    if unsplit:
        print("    [!] 未切分的块：模型没给行首时间戳，按转录任务书 2.1 节重读该块后重跑本命令")
    if suspect:
        print(
            "    [!] 边界可疑的块已整体标记 suspect，不会进入写作派发；"
            "按转录任务书 2.1 节补足逐段时间戳后重跑本命令"
        )
        return 3


def cmd_cluster_notes(args):
    info = resolve_target_info(
        args.url,
        sessdata=args.sessdata,
        custom_task=getattr(args, "task", None),
        base_dir=getattr(args, "base_dir", None),
    )
    bvid = info["bvid"]
    ws = TaskWorkspace.create(title=info["title"], bvid=bvid, custom_name=args.task, base_dir=args.base_dir, info_name=info.get("workspace_name"))

    # 集号基准以工作区为准（在线解析只用于首次建工作区），标题同理（离线也拿得到）
    # 再滤掉非视频作品：它们没有长文，留着会让归并出现「有集号没内容」的空洞。
    parts = [p for p in resolve_scope_parts(info, ws) if part_kind(p) == KIND_VIDEO]
    course_title = resolve_course_title(info, ws)

    print("=" * 65)
    print(f"[*] 启动笔记流水线（块清单 → 语义归并 → 笔记任务书）")
    print(f"[*] 课程标题: 《{course_title}》 (共 {len(parts)} 个分集)")
    print(f"[*] 任务工作区: {ws.root_dir}")
    print("=" * 65)

    # 过滤：--block-id / --start-block / --end-block 按**笔记序号**筛选（参数名保留兼容）
    _filter_active = bool(args.block_id or args.start_block or args.end_block)

    def _selected(note):
        note_id = int(note.get("note_id") or 0)
        if args.block_id:
            return note_id == args.block_id
        if args.start_block or args.end_block:
            s_n = args.start_block or 1
            e_n = args.end_block or 10**9
            return s_n <= note_id <= e_n
        return True

    outcome = BlockSynthesizer.dispatch_notes(
        ws,
        parts,
        course_title=course_title,
        force=args.force,
        # 全量派发时才传 None：分批派发下「本轮没派到的任务书」仍是有效待办，
        # 传个恒真函数会让下游误以为本轮就是全部，从而把待办当废纸清掉。
        select=_selected if _filter_active else None,
    )

    # 加载转绝对、保存转相对（TaskWorkspace 原生支持）
    manifest = ws.load_manifest(absolute=True)
    if outcome["note_status"] != "no-blocks":
        manifest["note_plan"] = outcome["notes"]
        manifest["note_results"] = outcome["results"]
    ws.save_manifest(manifest)

    print("\n" + "=" * 65)
    if outcome["note_status"] == "no-blocks":
        print(f"[✓] 本命令已正常结束（未派发笔记）：块清单缺失，界面已给出装箱命令。")
    else:
        _gen = sum(1 for r in outcome["results"] if r.get("status") == "generated")
        _cached = len(outcome["results"]) - _gen
        print(f"[✓] 笔记流水线执行完毕！{len(outcome['blocks'])} 个块 → {len(outcome['notes'])} 篇笔记")
        print(f"[✓] 已导出 {_gen} 份笔记任务书（另有 {_cached} 篇成品已存在，跳过派发）")
    print(f"[✓] 任务书目录: {ws.notes_dir}")
    print("=" * 65)


def cmd_cluster_articles(args):
    """把各块的**模块长文**按块序整编成册（textbooks/）：册=书、章=块。"""
    from src.core.audio_merger import AudioMerger
    from src.generator.integrator import ArticleIntegrator

    info = resolve_target_info(
        args.url,
        sessdata=args.sessdata,
        custom_task=getattr(args, "task", None),
        base_dir=getattr(args, "base_dir", None),
    )
    bvid = info["bvid"]
    ws = TaskWorkspace.create(title=info["title"], bvid=bvid, custom_name=args.task, base_dir=args.base_dir, info_name=info.get("workspace_name"))
    course_title = resolve_course_title(info, ws)

    print("=" * 65)
    print(f"[*] 启动教材整编流水线：按块序把各块模块长文整编成册 (Modular Textbook Integration)")
    print(f"[*] 课程标题: 《{course_title}》")
    print(f"[*] 任务工作区: {ws.root_dir}")
    print(f"[*] 目标教材目录: {ws.root_dir / 'textbooks'}")
    print("=" * 65)

    # 模块边界 = 块边界（`audio/_blocks/blocks.json`）：音频按 40–60 分钟装箱，一块一篇模块长文。
    # 没有块清单就没有模块可整编——提示先装箱，而不是退回按标题前缀硬分组（那正是被废除的老路）。
    blocks = (AudioMerger.load_manifest(ws) or {}).get("blocks") or []
    if not blocks:
        print(f"[!] 本工作区没有块清单，无法整编教材：模块边界来自音频装箱。")
        print(f"[*] 请先跑：python src/cli.py merge-audio \"{Path(ws.root_dir).as_posix()}\"")
        print("=" * 65)
        sys.exit(2)

    integrator = ArticleIntegrator(ws.root_dir)
    force = bool(getattr(args, "force", False))
    results = integrator.run(course_title=course_title, force=force, blocks=blocks)

    # Update manifest（textbooks 属列表型路径字段，save_manifest 会自动反向相对化）
    manifest = ws.load_manifest(absolute=True)
    manifest["textbooks"] = [str(r) for r in results]
    ws.save_manifest(manifest)

    print("\n" + "=" * 65)
    print(f"[✓] 教材已整编，共 {len(results)} 册（册=书、章=块；册名取自内容）"
          f"（{'已按最新章节强制重编' if force else '已有教材默认复用，需重编请加 --force'}）:")
    for r in results:
        size_kb = round(r.stat().st_size / 1024, 1)
        print(f"    - [{size_kb} KB] {r.name}")
    print(f"[i] 分册依据：{ws.root_dir / 'textbook_plan.json'}（Agent 按内容划分册并起名）；"
          f"缺该规划时按平台分节/章节标记兜底，任务书见 {ws.root_dir / 'textbook_plan_TASK.md'}")
    print(f"[✓] 模块长文保持完整: {ws.articles_dir} (未做任何删除)")
    print("=" * 65)


def cmd_dedup(args):
    """Scans duplicate audio and reuses the per-episode transcripts already cut from them."""
    info = resolve_target_info(
        args.url,
        sessdata=args.sessdata,
        custom_task=getattr(args, "task", None),
        base_dir=getattr(args, "base_dir", None),
    )
    bvid = info["bvid"]
    ws = TaskWorkspace.create(title=info["title"], bvid=bvid, custom_name=args.task, base_dir=args.base_dir, info_name=info.get("workspace_name"))

    print("=" * 65)
    print(f"[*] 启动音频 SHA-256 指纹去重扫描流水线 (Audio Fingerprint Deduplication)")
    print(f"[*] 任务工作区: {ws.root_dir}")
    print("=" * 65)

    synced = ws.sync_duplicate_assets(dry_run=args.dry_run)
    if synced:
        print(f"\n[✓] 发现并同步了 {len(synced)} 组重复音频资产 (0 Token 消耗):")
        for item in synced:
            print(f"    - P{item['src_page']:02d} ──► P{item['dst_page']:02d} [Hash: {item['hash']}] (分集逐字稿: {item['synced_transcript']})")
    else:
        print("\n[✓] 未发现需要同步的重复分集（所有音频独一无二或已全部同步就绪）。")
    print("=" * 65)


def cmd_cleanup(args):
    """回收已完成的派发任务书（*_TASK.md），每类保留 N 份范本供查阅提示词。"""
    from src.core.task_cleanup import CATEGORY_LABELS, cleanup_completed_tasks, find_workspaces

    workspaces = find_workspaces(args.base_dir)
    if getattr(args, "task", None):
        keyword = str(args.task)
        workspaces = [w for w in workspaces if keyword in w.root_dir.name]
    if not workspaces:
        print(f"[!] 在 {Path(args.base_dir).resolve()} 下未找到可用工作区。", file=sys.stderr)
        sys.exit(1)

    keep_n = max(0, int(getattr(args, "keep", 1) or 0))
    dry_run = bool(getattr(args, "dry_run", False))

    print("=" * 65)
    print(f"[*] 任务书回收流水线（{'预演，不落盘' if dry_run else '执行删除'}；每类保留 {keep_n} 份范本）")
    print(f"[*] 扫描基目录: {Path(args.base_dir).resolve()}")
    print("=" * 65)

    total_deleted = total_kept = total_skipped = total_failed_delete = 0
    for ws in workspaces:
        result = cleanup_completed_tasks(ws, keep_per_category=keep_n, dry_run=dry_run)
        counts = result["counts"]
        print(f"\n▶ {ws.root_dir.name}")
        for category, stat in counts.items():
            print(
                f"    {CATEGORY_LABELS[category]:<12} 共 {stat['total']:>4} 份 | "
                f"回收 {stat['deleted']:>4} | 保留范本 {stat['kept']} | "
                f"成品未产出仍保留 {stat['skipped_pending']} | 删除失败 {stat.get('failed_delete', 0)}"
            )
        total_deleted += len(result["deleted"])
        total_kept += len(result["kept"])
        total_skipped += len(result["skipped_pending"])
        total_failed_delete += len(result.get("failed_delete", []))
        for path in result["kept"]:
            print(f"    [留] {path}")
        for path in result.get("failed_delete", []):
            print(f"    [!] 删除失败（文件被占用或无权限）: {path}")

    print("\n" + "=" * 65)
    verb = "可回收" if dry_run else "已回收"
    print(f"[✓] {verb}任务书 {total_deleted} 份 | 保留范本 {total_kept} 份 | "
          f"成品未产出仍保留 {total_skipped} 份 | 删除失败 {total_failed_delete} 份")
    if dry_run:
        print("[i] 当前为预演模式；去掉 --dry-run 即真正删除。")
    print("=" * 65)


def cmd_sync(args):
    """按磁盘产成对账并回填 manifest.json（账本以硬盘为唯一真相）。"""
    from src.core.state_sync import reconcile_workspace_manifest
    from src.core.task_cleanup import find_workspaces

    workspaces = find_workspaces(args.base_dir)
    if getattr(args, "task", None):
        keyword = str(args.task)
        workspaces = [w for w in workspaces if keyword in w.root_dir.name]
    if not workspaces:
        print(f"[!] 在 {Path(args.base_dir).resolve()} 下未找到可用工作区。", file=sys.stderr)
        sys.exit(1)

    dry_run = bool(getattr(args, "dry_run", False))
    print("=" * 65)
    print(f"[*] 任务账本对账（{'预演，不写盘' if dry_run else '写入 manifest.json'}）")
    print("[*] 判定依据：块清单 + articles/ 合格模块长文（≥1000 字节）+ notes/ + textbooks/")
    print("=" * 65)

    for ws in workspaces:
        report = reconcile_workspace_manifest(ws, dry_run=dry_run)
        print(f"\n▶ {report['workspace']}")
        if report["stage1_unit"] == "module":
            print(f"    阶段一（按块）：{report['blocks_done']}/{report['blocks_total']} 块已有模块长文")
            print(f"    分集覆盖：{report['success']}/{report['total']} 集被模块长文覆盖 | "
                  f"待办 {report['pending']} | 跳过 {report['skipped']} | 历史失败 {report['failed']}")
        else:
            print("    老格式工作区（无块清单）：新链路按块对账，"
                  "请先 `cli.py merge-audio <工作区>` 重装块后再对账")
        print(f"    模块资产：模块笔记 {report['notes']} 份 | 教材 {report['textbooks']} 部")
        print(f"    pipeline_completed = {report['pipeline_completed']}")

    print("\n" + "=" * 65)
    print(f"[✓] 已对账 {len(workspaces)} 个工作区" + ("（预演模式，未写盘）" if dry_run else ""))
    print("=" * 65)


def cmd_login(args):
    """持久化保存登录凭证（B 站 SESSDATA / 抖音 Cookie），之后所有命令无需再传。

    刻意不提供交互式输入：本工具主要供 Agent 自动化调度，等待人工键入的分支
    在非交互环境下会直接卡死。
    """
    sessdata = (getattr(args, "sessdata", None) or "").strip()
    douyin_cookie = (getattr(args, "douyin_cookie", None) or "").strip()

    if not sessdata and not douyin_cookie:
        print("[!] 未提供任何凭证，拒绝写入空值。", file=sys.stderr)
        print("    B 站：python src/cli.py login --sessdata \"<你的 SESSDATA>\"", file=sys.stderr)
        print("          浏览器登录 bilibili.com → F12 → 应用/存储 → Cookie → 复制 SESSDATA 的值", file=sys.stderr)
        print("    抖音：python src/cli.py login --douyin-cookie \"<你的 Cookie 串>\"", file=sys.stderr)
        print("          浏览器登录 douyin.com → F12 → 应用/存储 → Cookie → 复制整串", file=sys.stderr)
        print("          （抖音必须登录态才能翻页取全量作品；匿名只放行约 20 条）", file=sys.stderr)
        sys.exit(1)

    if sessdata:
        path = SessdataStore.save(sessdata)
        print(f"[✓] SESSDATA 已持久化保存: {path}")
        print(f"    指纹: {SessdataStore.mask(sessdata)}")

    if douyin_cookie:
        path = DouyinCookieStore.save(douyin_cookie)
        print(f"[✓] 抖音 Cookie 已持久化保存: {path}")
        print(f"    指纹: {DouyinCookieStore.mask(douyin_cookie)}")

    print("[i] 该文件已被 .gitignore 排除，不会进入版本库。")
    print("[i] 撤销保存请运行：python src/cli.py logout")


def cmd_logout(args):
    """清除本地保存的全部凭证（SESSDATA 与抖音 Cookie）。"""
    清除 = []
    if SessdataStore.clear():
        清除.append("SESSDATA")
    if DouyinCookieStore.clear():
        清除.append("抖音 Cookie")
    if 清除:
        print(f"[✓] 已清除本地保存的：{'、'.join(清除)}。")
    else:
        print("[*] 本地没有保存过任何凭证，无需清除。")


def cmd_info(args):
    """中文注释：做实 info：WBI 有效期/sessdata 有无/412 状态/断点续跑示例。"""
    import time as _time
    if getattr(args, "refresh", False):
        try:
            if _STATUS_FILE.exists():
                _STATUS_FILE.unlink()
                print("[*] 已清理缓存状态文件")
        except Exception:
            pass
    print("=" * 65)
    print("【系统运行环境与工具链检查】")
    _py_ok = sys.version_info >= (3, 10)
    print(f"• Python 运行环境: v{sys.version.split()[0]} ({sys.executable})"
          + ("" if _py_ok else "  ✗ 低于最低要求 3.10，请先升级解释器"))
    if getattr(Path, "is_junction", None) is not None:
        print("• 链接去重能力 : 完整（Python 3.12+：junction 与符号链接都能识别）")
    else:
        print("• 链接去重能力 : 降级（< 3.12 无 Path.is_junction，改用文件属性位识别重解析点；"
              "junction 与符号链接同样会被跳过，不会重复计数）")

    # ffmpeg 是**硬前置**（取音频/切片），ffprobe 可选（缺失时改用 ffmpeg -i 解析时长）。
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path:
        print(f"• FFmpeg 状态   : 已就绪 ({ffmpeg_path})")
    else:
        print("• FFmpeg 状态   : ✗ 未找到——这是取音频/切片的硬前置，")
        print("                  `pipeline` / `audio` 会在音频阶段失败。安装并加入 PATH：")
        print("                    Windows : winget install Gyan.FFmpeg")
        print("                    macOS   : brew install ffmpeg")
        print("                    Linux   : sudo apt update && sudo apt install -y ffmpeg")
    print("• FFprobe 状态  : " + (
        f"已就绪 ({ffprobe_path})" if ffprobe_path
        else "未找到（可选：缺失时改用 `ffmpeg -i` 解析时长，精度略低、速度略慢）"
    ))
    print("• 架构模式      : 宿主 Agent 原生派发模式（多平台统一媒体内核，转录=对话模型原生唯一路径）")
    print("=" * 65)
    print("【多平台媒体采集引擎状态】")
    from src.core.ingestion import get_coordinator
    readiness = get_coordinator().readiness_report()
    for name, (ok, msg) in readiness.items():
        icon = "[✓]" if ok else "[✗]"
        print(f"• {name:<18}: {icon} {msg}")
    print("=" * 65)
    print("【阶段一听音通道（按宿主自己的工具列表选择，不要猜）】")
    # 两个 MCP 的位置由 paths.py 统一解析（新布局 <容器根>/omni-media/{mcp,mcp-ext}，
    # 兼容迁移前的旧布局与 $OMNI_MEDIA_MCP_DIR 覆盖）
    _mcp_dir = _paths.mcp_repo()
    _mcp_ext_dir = _paths.mcp_ext_repo()
    # 覆盖标记只挂在**真正受该变量影响的路径**上（下方「MCP 仓库」行是固定布局，不受它影响）
    _mcp_src = "  [来自 ${}]".format(_paths.ENV_MCP_DIR) \
        if os.environ.get(_paths.ENV_MCP_DIR, "").strip() else ""
    print("• read_audio（宿主原生听音）: " + (
        f"已就位 {_mcp_dir}{_mcp_src}" if fsutil.is_dir(_mcp_dir)
        else f"未发现 {_mcp_dir}（纯文本宿主请改用 read_media）{_mcp_src}"))
    print("• read_media（外部模型代读）: " + (
        f"已就位 {_mcp_ext_dir}" if fsutil.is_dir(_mcp_ext_dir)
        else f"未发现 {_mcp_ext_dir}（需 config.json 里的外部模型端点与 api_key）"))
    print("  两者都不可用时：阶段一必须停下并提示先挂载其一，不得跳过音频保真直接编造正文。")
    print("=" * 65)
    print("【三域路径（代码 / MCP / 产物 互相隔离）】")
    _三域 = _paths.describe()
    _products = Path(_三域["products_root"])
    _products_state = "已存在" if fsutil.is_dir(_products) else "尚不存在（首次运行会自动创建）"
    print(f"• 代码根       : {_三域['code_root']}")
    _有标记 = _三域["container_pinned"]
    _生效 = _有标记 and _三域["cwd_inside_container"]
    _容器说明 = (
        "有，且容器布局生效" if _生效
        else ("有，但当前工作目录不在容器内 → 按工作目录解析产物" if _有标记
              else "无（容器根可选，不影响可用性）")
    )
    print(f"• 容器根 home  : {_三域['home_root']}  [容器标记: {_容器说明}]"
          + ("  [来自 ${}]".format(_paths.ENV_HOME) if _三域["home_from_env"] else ""))
    _产物来源 = (
        "${}".format(_paths.ENV_OUTPUT_DIR) if _三域["products_from_env"]
        else ("容器根下的 output/" if _生效 else "当前工作目录下的 output/（默认）")
    )
    print(f"• 产物根       : {_products}  [{_products_state}]  [来自 {_产物来源}]")
    # MCP 仓库位置：显示**实际探查到**的那个，而不是拿 home_root() 拼一个可能不存在的路径。
    # 找不到时退回预期位置并明确标注「未找到」，避免把提示值伪装成事实。
    _mcp_base = (
        _mcp_dir.parent if fsutil.is_dir(_mcp_dir)
        else (_mcp_ext_dir.parent if fsutil.is_dir(_mcp_ext_dir) else None)
    )
    _mcp_expected = _paths.home_root() / _paths.DEFAULT_MCP_REPO_DIRNAME
    print(f"• MCP 仓库     : {_mcp_base or _mcp_expected}"
          + ("" if _mcp_base else "  [未找到，此为预期位置]")
          + "  （仅为位置提示；听音通道按你工具列表里的 read_audio / read_media 判定）")
    print(f"  工作区清单   : {store_path().parent}")
    print(f"  覆盖方式     : export {_paths.ENV_OUTPUT_DIR}=<产物根> / export {_paths.ENV_HOME}=<容器根>，或用 --base-dir")
    print("=" * 65)
    # 中文注释：WBI key 有效期读内存缓存+缓存文件
    print("【WBI Key 状态】")
    try:
        from src.core.wbi import WbiSigner
        exp = getattr(WbiSigner, "_cache_expire_time", 0.0)
        has_key = bool(getattr(WbiSigner, "_cached_mixin_key", None))
        if has_key and exp > _time.time():
            print(f"• WBI Key：有效，有效期至 {_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(exp))}")
        elif has_key:
            print("• WBI Key：已过期，下次请求自动刷新")
        else:
            print("• WBI Key：无记录（尚未请求，首次调用自动获取）")
    except Exception as err:
        print(f"• WBI Key：无记录（{err}）")
    # 中文注释：凭证只显示来源与脱敏指纹，绝不回显完整值
    sess = getattr(args, "sessdata", None)
    src = getattr(args, "sessdata_source", None)
    if sess:
        print(f"• SESSDATA：有（来源：{src}，指纹：{SessdataStore.mask(sess)}）")
    else:
        print("• SESSDATA：无（未传入 --sessdata，本地也无存档）")
    if SessdataStore.load():
        print(f"• 凭证存档：已保存于 {store_path()}")
    else:
        print('• 凭证存档：无（可用 python src/cli.py login --sessdata "<值>" 持久化保存）')

    # 抖音 Cookie：匿名只放行约 20 条作品（实测 216 条只取到 21 条），必须显式标出后果
    dy = getattr(args, "douyin_cookie", None)
    dy_src = getattr(args, "douyin_cookie_source", None)
    if dy:
        print(f"• 抖音 Cookie：有（来源：{dy_src}，指纹：{DouyinCookieStore.mask(dy)}）")
    else:
        print("• 抖音 Cookie：无 —— 抖音匿名访问只放行约 20 条作品（实测博主 216 条仅取到 21 条），")
        print("               合集接口亦返回 403，免 cookie 无解。需完整抓取请先执行：")
        print('               python src/cli.py login --douyin-cookie "<Cookie 串>"')
    if DouyinCookieStore.load():
        print(f"• 抖音凭证存档：已保存于 {douyin_store_path()}")
    else:
        print('• 抖音凭证存档：无（可用 python src/cli.py login --douyin-cookie "<值>" 持久化保存）')
    # 中文注释：上次 412/熔断状态
    print("【上次 412/熔断状态】")
    try:
        if _STATUS_FILE.exists():
            print(f"• 状态文件：{_STATUS_FILE}（仓库相对：{_to_relative_str(str(_STATUS_FILE))}）")
            print(f"• 内容：{_STATUS_FILE.read_text(encoding='utf-8')[:500]}")
        else:
            print("• 无记录")
    except Exception as err:
        print(f"• 无记录（读取失败：{err}）")
    print("=" * 65)
    print("【Agent 自主驱动工作协议】：")
    print("0. 类型判定（工作流第一步）: 依课程标题/分集标题判定长文类型，未命中已提供提示词的类型即终止任务")
    print("1. 物理层跑批: python src/cli.py pipeline \"<链接>\" --all --article-type learning")
    print("   - 长文提示词风格由用户确认：learning=学习（推荐）/ legacy=旧版；不确认即终止任务")
    print("2. 语料与任务书自动生成于 output/<任务名>/")
    print("   - articles/模块XX_*_TASK.md: 模块长文提示词（一个块一篇，读块级逐字稿撰写）")
    print("   - notes/笔记XX_*_TASK.md: 块归并后的复习笔记提示词")
    print("3. 宿主 Agent 主程序以 5 个并发通道（Task子代理或并行生成）读取任务书，直接撰写落盘！")
    print("=" * 65)
    print("【可复制的断点续跑命令示例】：")
    print('python src/cli.py login --sessdata "<你的 SESSDATA>"             # B 站凭证，一次持久化')
    print('python src/cli.py login --douyin-cookie "<你的 Cookie 串>"        # 抖音凭证（匿名只能取约 20 条）')
    print('python src/cli.py pipeline "<链接>" --all --article-type learning')
    print('python src/cli.py pipeline "<链接>" --range 1-10 --article-type learning')
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Video2Book Agent Toolkit (multi-platform video & knowledge extraction)")
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # parse
    p_parse = subparsers.add_parser("parse", help="Parse video topology & list parts (Bilibili URL or local media)")
    p_parse.add_argument("url", help="Bilibili URL/BV ID or local video/audio/directory path")
    p_parse.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)
    p_parse.add_argument("--douyin-cookie", dest="douyin_cookie", default=None, help=DOUYIN_COOKIE_HELP)
    p_parse.add_argument("--limit", type=int, default=10, help="Max items to display")
    p_parse.add_argument("--json", action="store_true", help="Output in JSON format")

    # audio
    p_audio = subparsers.add_parser("audio", help="Fetch & download/extract audio stream")
    p_audio.add_argument("url", help="Bilibili URL/BV ID or local video/audio/directory path")
    p_audio.add_argument("--page", type=int, default=None, help="Page/Part index (auto-detects ?p=X from URL if omitted)")
    p_audio.add_argument("--all", action="store_true", help="Batch download/extract all parts")
    p_audio.add_argument("--range", default=None, help="Episode range to download (e.g. 1-10, 1,3,5)")
    p_audio.add_argument("--quality", choices=["low", "medium", "high"], default="low", help="Audio quality (low=64k speech default, medium=132k, high=192k)")
    p_audio.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_audio.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_audio.add_argument("--force", action="store_true", help="Force re-download/re-extraction even if audio file already exists")
    p_audio.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)
    p_audio.add_argument("--douyin-cookie", dest="douyin_cookie", default=None, help=DOUYIN_COOKIE_HELP)
    p_audio.add_argument("--url-only", action="store_true", help="Only print stream URL without downloading")
    p_audio.add_argument("--output", default=None, help="Optional explicit output directory override")
    p_audio.add_argument("--json", action="store_true", help="Output in JSON format")

    # pipeline
    p_pipe = subparsers.add_parser("pipeline", help="Execute complete automated pipeline (Audio -> ASR -> Notes & Articles)")
    p_pipe.add_argument("url", help="Bilibili URL/BV ID, local video file, or local course directory")
    p_pipe.add_argument("--page", type=int, default=None, help="Page/Part index (auto-detects ?p=X from URL if omitted)")
    p_pipe.add_argument("--all", action="store_true", help="Process all episodes in multi-P collection or local course directory")
    p_pipe.add_argument("--range", default=None, help="Episode range to process (e.g. 1-10, 1,3,5)")
    p_pipe.add_argument("--quality", choices=["low", "medium", "high"], default="low", help="Audio quality (low=64k speech default, medium=132k, high=192k)")
    p_pipe.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_pipe.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_pipe.add_argument("--force", action="store_true", help="Force re-transcribing and re-generating even if exists")
    p_pipe.add_argument("--prefetch-workers", type=int, default=12, help="Parallel audio prefetch (download/extract) threads")
    p_pipe.add_argument("--skip-failed", action="store_true", default=False, help="Explicit opt-in: exempt failed episodes from transcription gate (recorded in manifest skip list)")
    p_pipe.add_argument(
        "--block-minutes", type=float, default=None,
        help="块级转录的块时长目标（分钟）；缺省取环境变量 BVB_AUDIO_BLOCK_MINUTES，再缺省 50（落 40–60 带中段）。"
             "单块硬上限由 BVB_AUDIO_ONESHOT_LIMIT_MINUTES 控制（默认 75）",
    )
    p_pipe.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)
    p_pipe.add_argument("--douyin-cookie", dest="douyin_cookie", default=None, help=DOUYIN_COOKIE_HELP)
    p_pipe.add_argument(
        "--article-type", default=None, dest="article_type",
        help="长文提示词风格（用户确认）：learning=学习（推荐，当前版）/ legacy=旧版（原稳定版）；"
             "未指定时打印风格菜单并请用户确认，确认不了即终止任务。"
             "另有 consulting/interview/review/livestream 四种形态已登记但提示词未提供，命中即终止。",
    )

    # merge-audio：块级转录链路的离线入口（单独重跑装箱合并，幂等）
    p_merge = subparsers.add_parser(
        "merge-audio", help="Merge per-episode audio into transcription blocks and export block task books"
    )
    p_merge.add_argument("workspace", help="Path to an existing course workspace (contains parts.json and audio/)")
    p_merge.add_argument(
        "--block-minutes", type=float, default=None,
        help="块时长目标（分钟）；缺省取 BVB_AUDIO_BLOCK_MINUTES，再缺省 50（落 40–60 带中段）",
    )
    p_merge.add_argument("--force", action="store_true", help="Rebuild blocks even if the manifest signature matches")

    # split-transcript：把块级逐字稿切回分集（机械切分，幂等）
    p_split = subparsers.add_parser(
        "split-transcript", help="Split block transcripts into per-episode transcripts under subtitles/"
    )
    p_split.add_argument("workspace", help="Path to an existing course workspace (contains audio/_blocks/blocks.json)")
    p_split.add_argument("--block", type=int, default=None, help="Only split this block id (default: all blocks)")

    # info
    p_info = subparsers.add_parser("info", help="Show environment & toolchain readiness status")
    p_info.add_argument("--refresh", action="store_true", help="Clear cached status")
    # 中文注释：凭证仅显示来源与脱敏指纹
    p_info.add_argument("--sessdata", help="Optional SESSDATA cookie (only shows source & masked fingerprint)", default=None)
    p_info.add_argument("--douyin-cookie", dest="douyin_cookie", default=None,
                        help="Optional Douyin cookie (only shows source & masked fingerprint)")

    # login / logout：登录凭证持久化（B 站 SESSDATA + 抖音 Cookie）
    p_login = subparsers.add_parser(
        "login", help="Persist Bilibili SESSDATA / Douyin cookie so later commands need no flag")
    p_login.add_argument("--sessdata", help="Bilibili SESSDATA value (no interactive prompt)", default=None)
    p_login.add_argument(
        "--douyin-cookie", dest="douyin_cookie", default=None,
        help="抖音完整 Cookie 串。抖音对匿名访问有硬窗口（实测某博主 216 条只放行 21 条，"
             "合集接口 403），配置登录态 Cookie 是取全量作品的唯一途径。",
    )

    subparsers.add_parser("logout", help="Remove persisted credentials (SESSDATA + Douyin cookie)")

    # cluster-notes
    p_cl = subparsers.add_parser("cluster-notes", help="Merge audio blocks into notes (note_plan.json dispatch) and export note task-files")
    p_cl.add_argument("url", help="Bilibili URL or BV ID")
    p_cl.add_argument("--block-id", type=int, default=None, help="Only process this note number (second-pass note id; legacy flag name)")
    p_cl.add_argument("--start-block", type=int, default=None, help="Start note number")
    p_cl.add_argument("--end-block", type=int, default=None, help="End note number")
    p_cl.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_cl.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_cl.add_argument("--force", action="store_true", help="Force re-exporting note task-files even if they exist")
    p_cl.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)

    # cluster-articles
    p_ca = subparsers.add_parser("cluster-articles", help="Compile each block's module article into a textbook volume in textbooks/")
    p_ca.add_argument("url", help="Bilibili URL or BV ID")
    p_ca.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_ca.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_ca.add_argument("--force", action="store_true", help="Force re-integrating modular textbooks (default: reuse existing textbooks/)")
    p_ca.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)

    # dedup
    p_dd = subparsers.add_parser("dedup", help="Scan and synchronize duplicate audio assets to save LLM tokens")
    p_dd.add_argument("url", help="Bilibili URL, BV ID, or local media path")
    p_dd.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_dd.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_dd.add_argument("--dry-run", action="store_true", help="Only check for duplicates without copying files")
    p_dd.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)

    # cleanup：任务书回收（成品已产出的 *_TASK.md 清场，每类保留 N 份范本）
    p_cl2 = subparsers.add_parser("cleanup", help="Reclaim completed dispatch task-files (*_TASK.md), keeping N samples per category")
    p_cl2.add_argument("--task", default=None, help="Only process workspaces whose folder name contains this keyword")
    p_cl2.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_cl2.add_argument("--keep", type=int, default=1, help="Samples to keep per category (default 1; 0=delete all completed)")
    p_cl2.add_argument("--all", action="store_true", help="Process every workspace under base-dir (compatibility flag; this is already the default)")
    p_cl2.add_argument("--dry-run", action="store_true", help="Only report what would be reclaimed")

    # sync：账本对账（以磁盘产物回填 manifest.json）
    p_sync = subparsers.add_parser("sync", help="Reconcile manifest.json with on-disk products (disk is the source of truth)")
    p_sync.add_argument("--task", default=None, help="Only process workspaces whose folder name contains this keyword")
    p_sync.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_sync.add_argument("--all", action="store_true", help="Process every workspace under base-dir (compatibility flag; this is already the default)")
    p_sync.add_argument("--dry-run", action="store_true", help="Only report the reconciled state without writing")

    args = parser.parse_args()
    if not args.subcommand:
        parser.print_help()
        sys.exit(1)

    # 凭证解析：命令行显式传入优先于本地存档；login 需拿到原始值以区分“未传”。
    raw_sessdata = (getattr(args, "sessdata", None) or "").strip()
    if args.subcommand == "login":
        args.sessdata = raw_sessdata or None
        args.sessdata_source = "命令行参数" if raw_sessdata else None
    else:
        args.sessdata = resolve_sessdata(raw_sessdata)
        args.sessdata_source = "命令行参数" if raw_sessdata else ("本地存档" if args.sessdata else None)

    # 抖音 Cookie：解析顺序与 SESSDATA 同构，但多一条环境变量来源（cookie 串很长，不便反复粘贴）
    if hasattr(args, "douyin_cookie"):
        raw_douyin = (args.douyin_cookie or "").strip()
        if args.subcommand == "login":
            args.douyin_cookie = raw_douyin or None
            args.douyin_cookie_source = "命令行参数" if raw_douyin else None
        else:
            args.douyin_cookie = resolve_douyin_cookie(raw_douyin)
            if raw_douyin:
                args.douyin_cookie_source = "命令行参数"
            elif os.environ.get("DYAUDIO_COOKIE", "").strip():
                args.douyin_cookie_source = "环境变量 $DYAUDIO_COOKIE"
            else:
                args.douyin_cookie_source = "本地存档" if args.douyin_cookie else None

        # 桥接到抖音内核：dyaudio 的 load_config() 本来就认 $DYAUDIO_COOKIE，在这里落一次即可，
        # 不必把 cookie 逐层透传过 coordinator / pipeline / cluster 的十来个调用点——那种写法
        # 漏一处就会**静默少抓**（正是本次要修的病灶）。
        # 于是抖音凭证的优先级统一为：--douyin-cookie > 本地存档 / $DYAUDIO_COOKIE > config.json。
        if args.douyin_cookie:
            os.environ["DYAUDIO_COOKIE"] = args.douyin_cookie

    # 产物根解析：--base-dir 缺省即产物根（绝对路径），使命令与当前工作目录彻底解耦。
    if hasattr(args, "base_dir"):
        args.base_dir = _resolve_base_dir(args.base_dir)

    dispatch = {
        "parse": cmd_parse,
        "audio": cmd_audio,
        "pipeline": cmd_pipeline,
        "merge-audio": cmd_merge_audio,
        "split-transcript": cmd_split_transcript,
        "cluster-notes": cmd_cluster_notes,
        "cluster-articles": cmd_cluster_articles,
        "dedup": cmd_dedup,
        "cleanup": cmd_cleanup,
        "sync": cmd_sync,
        "login": cmd_login,
        "logout": cmd_logout,
        "info": cmd_info,
    }
    try:
        result = dispatch[args.subcommand](args)
        return int(result or 0)
    except PipelineGateError as gate:
        # 流水线硬门禁自带退出码语义（cmd_pipeline 内部已转换，这里只是兜底透传，不改写码值）。
        sys.exit(getattr(gate, "exit_code", 1))
    except (RuntimeError, OSError, subprocess.SubprocessError) as err:
        # 环境类错误（缺 ffmpeg、ffmpeg 异常退出、产物根不可写、网络不通……）统一给人话，
        # 别让裸栈回溯淹没真正原因。BVB_DEBUG=1 时原样抛出，便于排查逻辑错误。
        # 注：SubprocessError 覆盖 ffmpeg 非零退出（CalledProcessError），它不是 OSError 子类。
        if os.environ.get("BVB_DEBUG", "").strip().lower() in ("1", "true", "yes"):
            raise
        print(f"\n[!] {err}", file=sys.stderr)
        print("    可用 `python src/cli.py info` 查看环境与工具链状态。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
