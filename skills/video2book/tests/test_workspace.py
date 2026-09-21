# -*- coding: utf-8 -*-
"""工作区名的推导与找回。

这几条规则都对应实测过的故障（历史工作区名被截断过）：
1. 标题超长时 BV 号被一起截掉 → 后续命令再也找不到工作区；
2. `sanitize_name` strip 结尾下划线 → 推导名与磁盘名差一个字符，命令在别处建空壳；
3. 空壳目录被当成已有工作区 → 对着空目录报「尚无任何语料」；
4. 多个同源候选时乱认 → 静默读写到别门课的产物上。
"""

from __future__ import annotations

from pathlib import Path

from src.core.workspace import TaskWorkspace

# 真实踩坑样本：标题长到会顶穿 TASK_NAME_MAX
LONG_TITLE = (
    "黑马程序员AI大模型NLP自然语言处理保姆级教程，PyTorch实现Transformer完整代码解析"
    "+预训练模型，一套搞定文本分类_翻译_情感分析等实战项目_"
)
BVID = "BV14mdfBDE4Q"
OTHER_BVID = "BV1bbbbbbbbb"


def _populated_dir(base: Path, name: str) -> Path:
    """造一个「有料」工作区：parts.json 是 _populated 的判据。"""
    root = base / name
    (root / "articles").mkdir(parents=True)
    (root / "parts.json").write_text("[]", encoding="utf-8")
    return root


def _shell_dir(base: Path, name: str) -> Path:
    """造一个空壳工作区：只有目录骨架，没有任何产物。"""
    root = base / name
    for sub in ("articles", "notes", "subtitles", "audio"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def test_long_title_keeps_bvid():
    """BV 号是找回工作区的唯一标识，截断必须在它之前停下来。"""
    name = TaskWorkspace.new_task_name(LONG_TITLE, BVID)
    assert name.endswith(f"_{BVID}"), f"超长标题把 BV 号截掉了: {name!r}"
    assert len(name) <= TaskWorkspace.TASK_NAME_MAX


def test_derived_name_is_idempotent():
    """推导名再清洗一次不得变化，否则 __init__ 会把磁盘上的名字改短一个字符。"""
    name = TaskWorkspace.new_task_name(LONG_TITLE, BVID)
    assert TaskWorkspace.sanitize_name(name) == name


def test_trailing_underscore_and_space_dropped():
    assert TaskWorkspace.new_task_name("标题 ", "") == "标题"
    assert TaskWorkspace.new_task_name("标题_", "") == "标题"


def test_same_course_requires_title_prefix():
    """同源 = 标题部分互为前缀；过短的标题不算同源，不同课不得认成同源。"""
    assert TaskWorkspace._same_course(f"甲课程导论_{BVID}", "甲课程导论")
    assert TaskWorkspace._same_course(f"甲课程导论_{BVID}", f"甲课程导论与实战_{OTHER_BVID}")
    assert not TaskWorkspace._same_course(f"甲课程导论_{BVID}", f"乙课程导论_{OTHER_BVID}")
    assert not TaskWorkspace._same_course(f"甲_{BVID}", "甲课程导论")


def test_populated_distinguishes_shell_from_real(tmp_path: Path):
    real = _populated_dir(tmp_path, "有料课程_BV1aaaaaaaaa")
    shell = _shell_dir(tmp_path, "空壳课程_BV1aaaaaaaaa")
    assert TaskWorkspace._populated(real)
    assert not TaskWorkspace._populated(shell), "空壳目录被当成有料"


def test_create_reuses_populated_workspace(tmp_path: Path):
    """盘上已有同源的有料目录时，必须复用而不是在旁边另建一个。"""
    real = _populated_dir(tmp_path, TaskWorkspace.new_task_name(LONG_TITLE, BVID))
    _shell_dir(tmp_path, TaskWorkspace.new_task_name(LONG_TITLE, ""))
    picked = TaskWorkspace.create(title=LONG_TITLE, bvid=BVID, base_dir=tmp_path)
    assert picked.root_dir.name == real.name


def test_find_existing_recovers_truncated_legacy_name(tmp_path: Path):
    """目标名不存在、盘上恰有一个同源目录 → 复用（历史截断名场景）。"""
    stem = LONG_TITLE[:40]
    legacy = _populated_dir(tmp_path, f"{stem}_翻译_情感分析等实战项目_")
    _populated_dir(tmp_path, f"别的课程_{OTHER_BVID}")
    assert TaskWorkspace._find_existing_by_bvid(tmp_path, stem) == legacy.name


def test_find_existing_gives_up_on_ambiguity(tmp_path: Path):
    """多个同源候选时放弃（不猜），绝不静默挑一个。"""
    stem = LONG_TITLE[:40]
    _populated_dir(tmp_path, f"{stem}_翻译_情感分析等实战项目_")
    _populated_dir(tmp_path, f"{stem}续篇_{OTHER_BVID}")
    assert TaskWorkspace._find_existing_by_bvid(tmp_path, stem) is None
