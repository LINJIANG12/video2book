#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-Line Interface for Video2Book (multi-platform video & knowledge extraction).

Commands:
  pipeline         - Stage-1 single entry: parse topology (--dry-run) / gather audio+blocks (--audio-only)
                     / full run: audio -> blocks -> dispatch task-files -> auto dedup + cleanup + sync
  merge-audio      - (Re)pack per-episode audio into blocks and export block task files
  cluster-notes    - Aggregate blocks into review notes (task files), then auto cleanup + sync
  cluster-articles - Consolidate module long-forms into modular textbooks, then auto cleanup + sync
  check            - Unified quality gate: --stage1 (grounding) / --deliver (note+render) / --fix-numbering
  cleanup          - Reclaim completed dispatch task-files (*_TASK.md), keeping N samples per category
  sync             - Reconcile manifest.json with on-disk products (disk is the source of truth)
  login / logout   - Persist or clear Bilibili SESSDATA / Douyin credentials (--sessdata / --douyin-cookie)
  info             - Show environment & toolchain readiness status
"""

import argparse
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


def cmd_pipeline(args):
    """阶段一唯一入口：解析 / 取音 / 装块 / 派发任务书，三段共用同一条编排。

    `--dry-run`（只解析拓扑）与 `--audio-only`（只收音频并装箱）取代了原先独立的
    `parse` / `audio` 子命令；完整运行时，去重、任务书回收与账本对账自动执行。
    """
    mode = "full"
    if getattr(args, "dry_run", False):
        mode = "dry-run"
    elif getattr(args, "audio_only", False):
        mode = "audio-only"
    # 只有完整链路才需要长文风格；解析与取音阶段尚未写长文任务书，不必打扰用户。
    article_type = _confirm_article_prompt_style(args) if mode == "full" else ""
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
            mode=mode,
        )
    except PipelineGateError as gate:
        sys.exit(gate.exit_code)


def _workspace_from_path(raw: str) -> TaskWorkspace:
    """按目录路径绑定一个**已存在**的工作区（`merge-audio` 使用）。

    为什么用路径而不是 URL：这是**离线重跑**——块清单与音频已经在盘上，再走一次
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
    print("[*] 启动笔记流水线（块清单 → 语义归并 → 笔记任务书）")
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
        print("[✓] 本命令已正常结束（未派发笔记）：块清单缺失，界面已给出装箱命令。")
    else:
        _gen = sum(1 for r in outcome["results"] if r.get("status") == "generated")
        _cached = len(outcome["results"]) - _gen
        print(f"[✓] 笔记流水线执行完毕！{len(outcome['blocks'])} 个块 → {len(outcome['notes'])} 篇笔记")
        print(f"[✓] 已导出 {_gen} 份笔记任务书（另有 {_cached} 篇成品已存在，跳过派发）")
    print(f"[✓] 任务书目录: {ws.notes_dir}")
    print("=" * 65)
    _autoclose_workspace(ws, "笔记聚合收尾")


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
    print("[*] 启动教材整编流水线：按块序把各块模块长文整编成册 (Modular Textbook Integration)")
    print(f"[*] 课程标题: 《{course_title}》")
    print(f"[*] 任务工作区: {ws.root_dir}")
    print(f"[*] 目标教材目录: {ws.root_dir / 'textbooks'}")
    print("=" * 65)

    # 模块边界 = 块边界（`audio/_blocks/blocks.json`）：音频按 40–60 分钟装箱，一块一篇模块长文。
    # 没有块清单就没有模块可整编——提示先装箱，而不是退回按标题前缀硬分组（那正是被废除的老路）。
    blocks = (AudioMerger.load_manifest(ws) or {}).get("blocks") or []
    if not blocks:
        print("[!] 本工作区没有块清单，无法整编教材：模块边界来自音频装箱。")
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
    _autoclose_workspace(ws, "教材整编收尾")


