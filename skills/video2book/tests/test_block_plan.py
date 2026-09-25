# -*- coding: utf-8 -*-
"""v4 BlockPlan 的纯离线契约测试。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.core.block_plan import BlockPlan, LegacyWorkspaceError


LIMITS = {"target": 50.0, "ceiling": 75.0, "min": 40.0, "max": 60.0}


def _parts(pages: List[int], duration: float = 600.0) -> List[Dict[str, Any]]:
    return [
        {
            "page": page,
            "title": f"第{page}讲 元数据",
            "duration": duration,
            "filepath": f"audio/P{page:02d}_第{page}讲.m4a",
            "cid": f"cid{page:03d}",
        }
        for page in pages
    ]


def test_ensure_writes_only_metadata_plan(make_workspace):
    ws = make_workspace("纯计划_BVTEST01")
    plan = BlockPlan.ensure(ws, _parts([1, 2]), limits=LIMITS, titles={"1": "第一模块"})

    assert BlockPlan.path(ws) == ws.root_dir / "block_plan.json"
    assert BlockPlan.path(ws).is_file()
    assert plan["version"] == 1
    assert plan["plan_source"] == "metadata"
    assert plan["audio_ready"] is False
    assert plan["limits"] == {**LIMITS, "effective": 50.0}
    assert plan["blocks"][0]["title"] == "第一模块"
    assert "audio" not in plan["blocks"][0]
    assert "filepath" not in json.dumps(plan, ensure_ascii=False)
    assert not list(ws.root_dir.rglob("*.m4a"))
    assert not (ws.audio_dir / "blocks").exists()


def test_pack_is_contiguous_and_covers_every_part():
    plan = BlockPlan.plan_from_parts(_parts(list(range(1, 13))), limits=LIMITS)
    blocks = plan["blocks"]

    assert [page for block in blocks for page in block["episodes"]] == list(range(1, 13))
    for block in blocks:
        unit_indexes = [int(unit["page"]) for unit in block["units"]]
        assert unit_indexes == list(range(unit_indexes[0], unit_indexes[-1] + 1))
        assert block["span"] == BlockPlan.span(block)
        assert abs(sum(unit["duration_sec"] for unit in block["units"]) - block["duration_sec"]) < 0.02


def test_split_legs_and_title_fallback_are_metadata_only():
    plan = BlockPlan.plan_from_parts(
        [{"page": 1, "title": "算法专题", "duration": 76 * 60}],
        limits=LIMITS,
        titles={"1": "算法专题模块", "2": "算法专题模块"},
    )
    blocks = plan["blocks"]

    assert [[unit["label"] for unit in block["units"]] for block in blocks] == [["P01上"], ["P01下"]]
    assert [block["span"] for block in blocks] == ["P01上", "P01下"]
    assert [block["title"] for block in blocks] == ["算法专题模块", "算法专题模块"]
    assert blocks[1]["units"][0]["source_offset_sec"] == 2280.0
    assert all(block["audio_ready"] is False for block in blocks)
    assert all("audio" not in block for block in blocks)


def test_append_keeps_existing_blocks_stable(make_workspace):
    ws = make_workspace("连载计划_BVTEST01")
    first_parts = _parts([1, 2])
    first = BlockPlan.ensure(ws, first_parts, limits=LIMITS, titles={"1": "旧块"})
    old_block = copy.deepcopy(first["blocks"][0])

    second = BlockPlan.ensure(
        ws,
        _parts([1, 2, 3]),
        limits=LIMITS,
        titles={"1": "不能改旧块", "2": "新块"},
    )

    assert second["blocks"][0] == old_block
    assert [block["block_id"] for block in second["blocks"]] == [1, 2]
    assert second["blocks"][1]["title"] == "新块"
    assert BlockPlan.load(ws)["blocks"][0] == old_block


def test_append_rejects_changed_old_prefix_and_limits(make_workspace):
    ws = make_workspace("前缀保护_BVTEST01")
    parts = _parts([1, 2])
    BlockPlan.ensure(ws, parts, limits=LIMITS)

    changed = copy.deepcopy(parts)
    changed[0]["title"] = "被改过的旧集"
    with pytest.raises(ValueError, match="前缀"):
        BlockPlan.ensure(ws, changed + _parts([3]), limits=LIMITS)

    with pytest.raises(ValueError, match="limits"):
        BlockPlan.ensure(ws, _parts([1, 2, 3]), limits={**LIMITS, "target": 60.0})


def test_legacy_workspace_is_rejected_without_reading_old_blocks_json(make_workspace):
    ws = make_workspace("旧工作区_BVTEST01")
    legacy = ws.audio_dir / "_blocks" / "blocks.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{ definitely-not-json", encoding="utf-8")

    with pytest.raises(LegacyWorkspaceError):
        BlockPlan.ensure(ws, _parts([1]), limits=LIMITS)

    assert BlockPlan.load(ws) is None
    assert BlockPlan.load_blocks(ws) == []
    assert legacy.read_text(encoding="utf-8") == "{ definitely-not-json"


def test_parts_json_without_v4_plan_is_legacy(make_workspace):
    ws = make_workspace("旧parts_BVTEST01")
    ws.save_parts(_parts([1]))

    with pytest.raises(LegacyWorkspaceError, match="parts.json"):
        BlockPlan.ensure(ws, _parts([1]), limits=LIMITS)


def test_root_title_file_is_used_and_audio_paths_are_only_derived(make_workspace):
    ws = make_workspace("标题与物化_BVTEST01")
    (ws.root_dir / BlockPlan.TITLES_NAME).write_text(
        json.dumps({"1": "根目录标题"}, ensure_ascii=False), encoding="utf-8"
    )
    plan = BlockPlan.ensure(ws, _parts([1, 2]), limits=LIMITS)
    block = plan["blocks"][0]

    expected_single = ws.audio_dir / "P01_第1讲 元数据.m4a"
    assert BlockPlan.audio_path(ws, block) == ws.audio_dir / "blocks" / "BLK01_根目录标题(P01-P02).m4a"
    assert not (ws.audio_dir / "blocks").exists()
    assert BlockPlan.audio_ready(ws, block) is False

    single = copy.deepcopy(block)
    single["episodes"] = [1]
    single["units"] = [single["units"][0]]
    single["span"] = "P01"
    assert BlockPlan.audio_path(ws, single) == expected_single
    expected_single.write_bytes(b"audio")
    assert BlockPlan.audio_ready(ws, single) is True
    expected_single.write_bytes(b"")
    assert BlockPlan.audio_ready(ws, single) is False
