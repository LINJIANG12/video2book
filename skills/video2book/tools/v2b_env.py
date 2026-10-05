"""video2book 派发工具的共用上下文：产物根、工作区定位、子进程环境。

**不硬编码任何课程或路径**。解析顺序与工具链本体一致：

- 产物根：`$BVB_OUTPUT_DIR` → 容器布局 `<home>/output` → `cwd/output`
  （直接复用 `src.core.paths.products_root()`，与 `cli.py` 同一条解析链，
  避免脚本与工具链对「产物落在哪」产生两套口径）
- 工作区：`--pattern/-p` 命令行 → `$V2B_PATTERN` → **产物根下只有一个工作区时自动选中**
  （单课程目录是绝大多数场景；多课程并存时才需要显式指定）

用法（三个脚本共用）：
    from v2b_env import Context
    ctx = Context.from_argv(sys.argv[1:])
    ctx.output_root      # Path
    ctx.workspace        # Path
    ctx.pattern          # str | None
    ctx.subprocess_env   # dict，可直接传给 subprocess 的 env=
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_PATTERN = "V2B_PATTERN"
ENV_BASE_DIR = "V2B_BASE_DIR"

# 不参与「非 -- 参数」解析的开关（会吃掉后面跟着的值）
_VALUE_FLAGS = {"--pattern", "-p", "--base-dir", "-b", "--out", "-o"}
_FLAG_ALIAS = {"-p": "--pattern", "-b": "--base-dir", "-o": "--out"}


def _load_products_root() -> Path:
    """复用工具链本体的产物根解析，避免脚本另立一套口径。"""
    try:
        from src.core.paths import products_root  # type: ignore
        return Path(products_root()).resolve()
    except Exception:
        # 兜底：本技能目录的祖先里找 output/；都没有就用 cwd/output
        for base in [ROOT, *ROOT.parents]:
            cand = base / "output"
            if cand.is_dir():
                return cand.resolve()
        return (Path.cwd() / "output").resolve()


def list_workspaces(output_root: Path, pattern: Optional[str] = None) -> List[Path]:
    """列出产物根下的课程工作区目录（`pattern` 为子串过滤）。"""
    if not output_root.is_dir():
        return []
    out = []
    for d in sorted(output_root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        if pattern and pattern not in d.name:
            continue
        out.append(d)
    return out


def split_argv(argv: List[str]):
    """把 `--pattern X` / `-p X` / `--base-dir X` / `--env-pattern X` 从 argv 里摘出来。

    返回 `(positional, options)`；`options` 的键统一为 `--pattern` / `--base-dir` / `--env-pattern`。
    """
    positional: List[str] = []
    options: Dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise SystemExit("%s 缺少取值" % a)
            options[_FLAG_ALIAS.get(a, a)] = argv[i + 1]
            i += 2
            continue
        if "=" in a and a.split("=", 1)[0] in _VALUE_FLAGS:
            key, val = a.split("=", 1)
            options[_FLAG_ALIAS.get(key, key)] = val
            i += 1
            continue
        positional.append(a)
        i += 1
    return positional, options


class Context:
    """一次调用里解析好的路径上下文。"""

    def __init__(self, output_root: Path, workspace: Path, pattern: Optional[str]):
        self.output_root = output_root
        self.workspace = workspace
        self.pattern = pattern

    @classmethod
    def build(cls, pattern: Optional[str] = None, base_dir: Optional[str] = None) -> "Context":
        root = Path(base_dir).expanduser().resolve() if base_dir else _load_products_root()
        hits = list_workspaces(root, pattern)
        if not hits:
            raise SystemExit(
                "在 %s 下没有匹配 %r 的课程工作区。\n"
                "  · 先跑 pipeline 建工作区；或\n"
                "  · 用 --pattern <课程关键字> 指定，或设置 $%s" % (root, pattern, ENV_PATTERN)
            )
        if len(hits) > 1:
            names = "\n".join("    - %s" % d.name for d in hits[:12])
            more = "\n    …共 %d 个" % len(hits) if len(hits) > 12 else ""
            raise SystemExit(
                "匹配到 %d 个工作区，请用 --pattern 精确指定：\n%s%s" % (len(hits), names, more)
            )
        ws = hits[0]
        return cls(root, ws, pattern)

    @classmethod
    def from_argv(cls, argv: List[str]) -> "Context":
        _positional, options = split_argv(argv)
        pattern = options.get("--pattern") or os.environ.get(ENV_PATTERN) or None
        # 产物根也允许外部钉死（CI / 多产物根并存时用），否则走工具链本体的解析链
        base_dir = (
            options.get("--base-dir")
            or os.environ.get(ENV_BASE_DIR)
            or None
        )
        return cls.build(pattern=pattern, base_dir=base_dir)

    @property
    def subprocess_env(self) -> Dict[str, str]:
        """给 queue_tracker 子进程用的环境：把产物根钉死，别让子进程按它自己的 cwd 重新解析。"""
        return dict(
            os.environ,
            BVB_OUTPUT_DIR=str(self.output_root),
            PYTHONIOENCODING="utf-8",
        )

    def tracker_cmd(self, *args: str) -> List[str]:
        """构造一条指向当前工作区的 queue_tracker 命令（pattern 命中唯一时省略以免过度约束）。"""
        cmd = [sys.executable, "scripts/queue_tracker.py"]
        if self.pattern:
            cmd += ["--pattern", self.pattern]
        return cmd + list(args)