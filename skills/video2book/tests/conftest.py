# -*- coding: utf-8 -*-
"""video2book 行为测试脚手架。

`scripts/selfcheck.py` 原来是一个 3186 行的扁平断言序列，其中约三成钉的是**实现形态**
（「某个已删文件不许回来」、源码子串、提示词措辞）而非行为。本套测试承接其中的**行为断言**：

- 一个测试函数只验一件事，失败点直接落在出问题的那条契约上；
- 公共构造（工作区、块清单、CLI 调用）收在 fixture 里，不再每个检查各写一份；
- 不依赖开发机上的 `output/`、容器标记或任何真实语料：需要工作区的地方一律传显式
  `base_dir`，落在 pytest 的 tmp 目录里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

# 技能根 = 安装单元 = skills/video2book/（src / scripts / SKILL.md / references 都在这里）
SKILL_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_ROOT.parent.parent

if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


# ---------------------------------------------------------------------------
# 路径 fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def skill_root() -> Path:
    return SKILL_ROOT


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def cli_path(skill_root: Path) -> Path:
    return skill_root / "src" / "cli.py"


@pytest.fixture(scope="session")
def queue_tracker_path(skill_root: Path) -> Path:
    return skill_root / "scripts" / "queue_tracker.py"


# ---------------------------------------------------------------------------
# 环境隔离
# ---------------------------------------------------------------------------

# 块装箱口径与凭证都属于「运行环境」，会把结果带偏；逐条清掉让默认值生效。
_ENV_TO_CLEAR = (
    "BVB_AUDIO_BLOCK_MINUTES",
    "BVB_AUDIO_BLOCK_MIN_MINUTES",
    "BVB_AUDIO_BLOCK_MAX_MINUTES",
    "BVB_AUDIO_ONESHOT_LIMIT_MINUTES",
    "BVB_PREFETCH_WORKERS",
    "DYAUDIO_COOKIE",
    "SESSDATA",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def utf8_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ---------------------------------------------------------------------------
# CLI 调用
# ---------------------------------------------------------------------------

class CommandResult:
    """一次子进程调用的结果；`out` 是 stdout+stderr 的合并文本。"""

    def __init__(self, code: int, out: str, argv: Sequence[str]):
        self.code = code
        self.out = out
        self.argv = list(argv)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于失败信息
        return f"CommandResult(code={self.code}, argv={self.argv!r})"

    @property
    def norm(self) -> str:
        """把开发机绝对路径换成占位符，便于断言固定文本。"""
        return normalize(self.out)


def normalize(text: str) -> str:
    """归一化：抹掉机器相关路径与时间戳，只留行为相关的内容。"""
    out = text
    for raw, token in ((REPO_ROOT, "<REPO>"), (SKILL_ROOT, "<SKILL>")):
        out = out.replace(str(raw), token)
    import re
    out = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "<TS>", out)
    out = re.sub(r"耗时\s*[\d.]+\s*(?:s|秒|分钟)", "耗时 <ELAPSED>", out)
    return out


def _run(argv: Sequence[str], env: Dict[str, str], cwd: Optional[Path]) -> CommandResult:
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=str(cwd or SKILL_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    return CommandResult(proc.returncode, proc.stdout, argv)


@pytest.fixture
def run_cli(cli_path: Path, utf8_env: Dict[str, str]):
    """调用 `src/cli.py`。用法：`run_cli("info")` / `run_cli("check", "--stage1", "--dir", ws)`。"""

    def _call(*args: str, cwd: Optional[Path] = None, env_extra: Optional[Dict[str, str]] = None) -> CommandResult:
        env = dict(utf8_env)
        if env_extra:
            env.update(env_extra)
        return _run([str(cli_path), *args], env, cwd)

    return _call


@pytest.fixture
def run_queue(queue_tracker_path: Path, utf8_env: Dict[str, str]):
    """调用 `scripts/queue_tracker.py`。"""

    def _call(*args: str, cwd: Optional[Path] = None, env_extra: Optional[Dict[str, str]] = None) -> CommandResult:
        env = dict(utf8_env)
        if env_extra:
            env.update(env_extra)
        return _run([str(queue_tracker_path), *args], env, cwd)

    return _call


# ---------------------------------------------------------------------------
# 工作区构造
# ---------------------------------------------------------------------------

@pytest.fixture
def products_root(tmp_path: Path) -> Path:
    """临时的产物根：工作区一律建在它下面，绝不碰开发机的 output/。"""
    root = tmp_path / "products"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def make_workspace(products_root: Path):
    """按名字造一个工作区（目录骨架由 TaskWorkspace 自己建）。"""
    from src.core.workspace import TaskWorkspace

    def _make(task_name: str = "测试课程_BVTEST01"):
        return TaskWorkspace(task_name, base_dir=products_root)

    return _make


@pytest.fixture
def make_parts(make_workspace):
    """造工作区并写入 `parts.json`（分集列表缓存）。"""

    def _make(pages: Sequence[int] = (1, 2), titles: Optional[Dict[int, str]] = None,
              task_name: str = "测试课程_BVTEST01"):
        ws = make_workspace(task_name)
        titles = titles or {}
        parts: List[Dict[str, Any]] = []
        for page in pages:
            title = titles.get(page, f"第{page}讲 测试单元")
            parts.append({
                "page": page,
                "cid": f"cid{page:03d}",
                "title": title,
                "duration": 600,
                "filepath": str(ws.audio_dir / f"P{page:02d}_{title}.m4a"),
                "url": f"https://example.invalid/p{page}",
            })
        ws.save_parts(parts)
        return ws

    return _make


def write_blocks(ws, blocks: Sequence[Dict[str, Any]], **meta: Any) -> Path:
    """写一份 v4 根块计划；下游只认这个文件。"""
    payload: Dict[str, Any] = {
        "version": 1,
        "plan_source": "metadata",
        "limits": {
            "target": 50.0,
            "ceiling": 75.0,
            "min": 40.0,
            "max": 60.0,
        },
        "parts_signature": "test-signature",
        "blocks": list(blocks),
    }
    payload.update(meta)
    from src.core.block_plan import BlockPlan

    return BlockPlan.save(ws, payload)


@pytest.fixture
def blocks_factory():
    """造块条目的工厂：`make_block(1, [1], "第1讲")`。"""

    def _make(block_id: int, episodes: Sequence[int], title: str = "", span: str = "",
              duration_min: float = 30.0, **extra: Any) -> Dict[str, Any]:
        eps = [int(e) for e in episodes]
        span = span or (f"P{eps[0]:02d}" if len(eps) == 1 else f"P{eps[0]:02d}-P{eps[-1]:02d}")
        labels = [span] if len(eps) == 1 else [f"P{page:02d}" for page in eps]
        block: Dict[str, Any] = {
            "block_id": int(block_id),
            "title": title or f"第{eps[0]}讲 测试单元",
            "span": span,
            "episodes": eps,
            "units": [
                {"page": page, "label": label, "split": False, "source_offset_sec": 0.0}
                for page, label in zip(eps, labels)
            ],
            "episode_split": False,
            "duration_sec": duration_min * 60,
            "duration_min": duration_min,
            "segments": [],
            "single_episode": len(eps) == 1,
        }
        block.update(extra)
        return block

    return _make


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def pad_to(text: str, min_bytes: int = 1000) -> str:
    """把正文补到阈值以上：门禁按字节数判定「成品是否已产出」，样本太短会被当成空壳。"""
    body = text
    while len(body.encode("utf-8")) < min_bytes:
        body += "\n补充说明：本条用于把样本补到产物阈值以上，内容本身不参与判定。\n"
    return body


@pytest.fixture
def has_ffmpeg() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
