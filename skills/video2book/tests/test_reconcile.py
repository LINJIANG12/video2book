# -*- coding: utf-8 -*-
"""账本对账（`sync`）与清单路径可移植性。

`sync` 被 pipeline 包在 try/except 里：它崩了只会打印「账本对账已跳过」然后继续，命令
看起来一切正常，而账本从此不再回填。实测事故是 `find_module_article` 的第一个参数是
**articles 目录**、而 `state_sync` 传了工作区对象——每次 reconcile 都抛 TypeError 被吞掉。
所以这里的断言要点是「对账真的跑通了」，而不是「对账函数存在」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.core import paths
from src.core.state_sync import reconcile_workspace_manifest
from src.core.workspace import TaskWorkspace

DRIVE_RE = re.compile(r"[A-Za-z]:[\\/]")


@pytest.fixture
def sync_ws(make_workspace, blocks_factory):
    """两集一块、块音频尚不存在的工作区（对账只读清单与长文，不碰音频）。"""
    ws = make_workspace("sync_probe")
    ws.save_parts([{"page": 1, "title": "绪论"}, {"page": 2, "title": "数制"}])
    block = blocks_factory(1, [1, 2], title="绪论与数制", span="P01-P02",
                           audio="audio/_blocks/探针_01_绪论与数制(P01-P02).m4a",
                           duration_min=46.0)
    block["segments"] = []
    block_dir = ws.audio_dir / "_blocks"
    block_dir.mkdir(parents=True, exist_ok=True)
    (block_dir / "blocks.json").write_text(json.dumps({
        "version": 2, "target_minutes": 50.0, "min_minutes": 40.0, "max_minutes": 60.0,
        "effective_limit_minutes": 75.0, "course_short": "探针", "noop": False,
        "input_signature": "probe", "blocks": [block],
    }, ensure_ascii=False), encoding="utf-8")
    return ws


# ---------------------------------------------------------------------------
# 对账按块跑通
# ---------------------------------------------------------------------------

def test_reconcile_runs_in_block_units(sync_ws):
    """对账必须跑通并如实报「0/1 块完成」，而不是抛异常被 pipeline 吞掉。"""
    report = reconcile_workspace_manifest(sync_ws, dry_run=True)
    assert report["stage1_unit"] == "module", report
    assert report["blocks_total"] == 1 and report["blocks_done"] == 0, report
    assert report["success"] == 0 and report["pending"] == 2, report
    assert report["pipeline_completed"] is False, report


def test_reconcile_marks_block_and_episodes_done(sync_ws):
    """模块长文落盘后，该块与它覆盖的集号一并记为完成，并写回 manifest。"""
    (sync_ws.articles_dir / "模块01_绪论与数制_精读长文.md").write_text("正文" * 400, encoding="utf-8")
    report = reconcile_workspace_manifest(sync_ws)
    assert report["blocks_done"] == 1, report
    assert report["success"] == 2 and report["pending"] == 0, report
    manifest = json.loads(sync_ws.manifest_file.read_text(encoding="utf-8"))
    assert manifest["blocks_done"] == 1 and manifest["stage1_unit"] == "module", manifest
    assert all(d["status"] == "success" for d in manifest["details"]), manifest["details"]


# ---------------------------------------------------------------------------
# 清单路径可移植
# ---------------------------------------------------------------------------

def test_relative_base_is_products_root_parent():
    """相对路径基准必须恰好是产物根的父目录（容器布局与平台安装两种情形都成立的前提）。

    不能用 home_root() 当基准：它只是容器的**提示值**，没有容器信号时可能落到与产物根
    不同的盘/树，跨盘 relativize 会退化成绝对路径。
    """
    assert TaskWorkspace.REPO_ROOT == paths.products_root().parent


def test_to_relative_yields_portable_posix_path():
    """产物根内的路径相对化后必须是纯相对、无盘符、无反斜杠。"""
    rel = TaskWorkspace.to_relative(paths.products_root() / "__probe__" / "模块01_甲_精读全书.md")
    assert rel == f"{paths.products_root().name}/__probe__/模块01_甲_精读全书.md", rel
    assert not DRIVE_RE.search(rel) and "\\" not in rel


def test_relativize_covers_scalar_and_list_path_fields(make_workspace):
    """路径字段（含列表型与嵌套 details）一律按 to_relative 归一，绝不原样落盘绝对路径。"""
    ws = make_workspace("portable_probe")
    raw = {
        "textbooks": [str(ws.root_dir / "textbooks" / "模块01_甲_精读全书.md")],
        "notes_files": [str(ws.notes_dir / "模块01_甲_笔记.md")],
        "note_file": str(ws.notes_dir / "模块01_甲_笔记.md"),
        "kernel_file": str(ws.subtitles_dir / "kernels" / "P01_甲_kernel.json"),
        "details": [{"page": 1, "article": str(ws.articles_dir / "P01_甲_精读文章.md")}],
    }
    rel = ws.relativize_obj(raw)
    assert rel["textbooks"] == [TaskWorkspace.to_relative(raw["textbooks"][0])]
    assert rel["notes_files"] == [TaskWorkspace.to_relative(raw["notes_files"][0])]
    assert rel["note_file"] == TaskWorkspace.to_relative(raw["note_file"])
    assert rel["kernel_file"] == TaskWorkspace.to_relative(raw["kernel_file"])
    assert rel["details"][0]["article"] == TaskWorkspace.to_relative(raw["details"][0]["article"])


def test_absolutize_restores_readable_paths(make_workspace):
    """反方向：读回时列表型路径字段必须逐项绝对化，程序内部可直接读取。"""
    ws = make_workspace("portable_probe")
    raw_file = str(ws.notes_dir / "模块01_甲_笔记.md")
    back = ws.absolutize_obj({"notes_files": [raw_file], "note_file": raw_file,
                              "details": [{"page": 1, "article": raw_file}]})
    assert back["notes_files"] == [raw_file]
    assert Path(back["note_file"]).is_absolute()
    assert Path(back["details"][0]["article"]).is_absolute()


def test_save_manifest_never_writes_backslashes(make_workspace):
    """落盘的 manifest 里不得出现 Windows 反斜杠路径。"""
    ws = make_workspace("portable_probe")
    article = str(ws.articles_dir / "模块01_甲_精读长文.md")
    note = str(ws.notes_dir / "模块01_甲_笔记.md")
    ws.save_manifest({
        "textbooks": [article],
        "knowledge_blocks_results": [{"block_id": 1, "note_file": note}],
    })
    text = ws.manifest_file.read_text(encoding="utf-8")
    assert "\\\\" not in text, "manifest.json 落盘了 Windows 反斜杠路径"
    payload = json.loads(text)
    assert payload["textbooks"] == [TaskWorkspace.to_relative(article)]
    assert payload["knowledge_blocks_results"][0]["note_file"] == TaskWorkspace.to_relative(note)