def _autoclose_workspace(ws, label: str) -> None:
    """聚合类命令的固定收尾：回收已完成任务书 + 按磁盘对账回填 manifest.json。

    原 `cleanup` / `sync` 是 Agent 需要额外记两条命令的手动步骤，漏跑只会让账本与产物脱节；
    既然工具已能判定「成品是否齐备」，就把它们并进主流程（子命令仍保留供单独调用）。
    """
    try:
        from src.core.task_cleanup import cleanup_completed_tasks
        _reclaim = cleanup_completed_tasks(ws, keep_per_category=1)
        if _reclaim["deleted"]:
            print(f"[*] {label}：已回收 {len(_reclaim['deleted'])} 份已完成任务书（每类保留 1 份范本）")
    except Exception as err:
        print(f"[!] {label}：任务书回收已跳过：{err}", file=sys.stderr)
    try:
        from src.core.state_sync import reconcile_workspace_manifest
        _sync = reconcile_workspace_manifest(ws)
        print(f"[*] {label}：账本对账 分集 {_sync['success']}/{_sync['total']} 集达标 | "
              f"模块笔记 {_sync['notes']} 份 | 教材 {_sync['textbooks']} 部")
    except Exception as err:
        print(f"[!] {label}：账本对账已跳过：{err}", file=sys.stderr)


