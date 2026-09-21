"""文件系统健壮性：把「条目在当前平台不可访问」降级为「跳过」，不让单个坏链接打垮整轮扫描。

背景（实测）：产物根里如果存在一个**不受信任的装入点**（手工建的 junction / 符号链接，
或指向其它卷的重解析点），Windows 会在 `stat()` 阶段直接拒绝：

    OSError: [WinError 448] 无法遍历该路径，因为它包含不受信任的装入点。

`Path.exists()` 会把这个错误吞掉返回 False，但 `Path.is_dir()` / `Path.stat()` /
`Path.rglob()` **不会**——于是 `find_workspaces()` 在枚举产物根第一层子目录时就抛错，
cleanup / sync / 两个质检脚本 / 自检里的真实产物门禁**集体失明**：一门课的一个坏链接
就足以让所有体检报错退出，而不是跳过那一门继续跑。

对策：
1. 枚举一律走 `os.scandir`——Windows 下目录项属性来自目录缓存，`follow_symlinks=False`
   时无需额外 `stat()`，因此不会踩到「不受信任的装入点」这个异常；
2. 每个条目的探测各自 `try/except OSError`，单个条目出错只跳过它，不中断整轮枚举；
3. 链接与重解析点整体跳过——它们指向的工作区若被跟随会被枚举两次（实测：一个手工建的
   junction 让 5 个工作区变成 6 个）。
"""

import fnmatch
import os
from pathlib import Path
from typing import Iterator, List, Union

PathLike = Union[str, "os.PathLike[str]"]

# Windows 文件属性位：FILE_ATTRIBUTE_REPARSE_POINT（junction / 符号链接 / 其它重解析点）
_REPARSE_POINT_ATTR = 0x400

# ---------------------------------------------------------------------------
# 体积门槛（**全仓唯一定义处**）
# ---------------------------------------------------------------------------
# 为什么必须只有一处：这两个数决定「这个文件算不算已产出」，而它们被阶段一门禁、
# 笔记归并、任务书回收、模块长文定位、教材整编同时消费。散落成多个字面量时会出现
# 「这边算完成、那边算空壳」的对账裂缝——而且不会报错，只会静默地把同一份文件
# 一会儿当成品、一会儿当待办。
#
# PRODUCT_MIN_BYTES：成品是否成立。低于它视为空壳/占位（任务书、写了一半的文件），
# 需要重新派发。1000 字节是「一段真实正文」与「一个标题加几行占位」的经验分界。
PRODUCT_MIN_BYTES = 1000
# RENDER_MIN_BYTES：渲染门禁的扫描下限。比 PRODUCT_MIN_BYTES 低得多，因为它的目的
# 只是「别去 lint 一个空文件」，不是判断成品是否成立。
RENDER_MIN_BYTES = 200


def is_product(path: PathLike, min_bytes: int = PRODUCT_MIN_BYTES) -> bool:
    """该路径是否已是一份**成立**的成品（存在、是文件、且体积达到门槛）。"""
    try:
        return Path(path).is_file() and file_size(path) >= min_bytes
    except OSError:
        return False


def is_dir(path: PathLike) -> bool:
    """`Path.is_dir()` 的安全版：不可访问（含 WinError 448）一律当作「不是目录」。

    `Path.is_dir()` 只对 ENOENT / ENOTDIR / ELOOP 等少量错误返回 False，
    Windows 的「不受信任的装入点」不在其列，会直接把异常抛给调用方；
    `os.path.isdir` 内部吞掉 `OSError` / `ValueError`，正是这里需要的语义。
    """
    return os.path.isdir(path)


def file_size(path: PathLike) -> int:
    """文件字节数；不可访问（含 WinError 448）时返回 0。

    等价于「这个文件为空 / 不可用」——调用方通常拿它做体积门槛判定，
    返回 0 会让该文件被当作未就绪跳过，而不是把整轮扫描打断。
    """
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def is_reparse_point(path: PathLike) -> bool:
    """是否符号链接 / junction / 其它重解析点；**不可访问时同样返回 True**（按不可遍历处理）。"""
    target = Path(path)

    try:
        if target.is_symlink():
            return True
    except OSError:
        return True

    # Python 3.12+ 提供 Path.is_junction，旧版本用能力探测降级（与 task_cleanup 同一口径）。
    is_junction = getattr(Path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction(target):
                return True
        except OSError:
            return True

    # 3.8~3.11 兜底：读文件属性位（follow_symlinks=False 不跟随，避免访问不可信目标）。
    try:
        st = os.stat(target, follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT_ATTR)


def iter_child_dirs(base: PathLike, skip_hidden: bool = False) -> Iterator[Path]:
    """安全枚举 `base` 的直接子目录：单个条目出错只跳过，链接/重解析点整体跳过。

    `base` 本身不可访问时产出空序列（不抛异常）——调用方据此走「无工作区」分支即可，
    比让脚本整体崩掉更可解释。
    """
    try:
        with os.scandir(base) as scanner:
            entries = sorted(scanner, key=lambda ent: ent.name)
    except OSError:
        return

    for ent in entries:
        try:
            if skip_hidden and ent.name.startswith("."):
                continue
            # follow_symlinks=False：Windows 下只用目录项缓存属性，不做额外的 stat()。
            if not ent.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue

        child = Path(ent.path)
        if is_reparse_point(child):
            continue
        yield child


def iter_files(
    base: PathLike,
    pattern: str = "*",
    skip_hidden_dirs: bool = False,
) -> Iterator[Path]:
    """安全递归枚举 `base` 下匹配 `pattern` 的文件（等价 `rglob`，但遇错整支跳过）。

    与 `Path.rglob()` 的差别：任一目录不可读（含 Windows「不受信任的装入点」）时，
    只跳过那一支并继续其余分支，不抛出；重解析点不跟随，避免同一批文件被算两遍。
    命中顺序：同一目录内按名字排序，先本层文件、再逐个子目录深度优先。
    """
    yield from _walk_files(Path(base), pattern, skip_hidden_dirs)


def _walk_files(directory: Path, pattern: str, skip_hidden_dirs: bool) -> Iterator[Path]:
    try:
        with os.scandir(directory) as scanner:
            entries = sorted(scanner, key=lambda ent: ent.name)
    except OSError:
        return

    subdirs: List[Path] = []
    for ent in entries:
        try:
            is_directory = ent.is_dir(follow_symlinks=False)
        except OSError:
            continue

        if is_directory:
            if skip_hidden_dirs and ent.name.startswith("."):
                continue
            subdirs.append(Path(ent.path))
            continue

        if fnmatch.fnmatch(ent.name, pattern):
            yield Path(ent.path)

    for subdir in subdirs:
        if is_reparse_point(subdir):
            continue
        yield from _walk_files(subdir, pattern, skip_hidden_dirs)
