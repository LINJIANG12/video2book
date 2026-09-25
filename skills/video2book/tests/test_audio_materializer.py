# -*- coding: utf-8 -*-
"""v4 物理音频路径与按需物化契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.audio_materializer import AudioMaterializationError, AudioMaterializer
from src.core.block_plan import BlockPlan


def test_paths_are_derived_without_audio_files(make_workspace):
    ws = make_workspace("物化路径_BVTEST01")
    plan = BlockPlan.ensure(ws, [
        {"page": 1, "title": "第一讲", "duration": 600, "cid": 1},
        {"page": 2, "title": "第二讲", "duration": 600, "cid": 2},
    ])
    block = plan["blocks"][0]
    assert BlockPlan.audio_ready(ws, block) is False
    assert not list(ws.audio_dir.rglob("*.m4a"))
    assert AudioMaterializer.block_audio_path(ws, block) == BlockPlan.audio_path(ws, block)


def test_materializer_rejects_mismatched_units_and_segments(make_workspace):
    ws = make_workspace("物化校验_BVTEST01")
    block = {
        "block_id": 1, "title": "错误块", "span": "P01", "episodes": [1],
        "duration_sec": 600, "duration_min": 10,
        "units": [{"page": 1, "label": "P01", "split": False, "duration_sec": 600}],
        "segments": [{"page": 1, "label": "P02", "start_sec": 0, "duration_sec": 600}],
    }
    with pytest.raises(AudioMaterializationError, match="unit/segment"):
        AudioMaterializer._validate_block_shape(block)


def test_source_audio_path_uses_parts_title(make_workspace):
    ws = make_workspace("源路径_BVTEST01")
    part = {"page": 7, "title": "第七讲"}
    assert BlockPlan.source_audio_path(ws, part) == ws.audio_dir / "P07_第七讲.m4a"