def cmd_check(args):
    """交付质量门禁统一入口（合并原三个质检脚本 + 标题去号清理脚本）。

    - `--stage1`：阶段一放行门禁——模块长文是否基于本块逐字稿（实体覆盖率）；
    - `--deliver`（默认）：交付前体检——笔记成色 + 渲染合规；
    - `--fix-numbering`：存量产物标题手写序号就地清理（幂等，可先 `--dry-run` 预演）；
    - `--strict`：致命项才返回非零退出码（默认提示级）。
    """
    if getattr(args, "fix_numbering", False):
        from src.core.heading_cleanup import run_fix_numbering
        return run_fix_numbering(
            base_dir=getattr(args, "base_dir", None),
            task=getattr(args, "task", None),
            dir_path=getattr(args, "dir", None),
            only=getattr(args, "only", "both"),
            dry_run=bool(getattr(args, "dry_run", False)),
            as_json=bool(getattr(args, "json", False)),
            max_samples=int(getattr(args, "max_samples", 5) or 5),
            hash_nonheading=bool(getattr(args, "hash_nonheading", False)),
        )

    from src.core.quality_gate import run_deliver, run_stage1
    if getattr(args, "stage1", False) and not getattr(args, "deliver", False):
        return run_stage1(
            base_dir=getattr(args, "base_dir", None),
            task=getattr(args, "task", None),
            dir_path=getattr(args, "dir", None),
            min_freq=int(getattr(args, "min_freq", 2) or 2),
            min_coverage=float(getattr(args, "min_coverage", 0.5) or 0.5),
            strict=bool(getattr(args, "strict", False)),
            as_json=bool(getattr(args, "json", False)),
        )
    return run_deliver(
        base_dir=getattr(args, "base_dir", None),
        task=getattr(args, "task", None),
        dir_path=getattr(args, "dir", None),
        max_truncated=int(getattr(args, "max_truncated", 4) or 4),
        require_structure=bool(getattr(args, "require_structure", False)),
        require_lang=bool(getattr(args, "require_lang", False)),
        require_no_numbering=bool(getattr(args, "require_no_numbering", False)),
        strict=bool(getattr(args, "strict", False)),
        as_json=bool(getattr(args, "json", False)),
    )


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
        print("                  `pipeline` 会在音频阶段失败。安装并加入 PATH：")
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

    # pipeline：阶段一唯一入口（解析 / 取音 / 装块 / 派发任务书共用一条编排）
    p_pipe = subparsers.add_parser(
        "pipeline",
        help="Stage-1 single entry: parse topology (--dry-run) / audio+blocks (--audio-only) / full run",
    )
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
    # 解析 / 取音两个轻量入口收敛进 pipeline（原 `parse` / `audio` 子命令）
    p_pipe.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="只解析拓扑并列出将处理的分集，不下载音频、不写任务书")
    p_pipe.add_argument("--audio-only", action="store_true", dest="audio_only",
                        help="只收齐音频并装箱、导出块级转录任务书后即返回（不派发长文任务书）")

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

    # check：交付质量门禁统一入口（阶段一放行 / 交付前体检 / 存量标题去号）
    p_check = subparsers.add_parser(
        "check",
        help="Unified quality gate: --stage1 (grounding) / --deliver (note+render, default) / --fix-numbering",
    )
    p_check.add_argument("--stage1", action="store_true", help="阶段一放行门禁：模块长文是否基于本块逐字稿")
    p_check.add_argument("--deliver", action="store_true", help="交付前体检：笔记成色 + 渲染合规（默认）")
    p_check.add_argument("--fix-numbering", action="store_true", dest="fix_numbering",
                         help="存量产物标题手写序号就地清理（幂等；加 --dry-run 预演）")
    p_check.add_argument("--dir", default=None, help="直接指定单个工作区目录")
    p_check.add_argument("--task", default=None, help="仅处理目录名包含该关键字的工作区")
    p_check.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_check.add_argument("--json", action="store_true", help="JSON 输出")
    p_check.add_argument("--strict", action="store_true", help="存在致命项才返回非零（默认提示级）")
    p_check.add_argument("--min-freq", type=int, default=2, dest="min_freq",
                         help="[--stage1] 实体在逐字稿里的最低出现次数（默认 2）")
    p_check.add_argument("--min-coverage", type=float, default=0.5, dest="min_coverage",
                         help="[--stage1] 覆盖率下限（默认 0.5）")
    p_check.add_argument("--max-truncated", type=int, default=4, dest="max_truncated",
                         help="[--deliver] 每份笔记允许的断句上限（默认 4）")
    p_check.add_argument("--require-structure", action="store_true", dest="require_structure",
                         help="[--deliver] 把「结构缺件」纳入门禁")
    p_check.add_argument("--require-lang", action="store_true", dest="require_lang",
                         help="[--deliver] 把「围栏缺语言标识」纳入门禁")
    p_check.add_argument("--require-no-numbering", action="store_true", dest="require_no_numbering",
                         help="[--deliver] 把「标题手写序号」纳入门禁")
    p_check.add_argument("--only", choices=("textbooks", "articles", "both"), default="both",
                         help="[--fix-numbering] 只处理哪一类（默认 both）")
    p_check.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="[--fix-numbering] 只报不改")
    p_check.add_argument("--max-samples", type=int, default=5, dest="max_samples",
                         help="[--fix-numbering] 每类最多打印几条样例（默认 5）")
    p_check.add_argument("--hash-nonheading", action="store_true", dest="hash_nonheading",
                         help="[--fix-numbering] 打印非标题行内容指纹")

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
    p_cl.add_argument("url", help="Bilibili URL, BV ID, or local media path")
    p_cl.add_argument("--block-id", type=int, default=None, help="Only process this note number (second-pass note id; legacy flag name)")
    p_cl.add_argument("--start-block", type=int, default=None, help="Start note number")
    p_cl.add_argument("--end-block", type=int, default=None, help="End note number")
    p_cl.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_cl.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_cl.add_argument("--force", action="store_true", help="Force re-exporting note task-files even if they exist")
    p_cl.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)

    # cluster-articles
    p_ca = subparsers.add_parser("cluster-articles", help="Compile each block's module article into a textbook volume in textbooks/")
    p_ca.add_argument("url", help="Bilibili URL, BV ID, or local media path")
    p_ca.add_argument("--task", default=None, help="Custom task workspace folder name")
    p_ca.add_argument("--base-dir", default=None, help=BASE_DIR_HELP)
    p_ca.add_argument("--force", action="store_true", help="Force re-integrating modular textbooks (default: reuse existing textbooks/)")
    p_ca.add_argument("--sessdata", help="Optional SESSDATA cookie", default=None)

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
        "pipeline": cmd_pipeline,
        "merge-audio": cmd_merge_audio,
        "cluster-notes": cmd_cluster_notes,
        "cluster-articles": cmd_cluster_articles,
        "check": cmd_check,
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
