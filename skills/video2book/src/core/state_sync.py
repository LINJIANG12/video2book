"""State Sync: 以磁盘为唯一真相回填 manifest.json（对账）。

问题背景：`manifest.json` 只记录**工具自己派发**的活。宿主 Agent 事后写进 `articles/` 的成品
永远不会回填，于是清单里写着「一集都没完成 / 全流程未完成」，而硬盘上成品早已齐全——
账本与仓库脱节，任何依赖清单的自动判断都会误判。

本模块做一件事：**数硬盘，然后改账本**。
- 阶段一完成度：**按块**统计——v4 `block_plan.json` 里每块是否已有模块长文
  （`articles/模块XX_*_精读长文.md`，≥ `min_article_bytes`）。集号只作为「块覆盖了哪些集」的
  派生视图写进 `details`，不再是完成单位；
- 模块资产：以 `notes/`、`textbooks/` 实际文件为准（排除任务书）；
- 没有 v4 块计划的工作区：**不做阶段一判定**，返回 `stage1_unit="none"`，等待块计划落盘。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from . import fsutil
from .block_plan import BlockPlan
from .workspace import find_module_article, module_article_path

MIN_ARTICLE_BYTES = fsutil.PRODUCT_MIN_BYTES


def _list_products(directory: Path, suffix: str = ".md") -> List[Path]:
    """列出目录下的成品文件（排除 `*_TASK.md` 任务书与隐藏目录）。

    体积判定走 `fsutil.file_size`：不可访问的条目按「空文件」跳过，不打断对账。
    """
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.glob(f"*{suffix}")
        if not p.name.endswith("_TASK.md") and fsutil.file_size(p) >= fsutil.RENDER_MIN_BYTES
    )


def reconcile_workspace_manifest(
    ws: Any,
    min_article_bytes: int = MIN_ARTICLE_BYTES,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """按磁盘现状回填 manifest，返回对账摘要（dry_run 时不落盘）。"""
    from .workspace import TaskWorkspace

    manifest = ws.load_manifest(absolute=True)
    parts = ws.load_parts() or []
    blocks = BlockPlan.load_blocks(ws)

    details: Dict[Any, Dict[str, Any]] = {}
    for entry in manifest.get("details", []):
        if isinstance(entry, dict) and entry.get("page") is not None:
            details[entry["page"]] = dict(entry)
    for part in parts:
        page = part.get("page")
        if page is None:
            continue
        details.setdefault(page, {
            "page": page,
            "title": part.get("title", ""),
            "cid": part.get("cid", 0),
        })

    # ---- 阶段一：按块判定 ----
    block_entries: List[Dict[str, Any]] = []
    success_pages: List[int] = []
    for block in blocks:
        article = find_module_article(ws.articles_dir, block, min_article_bytes)
        pages = [int(p) for p in (block.get("episodes") or [])]
        block_entries.append({
            "block_id": int(block.get("block_id") or 0),
            "span": BlockPlan.span(block),
            "title": str(block.get("title") or ""),
            "episodes": pages,
            "duration_min": float(block.get("duration_min") or 0.0),
            "status": "success" if article is not None else "need-agent-article",
            "article": str(article) if article is not None else "",
            "target_article": str(module_article_path(ws.articles_dir, block)),
        })
        if article is not None:
            success_pages.extend(pages)
    success_pages = sorted(set(success_pages))

    skipped_pages = [p for p in manifest.get("skipped_pages", []) if p is not None]
    done_blocks = [b for b in block_entries if b["status"] == "success"]
    total = len(parts) or len(details)
    effective_total = max(0, total - len(skipped_pages))

    for page in sorted(details, key=lambda x: (x is None, x)):
        entry = details[page]
        if blocks:
            # 逐集视图由「覆盖它的块是否完成」派生：块完成即本集已被教材覆盖
            entry["status"] = "success" if page in success_pages else "need-agent-article"
            if entry["status"] == "success":
                task_prompt = entry.get("task_prompt")
                if task_prompt and not Path(str(task_prompt)).exists():
                    entry.pop("task_prompt", None)
            else:
                entry.pop("article", None)
        else:
            entry["status"] = "need-agent-article"
            entry.pop("article", None)

    pending_pages = [p for p in details if p not in set(success_pages)]

    note_files = _list_products(ws.notes_dir)
    textbook_files = _list_products(ws.root_dir / "textbooks")

    failed_entries = [d for d in manifest.get("failed_episodes", []) if isinstance(d, dict)]
    consolidation_evidence = bool(note_files) and bool(textbook_files)
    stage1_unit = "module" if blocks else "none"
    stage1_complete = bool(blocks) and len(done_blocks) == len(blocks)
    completed = (
        stage1_complete
        and not failed_entries
        and consolidation_evidence
        and effective_total > 0
    )

    updated: Dict[str, Any] = {
        "details": [details[k] for k in sorted(details, key=lambda x: (x is None, x))],
        "blocks": block_entries,
        "blocks_total": len(blocks),
        "blocks_done": len(done_blocks),
        "stage1_unit": stage1_unit,
        "processed_episodes": len([p for p in success_pages if p not in skipped_pages]),
        "episode_total": total,
        "episode_pending": len([p for p in pending_pages if p not in skipped_pages]),
        "pipeline_completed": bool(completed),
        "notes_files": [TaskWorkspace.to_relative(p) for p in note_files],
        "textbooks": [TaskWorkspace.to_relative(p) for p in textbook_files],
        "reconciled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not dry_run:
        merged = dict(manifest)
        merged.update(updated)
        ws.save_manifest(merged)

    return {
        "workspace": ws.root_dir.name,
        "workspace_path": str(ws.root_dir),
        "stage1_unit": stage1_unit,
        "total": total,
        "success": len([p for p in success_pages if p not in skipped_pages]),
        "pending": len([p for p in pending_pages if p not in skipped_pages]),
        "skipped": len(skipped_pages),
        "failed": len(failed_entries),
        "blocks_total": len(blocks),
        "blocks_done": len(done_blocks),
        "notes": len(note_files),
        "textbooks": len(textbook_files),
        "pipeline_completed": bool(completed),
        "dry_run": bool(dry_run),
        "updated_fields": sorted(updated.keys()),
    }


def reconcile_all(base_dir: Any = None, dry_run: bool = False) -> List[Dict[str, Any]]:
    """对 base_dir 下所有工作区逐一执行对账（base_dir 为空即产物根）。"""
    from .task_cleanup import find_workspaces

    return [
        reconcile_workspace_manifest(ws, dry_run=dry_run)
        for ws in find_workspaces(base_dir)
    ]
