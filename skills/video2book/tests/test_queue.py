# -*- coding: utf-8 -*-
"""`scripts/queue_tracker.py` 的对外契约。

这是全仓最容易被重构悄悄改坏的接口：主程序靠它拿「下一批该派发什么」的载荷，载荷一旦
缺字段或退出码变化，派发链路会静默停摆（没有异常，只是再也不派发）。因此这里把
**键名、退出码、台账字段**当作契约钉住，而不是钉实现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write_blocks

from src.core import budget
from src.core.block_plan import BlockPlan

# 派发载荷的字段清单（缺一个主程序就读不到该做什么）
MODULE_ITEM_KEYS = (
    "block_id", "span", "title", "episodes", "duration_min", "block_audio",
    "transcript_file", "transcript_bytes", "task_file", "target_article", "dispatch_prompt",
)
TRANSCRIBE_ITEM_KEYS = (
    "block_id", "span", "audio_file", "task_file", "block_transcript", "dispatch_prompt",
)
NOTE_ITEM_KEYS = ("note_id", "title", "blocks_str", "task_file", "target_note", "dispatch_prompt")
BUDGET_KEYS = (
    "suggest_workers", "suggest_batch", "audio_tokens_per_sec",
    "context_window_tokens", "dispatch_required", "total_audio_min",
)


@pytest.fixture
def dispatch_ws(make_workspace, blocks_factory):
    """一个「音频已装箱但尚未转录」的工作区：三集、一个块、块音频已落盘。"""
    ws = make_workspace("探针课_BV1probe001")
    ws.save_parts([
        {"page": 1, "title": "导学", "duration": 900},
        {"page": 2, "title": "变量", "duration": 1200},
    ])
    block = blocks_factory(1, [1, 2], title="导学与变量", span="P01-P02",
                           duration_min=35.0)
    write_blocks(ws, [block])
    block_audio = BlockPlan.audio_path(ws, block)
    block_audio.parent.mkdir(parents=True, exist_ok=True)
    block_audio.write_bytes(b"x" * 20000)
    return ws


def _payload(res) -> dict:
    assert res.code == 0, f"queue_tracker 退出码 {res.code}: {res.out[-400:]}"
    return json.loads(res.out)


def _write_transcript(ws, name: str = "BLK01_P01-P02_逐字稿.md") -> Path:
    path = ws.subtitles_dir / name
    path.write_text("逐字稿正文" * 200, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# --base-dir 的两种口径
# ---------------------------------------------------------------------------

def test_base_dir_accepts_products_root_or_workspace(run_queue, dispatch_ws):
    """`--base-dir` 既可以指产物根（下面含工作区），也可以直接指工作区目录。"""
    as_root = run_queue("--base-dir", str(dispatch_ws.base_dir), "--summary")
    as_ws = run_queue("--base-dir", str(dispatch_ws.root_dir), "--summary")
    assert as_root.code == 0, as_root.out[-200:]
    assert as_ws.code == 0, as_ws.out[-200:]
    assert as_root.out.split(";")[0] == as_ws.out.split(";")[0], "两种 --base-dir 口径结果不一致"


# ---------------------------------------------------------------------------
# --next-module / --next-transcribe
# ---------------------------------------------------------------------------

def test_next_module_payload_shape_without_transcript(run_queue, dispatch_ws):
    """逐字稿未就绪时不派发该块（写作角色不该领到没有语料的块），但载荷骨架要齐备。"""
    payload = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-module", "1", "--json"))
    assert "budget" in payload and "next" in payload
    for key in BUDGET_KEYS:
        assert key in payload["budget"], f"budget 缺少字段：{key}"
    assert payload["next"] == []


def test_next_module_does_not_write_ledger_by_default(run_queue, dispatch_ws):
    """默认零写入：不加 `--log-dispatch` 就不许动台账。"""
    _payload(run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-module", "1", "--json"))
    assert not (dispatch_ws.root_dir / ".dispatch_log.jsonl").exists()


def test_next_transcribe_payload_shape(run_queue, dispatch_ws):
    payload = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-transcribe", "1", "--json"))
    items = payload["next_transcribe"]
    assert len(items) == 1, items
    item = items[0]
    for key in TRANSCRIBE_ITEM_KEYS:
        assert key in item, f"转录载荷缺少字段：{key}"
    assert "转录任务书文件" in item["dispatch_prompt"]
    assert item["block_transcript"] in item["dispatch_prompt"]
    block = BlockPlan.load_blocks(dispatch_ws)[0]
    assert Path(item["audio_file"]) == BlockPlan.audio_path(dispatch_ws, block)
    assert item["task_file"].endswith("BLK01_P01-P02_转录任务书.md")


def test_next_transcribe_skips_block_without_physical_audio(run_queue, dispatch_ws):
    """块计划存在但物理块音频缺失时，不得派发一个必然失败的转录任务。"""
    block = BlockPlan.load_blocks(dispatch_ws)[0]
    BlockPlan.audio_path(dispatch_ws, block).unlink()
    payload = _payload(run_queue(
        "--base-dir", str(dispatch_ws.root_dir), "--next-transcribe", "1", "--json"
    ))
    assert payload["next_transcribe"] == []
    assert payload["transcript"]["blocks_total"] == 1


def test_next_module_payload_shape_with_transcript(run_queue, dispatch_ws):
    _write_transcript(dispatch_ws)
    item = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir),
                              "--next-module", "1", "--json"))["next"][0]
    for key in MODULE_ITEM_KEYS:
        assert key in item, f"模块载荷缺少字段：{key}"
    assert item["block_id"] == 1
    assert item["span"] == "P01-P02"
    assert item["episodes"] == [1, 2]
    assert item["transcript_file"].endswith("BLK01_P01-P02_逐字稿.md")
    assert item["transcript_bytes"] > 0
    assert item["target_article"].endswith("模块01_导学与变量_精读长文.md")
    assert item["task_file"].endswith("模块01_导学与变量_TASK.md")
    assert item["task_file"] in item["dispatch_prompt"], "派发词未包含任务书路径"
    assert item["target_article"] in item["dispatch_prompt"], "派发词未包含目标长文路径"


def test_next_module_empty_after_article_written(run_queue, dispatch_ws):
    """模块长文已落盘（≥1000 字节）→ 该块不再派发。"""
    _write_transcript(dispatch_ws)
    (dispatch_ws.articles_dir / "模块01_导学与变量_精读长文.md").write_text("正文" * 400, encoding="utf-8")
    payload = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-module", "1", "--json"))
    assert payload["next"] == [], "模块长文已存在却仍派发"


# ---------------------------------------------------------------------------
# --next-note
# ---------------------------------------------------------------------------

def test_next_note_payload_shape(run_queue, dispatch_ws):
    task = dispatch_ws.notes_dir / "笔记01_导学与变量_TASK.md"
    task.write_text("# 笔记 01 导学与变量 笔记任务书（NOTE_TASK）\n> 涵盖块：块 01\n", encoding="utf-8")
    items = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir),
                               "--next-note", "1", "--json"))["next_note"]
    assert len(items) == 1, items
    item = items[0]
    for key in NOTE_ITEM_KEYS:
        assert key in item, f"笔记载荷缺少字段：{key}"
    assert item["note_id"] == 1
    assert "复习笔记任务书文件" in item["dispatch_prompt"]
    assert item["target_note"] in item["dispatch_prompt"]


def test_next_note_empty_after_product_written(run_queue, dispatch_ws):
    task = dispatch_ws.notes_dir / "笔记01_导学与变量_TASK.md"
    task.write_text("# 笔记 01 导学与变量 笔记任务书（NOTE_TASK）\n", encoding="utf-8")
    (dispatch_ws.notes_dir / "笔记01_导学与变量_笔记.md").write_text("笔记成品正文" * 300, encoding="utf-8")
    payload = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-note", "1", "--json"))
    assert payload["next_note"] == [], "笔记成品已落盘却仍被派发"


def test_next_note_merged_mode_not_shadowed_by_old_product(run_queue, dispatch_ws):
    """归并模式下，同编号的**旧粒度**成品不得遮蔽新归并任务书的派发。"""
    (dispatch_ws.notes_dir / "笔记01_导学与变量_TASK.md").write_text(
        "# 笔记 01 导学与变量 笔记任务书（NOTE_TASK）\n", encoding="utf-8")
    (dispatch_ws.root_dir / "note_plan.json").write_text(json.dumps(
        [{"note_id": 1, "note_title": "导学与变量", "blocks": [1], "core_theme": "x"}],
        ensure_ascii=False), encoding="utf-8")
    (dispatch_ws.notes_dir / "笔记01_旧粒度主题_笔记.md").write_text("旧笔记成品" * 300, encoding="utf-8")
    items = _payload(run_queue("--base-dir", str(dispatch_ws.root_dir),
                               "--next-note", "1", "--json"))["next_note"]
    assert len(items) == 1, "归并模式下新任务书被同编号旧笔记错误遮蔽"


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------

def test_log_dispatch_appends_ledger_entry(run_queue, dispatch_ws):
    """`--log-dispatch` 往工作区追加台账，记录的是**块号**而不是别的编号。"""
    _write_transcript(dispatch_ws)
    res = run_queue("--base-dir", str(dispatch_ws.root_dir), "--next-module", "1", "--json", "--log-dispatch")
    assert res.code == 0, res.out[-300:]
    log_path = dispatch_ws.root_dir / ".dispatch_log.jsonl"
    assert log_path.exists(), "--log-dispatch 未写出台账"
    entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["suggested_blocks"] == [1]
    assert entry["requested"] == 1
    assert entry["audio_tokens_per_sec"] == budget.audio_tokens_per_sec()


# ---------------------------------------------------------------------------
# 预算系数与阈值口径
# ---------------------------------------------------------------------------

def test_audio_tokens_per_sec_env_override(run_queue, dispatch_ws):
    res = run_queue("--base-dir", str(dispatch_ws.root_dir), "--summary",
                    env_extra={"BVB_AUDIO_TOKENS_PER_SEC": "100"})
    assert res.code == 0, res.out[-200:]
    assert "AUDIO_TOKENS_PER_SEC=100" in res.out, res.out[:200]
    assert "SUGGEST_WORKERS=" in res.out and "DISPATCH_REQUIRED=" in res.out


@pytest.mark.parametrize("total_sec,episodes,expected", [
    (30 * 60, 4, False),        # 30 分钟 4 集：可串行
    (4 * 3600, 40, True),       # 4 小时 40 集：必须派发
])
def test_dispatch_required_thresholds(total_sec, episodes, expected):
    assert budget.dispatch_required(total_sec, episodes=episodes) is expected


def test_serial_ok_for_short_course():
    assert budget.serial_ok(30 * 60) is True


@pytest.mark.parametrize("minutes,expected", [(190, 6), (2, 2)])
def test_suggest_workers_floor_and_cap(minutes, expected):
    assert budget.suggest_workers(minutes) == expected


@pytest.mark.parametrize("duration_ms,expected,reason", [
    (35_000, 5, "短集多集应建议打包"),
    (80_000, 1, "长集不应打包"),
])
def test_suggest_batch_bundles_short_episodes_only(duration_ms, expected, reason):
    assert budget.suggest_batch([duration_ms] * 20, 20) == expected, reason
