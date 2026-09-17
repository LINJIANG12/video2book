#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task-file Reclaim (任务书回收) —— 独立可执行入口。

任务书（`*_TASK.md`）是工具层写给宿主 Agent 的临时派发物，成品（`articles/` /
`subtitles/kernels/` / `notes/`）落地后即可回收，每个类别保留编号最小的 N 份作为提示词范本。
成品尚未产出的任务书一律保留，不会误删正在进行的派发。

用法：
    python scripts/cleanup_tasks.py --dry-run                 # 预演（推荐先跑）
    python scripts/cleanup_tasks.py                           # 回收全部工作区，每类留 1 份
    python scripts/cleanup_tasks.py --task 数据库             # 只处理名称含「数据库」的工作区
    python scripts/cleanup_tasks.py --keep 0                  # 全部回收，不留范本
    python scripts/cleanup_tasks.py --json                    # 机器可读输出
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.console import enable_utf8_console  # noqa: E402

# 控制台硬化：输出含 `✓`/`▶` 等符号，管道捕获时若按 locale(cp936) 编码会崩。
enable_utf8_console()

from src.core.task_cleanup import (  # noqa: E402
    CATEGORY_LABELS,
    cleanup_completed_tasks,
    find_workspaces,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reclaim completed dispatch task-files (*_TASK.md)")
    parser.add_argument("--base-dir", default=None,
                        help="工作区基目录（默认：由 src/core/paths.py 解析的产物根——默认 <当前工作目录>/output，在容器内工作时为 <容器根>/output）")
    parser.add_argument("--task", default=None, help="仅处理目录名包含该关键字的工作区")
    parser.add_argument("--keep", type=int, default=1, help="每类保留的范本数量（默认 1；0=全部回收）")
    parser.add_argument("--dry-run", action="store_true", help="仅预演，不实际删除")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--strict", action="store_true", help="回收后仍存在应删任务书则返回非零（CI 用）")
    args = parser.parse_args()

    from src.core.paths import resolve_base_dir  # noqa: E402  （脚本头部已注入仓库根）

    base_dir = resolve_base_dir(args.base_dir)
    workspaces = find_workspaces(base_dir)
    if args.task:
        workspaces = [w for w in workspaces if str(args.task) in w.root_dir.name]
    if not workspaces:
        print(f"[ERROR] 在 {base_dir} 下未找到可用工作区", file=sys.stderr)
        return 1

    keep_n = max(0, int(args.keep))
    report: List[Dict[str, Any]] = []
    total_deleted = total_kept = total_skipped = total_failed_delete = 0

    for ws in workspaces:
        result = cleanup_completed_tasks(ws, keep_per_category=keep_n, dry_run=args.dry_run)
        total_deleted += len(result["deleted"])
        total_kept += len(result["kept"])
        total_skipped += len(result["skipped_pending"])
        total_failed_delete += len(result.get("failed_delete", []))
        report.append({"workspace": ws.root_dir.name, **result})

    if args.json:
        print(json.dumps({
            "dry_run": bool(args.dry_run),
            "total_deleted": total_deleted,
            "total_kept": total_kept,
            "total_skipped_pending": total_skipped,
            "total_failed_delete": total_failed_delete,
            "workspaces": report,
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 68)
    print(f"[*] 任务书回收（{'预演，不落盘' if args.dry_run else '执行删除'}；每类保留 {keep_n} 份范本）")
    print("=" * 68)
    for item in report:
        print(f"\n▶ {item['workspace']}")
        for category, stat in item["counts"].items():
            print(
                f"    {CATEGORY_LABELS[category]:<12} 共 {stat['total']:>4} 份 | "
                f"回收 {stat['deleted']:>4} | 保留范本 {stat['kept']} | "
                f"成品未产出仍保留 {stat['skipped_pending']} | 删除失败 {stat.get('failed_delete', 0)}"
            )
        for path in item["kept"]:
            print(f"    [留] {path}")

    print("\n" + "=" * 68)
    verb = "可回收" if args.dry_run else "已回收"
    print(f"[✓] {verb}任务书 {total_deleted} 份 | 保留范本 {total_kept} 份 | "
          f"成品未产出仍保留 {total_skipped} 份 | 删除失败 {total_failed_delete} 份")
    print("[i] note_plan_TASK.md 属课程级规划任务书，唯一存在，永不回收。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
