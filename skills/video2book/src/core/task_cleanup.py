"""Task-file Reclaim: 回收已完成的派发任务书（*_TASK.md / *_转录任务书.md）。

架构定位：任务书是工具层写给宿主 Agent 的**临时派发物**；Agent 读完后把成品落到
`subtitles/` / `articles/` / `notes/`。旧版本只负责写、不负责收，于是任务书从第一门课
堆到第 N 门课，目录里混着大量废纸，并且把 `queue_tracker` 的计数也带偏了。

**三类**任务书与成品一一对应（完成单位都是**块**）：

| 类别 | 任务书 | 成品 |
| :--- | :--- | :--- |
| 块级转录 | `subtitles/BLKxx_*_转录任务书.md` | `subtitles/BLKxx_*_逐字稿.md` |
| 模块长文 | `articles/模块XX_*_TASK.md` | `articles/模块XX_*_精读长文.md` |
| 复习笔记 | `notes/笔记XX_*_TASK.md` | `notes/笔记XX_*_笔记.md` |

本模块补齐「收」的这一半：
- **只回收成品已落盘**的任务书；成品未产出的任务书一律保留；
- 每个类别保留编号最小的 1 份作为**提示词范本**（`--keep 0` 可全清）；
- `note_plan_TASK.md` 全库唯一，永不回收。
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fsutil

# 类别标识
CATEGORY_ARTICLES = "articles"
CATEGORY_NOTES = "notes"
CATEGORY_TRANSCRIPTS = "transcripts"

CATEGORY_LABELS = {
    CATEGORY_ARTICLES: "模块长文任务书",
    CATEGORY_NOTES: "笔记任务书",
    CATEGORY_TRANSCRIPTS: "块级转录任务书",
}

# 成品体积门槛：与阶段一门禁一致，避免把空壳文件误判为成品（唯一定义处见 fsutil）
MIN_PRODUCT_BYTES = fsutil.PRODUCT_MIN_BYTES

# 模块长文任务书/成品统一为 `模块XX_…`（一个块一篇）：逐集命名 `PXX_…` 已不再存在
_MODULE_ARTICLE_RE = re.compile(r"^模块(\d+)_")
# 笔记任务书/成品统一为 `笔记XX_…`（块归并后的粒度）
_NOTE_RE = re.compile(r"^笔记(\d+)_")
# 块级转录任务书/成品统一为 `BLKxx_…`（一个块一份）：块号是清单里的事实
_BLOCK_RE = re.compile(r"^BLK(\d+)_")
# 转录任务书 / 逐字稿的文件名后缀（与 `export_block_transcribe_task`、
# `TaskWorkspace.block_path` 同源；这里只做**同名换后缀**的定位，不重算路径）
_TRANSCRIBE_TASK_SUFFIX = "_转录任务书.md"
_BLOCK_TRANSCRIPT_SUFFIX = "_逐字稿.md"


def _article_module_no(name: str) -> Optional[int]:
    m = _MODULE_ARTICLE_RE.match(name)
    return int(m.group(1)) if m else None


def _note_no(name: str) -> Optional[int]:
    m = _NOTE_RE.match(name)
    return int(m.group(1)) if m else None


def _block_no(name: str) -> Optional[int]:
    m = _BLOCK_RE.match(name)
    return int(m.group(1)) if m else None


def _iter_tasks(ws: Any, category: str) -> List[Path]:
    """列出某类别的任务书，按编号升序（编号缺失者排到最后）。"""
    if category == CATEGORY_ARTICLES:
        files = [f for f in ws.articles_dir.glob("模块*_TASK.md")
                 if _article_module_no(f.name) is not None]
    elif category == CATEGORY_NOTES:
        files = [f for f in ws.notes_dir.glob("笔记*_TASK.md")
                 if _note_no(f.name) is not None]
    elif category == CATEGORY_TRANSCRIPTS:
        files = [f for f in ws.subtitles_dir.glob(f"BLK*{_TRANSCRIBE_TASK_SUFFIX}")
                 if _block_no(f.name) is not None]
    else:  # pragma: no cover - 防御式分支
        return []

    def 排序键(f: Path) -> int:
        if category == CATEGORY_NOTES:
            num = _note_no(f.name)
        elif category == CATEGORY_TRANSCRIPTS:
            num = _block_no(f.name)
        else:
            num = _article_module_no(f.name)
        return num if num is not None else 10**9

    return sorted(files, key=排序键)


def _transcript_product_ready(ws: Any, task_file: Path) -> bool:
    """块级逐字稿是否已落盘（同名换后缀，非空即算）。

    转录任务书与逐字稿是**同名不同后缀**的配对（`BLK03_P08_转录任务书.md` ↔
    `BLK03_P08_逐字稿.md`）：块号与覆盖范围就是文件名里的锚，不需要重算路径。
    """
    if not task_file.name.endswith(_TRANSCRIBE_TASK_SUFFIX):
        return False
    transcript = ws.subtitles_dir / (
        task_file.name[: -len(_TRANSCRIBE_TASK_SUFFIX)] + _BLOCK_TRANSCRIPT_SUFFIX
    )
    try:
        return transcript.exists() and transcript.stat().st_size > 0
    except OSError:
        return False


def _article_product_ready(ws: Any, task_file: Path) -> bool:
    """模块长文是否已落盘（按 `模块XX_` 前缀宽容定位，与队列、对账、门禁同口径）。"""
    from .workspace import find_module_article

    module_no = _article_module_no(task_file.name)
    if module_no is None:
        return False
    article = find_module_article(
        ws.articles_dir, {"block_id": module_no}, min_bytes=MIN_PRODUCT_BYTES
    )
    return article is not None


def find_module_note(
    ws: Any,
    module_no: Optional[int],
    preferred_name: Optional[str] = None,
    min_bytes: int = MIN_PRODUCT_BYTES,
) -> Optional[Path]:
    """定位某篇笔记**已落盘**的成品（供复用判定与任务书回收共用）。

    两级定位：
    1. 先试规范文件名（工具层派发时约定的 `笔记XX_<题名>_笔记.md`）；
    2. 再按编号前缀回退匹配 `笔记XX_*.md`——成品的后缀可能不是规范名，只认规范名
       会误判为「无成品」并重复派发。
    """
    notes_dir = ws.notes_dir
    if preferred_name:
        exact = notes_dir / str(preferred_name)
        try:
            if exact.exists() and exact.stat().st_size >= min_bytes:
                return exact
        except OSError:
            pass
    if module_no is None:
        return None
    try:
        candidates = sorted(notes_dir.glob(f"笔记{int(module_no):02d}_*.md"))
    except (OSError, ValueError):
        return None
    for candidate in candidates:
        if candidate.name.endswith("_TASK.md"):
            continue
        try:
            if candidate.stat().st_size >= min_bytes:
                return candidate
        except OSError:
            continue
    return None


def _note_product_ready(ws: Any, task_file: Path) -> bool:
    """模块笔记成品是否已落盘（规范名优先，回退按模块编号前缀匹配）。"""
    preferred = None
    if task_file.name.endswith("_TASK.md"):
        preferred = task_file.name[: -len("_TASK.md")] + "_笔记.md"
    return find_module_note(ws, _note_no(task_file.name), preferred_name=preferred) is not None


def _product_ready(ws: Any, category: str, task_file: Path) -> bool:
    if category == CATEGORY_ARTICLES:
        return _article_product_ready(ws, task_file)
    if category == CATEGORY_TRANSCRIPTS:
        return _transcript_product_ready(ws, task_file)
    return _note_product_ready(ws, task_file)


def reclaim_task_file(task_file: Path, dry_run: bool = False) -> bool:
    """回收单个任务书；返回是否实际删除成功（dry_run 时返回可删与否）。"""
    if not task_file.exists():
        return False
    if dry_run:
        return True
    try:
        task_file.unlink()
        return True
    except OSError:
        return False


def cleanup_completed_tasks(
    ws: Any,
    keep_per_category: int = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """回收各分类中「成品已落盘」的任务书，每类保留编号最小的 N 份作范本。

    返回：{"deleted": [...], "kept": [...], "skipped_pending": [...], "failed_delete": [...], "counts": {...}}
    其中 skipped_pending = 成品未产出而保留 / failed_delete = 成品已产出但删除失败（占用、权限），
    两者必须分开统计——混在一起会把删除失败说成「成品未产出仍保留」，掩盖真实故障。
    路径一律以仓库相对路径呈现，便于日志阅读。
    """
    from .workspace import TaskWorkspace

    keep_n = max(0, int(keep_per_category))
    deleted: List[str] = []
    kept: List[str] = []
    skipped: List[str] = []
    failed_delete: List[str] = []
    counts: Dict[str, Dict[str, int]] = {}

    def 相对(p: Path) -> str:
        return TaskWorkspace.to_relative(p)

    for category in (CATEGORY_ARTICLES, CATEGORY_NOTES, CATEGORY_TRANSCRIPTS):
        tasks = _iter_tasks(ws, category)
        cat_kept: List[str] = []
        cat_deleted = 0
        cat_skipped = 0
        cat_failed = 0

        for idx, task_file in enumerate(tasks):
            if idx < keep_n:
                cat_kept.append(相对(task_file))
                continue
            if _product_ready(ws, category, task_file):
                if reclaim_task_file(task_file, dry_run=dry_run):
                    cat_deleted += 1
                    deleted.append(相对(task_file))
                else:
                    cat_failed += 1
                    failed_delete.append(相对(task_file))
            else:
                cat_skipped += 1
                skipped.append(相对(task_file))

        kept.extend(cat_kept)
        counts[category] = {
            "total": len(tasks),
            "kept": len(cat_kept),
            "deleted": cat_deleted,
            "skipped_pending": cat_skipped,
            "failed_delete": cat_failed,
        }

    return {
        "deleted": deleted,
        "kept": kept,
        "skipped_pending": skipped,
        "failed_delete": failed_delete,
        "counts": counts,
        "dry_run": dry_run,
    }


def reclaim_module_note_task(ws: Any, block_id: int, keep_module: int = 1) -> Optional[str]:
    """模块笔记任务书即时回收（笔记成品已落盘且非范本时）。"""
    if block_id <= keep_module:
        return None
    for task_file in _iter_tasks(ws, CATEGORY_NOTES):
        if _note_no(task_file.name) == block_id and _note_product_ready(ws, task_file):
            if reclaim_task_file(task_file):
                return str(task_file)
    return None


def find_workspaces(base_dir: Any = None) -> List[Any]:
    """枚举 base_dir 下所有可用工作区（含 parts.json/manifest.json 或任一产物子目录）。

    基目录解析统一交给 `paths.resolve_base_dir`：空值即**产物根**（绝对路径，与当前工作目录无关）；
    相对路径优先按当前工作目录解析，不存在则回退到产物根下的同名目录——这样从任意目录调用
    脚本都能找到工作区（README/SKILL 推荐直接跑 scripts/）。
    """
    from . import paths as _paths
    from .workspace import TaskWorkspace

    base = _paths.resolve_base_dir(base_dir)
    if not base.exists():
        return []

    workspaces: List[Any] = []
    seen: set = set()
    # 枚举走 fsutil.iter_child_dirs：条目不可访问（Windows「不受信任的装入点」会连
    # `Path.is_dir()` 都抛 OSError）时只跳过它，不让一门课的坏链接把整轮扫描打断；
    # 链接（junction / 符号链接）与重解析点同样在那里被跳过——它们指向的工作区若被跟随
    # 会被枚举两次（实测：一个手工建的 junction 让 5 个工作区变成 6 个）。
    for child in fsutil.iter_child_dirs(base, skip_hidden=True):
        has_parts = (child / "parts.json").exists() or (child / "manifest.json").exists()
        has_artifacts = any(fsutil.is_dir(child / name) for name in ("articles", "notes", "subtitles", "textbooks"))
        if not (has_parts or has_artifacts):
            continue
        try:
            # 同一真实路径只收一次（大小写、短路径等写法差异也归并掉）
            key = str(child.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            # 直接绑定已存在目录（长目录名不会被 sanitize_name 截断）
            workspaces.append(TaskWorkspace.from_existing(child))
        except OSError:
            # 只吞真正的文件系统错误。**不要**用裸 except Exception：那会把「逻辑错误」
            # 伪装成「这个目录跳过」，让整门课从枚举结果里凭空消失且毫无提示
            # （实测：is_junction 在 3.12 以下抛 AttributeError，被吞掉后 find_workspaces
            #  返回 0 个工作区，cleanup / sync / 质检脚本集体失明）。
            continue
    return workspaces
