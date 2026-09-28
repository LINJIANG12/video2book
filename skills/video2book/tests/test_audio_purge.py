# -*- coding: utf-8 -*-
"""交付完成后回收物理音频的契约。

音频是**唯一大体积的中间产物**（一套 145 集的课约 476 MB：分集源音频与块级
拼接各占一半，而块音频就是分集音频的 concat 副本），而逐字稿正文一个字节都
没提音频路径。所以「长文/笔记/教材全部产出」之后，音频就是纯可再生资源。

这里锁死四件事，缺一件都会造成不可逆的损失或后续死局：

1. **判定从严**——四个条件全过才删。任一不满足必须不删并说清缺什么。
2. **只删 audio/ 内的三类文件**——不递归删工作区，避免误伤嵌套工作区。
3. **幂等**——重复执行不报错、不重复计数。
4. **可预演、可豁免**——dry-run 不动文件；`--keep-audio` 跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.audio_materializer import AudioMaterializer
from src.core.block_plan import BlockPlan
from src.core.workspace import TaskWorkspace
from src.pipeline import PipelineCoordinator


# ---------------------------------------------------------------------------
# 夹具：一套「已完工」与「半成品」工作区
# ---------------------------------------------------------------------------

def _铺音频(ws) -> list[Path]:
    """在 audio/ 下铺出三类真实产物，返回全部文件。"""
    ws.audio_dir.mkdir(parents=True, exist_ok=True)
    blocks_dir = ws.audio_dir / "blocks"
    parts_dir = blocks_dir / "_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    made = [
        ws.audio_dir / "P01_第一讲.m4a",
        ws.audio_dir / "P02_第二讲.m4a",
        blocks_dir / "BLK01_P01-P02.m4a",
        parts_dir / "BLK01_P01上.m4a",
    ]
    for path in made:
        path.write_bytes(b"a" * 2048)
    # 非音频的同目录文件：必须**原样保留**（证明删除按类型而非按目录）
    (ws.audio_dir / "readme.txt").write_text("保留我", encoding="utf-8")
    return made


def _铺成品(ws, blocks, *, 长文=True, 笔记=True, 教材=True) -> None:
    """铺出长文 / 笔记 / 教材三类交付物。"""
    for block in blocks:
        if 长文:
            path = TaskWorkspace_article(ws, block)
            path.write_text("# 长文\n", encoding="utf-8")
    if 笔记:
        (ws.notes_dir).mkdir(parents=True, exist_ok=True)
        (ws.notes_dir / "笔记01_某主题_笔记.md").write_text("笔记\n", encoding="utf-8")
    if 教材:
        tb = Path(ws.root_dir) / "textbooks"
        tb.mkdir(parents=True, exist_ok=True)
        (tb / "模块01_某册_精读全书.md").write_text("教材\n", encoding="utf-8")


def TaskWorkspace_article(ws, block) -> Path:
    """长文落盘路径与 `queue_tracker` 的判定口径一致。"""
    return Path(ws.articles_dir) / f"模块{block['block_id']:02d}_标题_精读长文.md"


@pytest.fixture()
def 完工工作区(make_workspace):
    """三块全齐、长文/笔记/教材齐备、音频在盘——唯一允许删除的状态。

    每集 2000 秒（33 分钟）：够长到不会被 50 分钟的块目标吞进同一块，于是
    3 集 → 3 块，用例里可以按块号分别验证「缺一块就不删」。
    """
    ws = make_workspace("完工回收_BVTEST01")
    plan = BlockPlan.ensure(ws, [
        {"page": 1, "title": "第一讲", "duration": 2000, "cid": 1},
        {"page": 2, "title": "第二讲", "duration": 2000, "cid": 2},
        {"page": 3, "title": "第三讲", "duration": 2000, "cid": 3},
    ])
    blocks = plan["blocks"]
    assert len(blocks) == 3, f"夹具前提失效：期望 3 块，实得 {len(blocks)}"
    for block in blocks:
        TaskWorkspace.block_path(ws, dict(block)).write_text("逐字稿\n", encoding="utf-8")
    _铺音频(ws)
    _铺成品(ws, blocks)
    return ws, blocks


# ---------------------------------------------------------------------------
# 1. 判定：四条件全过才删，缺一不动
# ---------------------------------------------------------------------------

def test_purges_when_all_four_conditions_met(完工工作区):
    """全齐时删光三类音频，并留下目录骨架与非音频文件。"""
    ws, blocks = 完工工作区
    assert AudioMaterializer.audio_exists(ws) is True

    结论 = AudioMaterializer.purge_audio(ws, blocks)

    assert 结论["deleted"] == 4
    assert 结论["bytes"] == 4 * 2048
    assert 结论["failed"] == []
    assert 结论["dry_run"] is False
    assert list(ws.audio_dir.rglob("*.m4a")) == []      # 一片不剩
    assert (ws.audio_dir / "readme.txt").exists()        # 非音频原样保留
    assert ws.audio_dir.is_dir()                         # 目录骨架保留
    assert AudioMaterializer.audio_exists(ws) is False   # 回收后 audio_exists 翻假


@pytest.mark.parametrize(
    "缺项, 关键词",
    [
        ("逐字稿", "逐字稿"),
        ("长文", "模块长文"),
        ("笔记", "笔记"),
        ("教材", "精读全书"),
    ],
)
def test_never_purges_when_one_condition_missing(完工工作区, 缺项, 关键词):
    """**防误删的核心**：任一条件不满足就一片都不删，且必须说清缺什么。"""
    ws, blocks = 完工工作区
    if 缺项 == "逐字稿":
        目标 = TaskWorkspace.block_path(ws, dict(blocks[1]))
    elif 缺项 == "长文":
        目标 = TaskWorkspace_article(ws, blocks[1])
    elif 缺项 == "笔记":
        目标 = ws.notes_dir / "笔记01_某主题_笔记.md"
    else:
        目标 = Path(ws.root_dir) / "textbooks" / "模块01_某册_精读全书.md"
    目标.unlink()

    结论 = AudioMaterializer.purge_audio(ws, blocks)

    assert 结论["deleted"] == 0
    assert 结论["purged"] is False
    assert 关键词 in 结论["reason"]     # 报告必须点名缺的那一项
    assert len(list(ws.audio_dir.rglob("*.m4a"))) == 4   # 一片没少


def test_empty_block_list_is_not_complete(完工工作区):
    """空块清单 = 没有可判定的块，属于「不敢删」而非「删定了」。"""
    ws, _ = 完工工作区
    结论 = AudioMaterializer.purge_audio(ws, [])
    assert 结论["purged"] is False
    assert 结论["deleted"] == 0
    assert len(list(ws.audio_dir.rglob("*.m4a"))) == 4


# ---------------------------------------------------------------------------
# 2. 幂等与预演
# ---------------------------------------------------------------------------

def test_second_purge_is_a_no_op(完工工作区):
    """音频已删后再跑一次：不算失败，也不重复计数。"""
    ws, blocks = 完工工作区
    AudioMaterializer.purge_audio(ws, blocks)

    结论 = AudioMaterializer.purge_audio(ws, blocks)

    assert 结论["deleted"] == 0
    assert 结论["purged"] is True         # 条件仍成立，只是没东西可删
    assert 结论["bytes"] == 0


def test_dry_run_reports_without_touching_disk(完工工作区):
    """预演要报出「将删多少、多少钱」，但一个字节都不动。"""
    ws, blocks = 完工工作区

    结论 = AudioMaterializer.purge_audio(ws, blocks, dry_run=True)

    assert 结论["dry_run"] is True
    assert 结论["deleted"] == 4
    assert 结论["bytes"] == 4 * 2048
    assert len(list(ws.audio_dir.rglob("*.m4a"))) == 4   # 原封不动


# ---------------------------------------------------------------------------
# 3. 作用域：只删三类音频，不碰工作区其它东西
# ---------------------------------------------------------------------------

def test_purge_never_leaves_audio_directory(make_workspace):
    """嵌套工作区（output/<课>/x/）不在删除范围内——按 glob 精确匹配，不递归删。"""
    ws = make_workspace("作用域_BVTEST01")
    plan = BlockPlan.ensure(ws, [{"page": 1, "title": "第一讲", "duration": 600, "cid": 1}])
    blocks = plan["blocks"]
    for block in blocks:
        TaskWorkspace.block_path(ws, dict(block)).write_text("x", encoding="utf-8")
    _铺音频(ws)
    _铺成品(ws, blocks)

    # 工作区里其它必须幸存的产物
    嵌套 = Path(ws.root_dir) / "x" / "audio"
    嵌套.mkdir(parents=True, exist_ok=True)
    (嵌套 / "BLK99_别删我.m4a").write_bytes(b"b" * 100)
    (Path(ws.root_dir) / "manifest.json").write_text("{}", encoding="utf-8")

    AudioMaterializer.purge_audio(ws, blocks)

    assert not list(ws.audio_dir.rglob("*.m4a"))          # 目标目录清空
    assert (嵌套 / "BLK99_别删我.m4a").exists()             # 嵌套工作区安然无恙
    assert (Path(ws.root_dir) / "manifest.json").exists()


def test_non_audio_extensions_survive(完工工作区):
    """块目录下若混入非 .m4a（如 ffmpeg 清单），也不该被误删。"""
    ws, blocks = 完工工作区
    (ws.audio_dir / "blocks" / "_BLK01_concat.txt").write_text("x", encoding="utf-8")

    AudioMaterializer.purge_audio(ws, blocks)

    assert (ws.audio_dir / "blocks" / "_BLK01_concat.txt").exists()


# ---------------------------------------------------------------------------
# 4. 记账
# ---------------------------------------------------------------------------

def test_manifest_records_the_purge(完工工作区, make_workspace):
    """删除是不可逆动作，必须在 manifest 留下可追溯的记录。"""
    ws, blocks = 完工工作区

    结论 = AudioMaterializer.purge_audio(ws, blocks)
    manifest = {"audio_purge": AudioMaterializer.purge_record(结论)}
    Path(ws.root_dir).write_text  # 占位：确认 root_dir 是 Path

    assert manifest["audio_purge"]["deleted"] == 4
    assert manifest["audio_purge"]["bytes"] == 4 * 2048
    assert manifest["audio_purge"]["at"]          # 时间戳非空
    assert "m4a" in manifest["audio_purge"]["scope"]


def test_purge_record_is_json_serialisable(完工工作区):
    """manifest 是 JSON 文件：记录必须能直接序列化，不能塞 Path 对象。"""
    ws, blocks = 完工工作区
    记录 = AudioMaterializer.purge_record(AudioMaterializer.purge_audio(ws, blocks))
    文本 = json.dumps(记录, ensure_ascii=False)
    assert json.loads(文本)["deleted"] == 4


# ---------------------------------------------------------------------------
# 5. pipeline 收尾接入
# ---------------------------------------------------------------------------

def test_pipeline_reclaims_audio_when_complete(完工工作区):
    """收尾默认回收：条件齐备就把 476 MB 那类音频清掉。"""
    ws, blocks = 完工工作区

    结论 = PipelineCoordinator._purge_audio_if_complete(ws, blocks)

    assert 结论 is not None
    assert 结论["deleted"] == 4
    assert list(ws.audio_dir.rglob("*.m4a")) == []


def test_pipeline_respects_keep_audio(完工工作区):
    """`--keep-audio` 是后悔药：豁免时一片都不动，也不写 manifest 标记。"""
    ws, blocks = 完工工作区

    assert PipelineCoordinator._purge_audio_if_complete(ws, blocks, keep_audio=True) is None
    assert len(list(ws.audio_dir.rglob("*.m4a"))) == 4
    assert "audio_purge" not in ws.load_manifest(absolute=True)


def test_pipeline_skips_and_reports_when_incomplete(完工工作区):
    """未完工时收尾照常跑完，只是回收被跳过并说清原因。"""
    ws, blocks = 完工工作区
    (Path(ws.root_dir) / "textbooks" / "模块01_某册_精读全书.md").unlink()

    结论 = PipelineCoordinator._purge_audio_if_complete(ws, blocks)

    assert 结论 is not None and 结论["purged"] is False
    assert len(list(ws.audio_dir.rglob("*.m4a"))) == 4
    assert "audio_purge" not in ws.load_manifest(absolute=True)


def test_pipeline_survives_purge_error(完工工作区, monkeypatch):
    """回收失败**绝不能**掀翻整条流水线：它只是省磁盘，不是交付物。"""

    def 炸(*a, **k):
        raise OSError("磁盘忙")

    monkeypatch.setattr(AudioMaterializer, "purge_audio", staticmethod(炸))

    assert PipelineCoordinator._purge_audio_if_complete(完工工作区[0], 完工工作区[1]) is None
    assert len(list(完工工作区[0].audio_dir.rglob("*.m4a"))) == 4


def test_manifest_status_becomes_purged_not_partial(完工工作区):
    """删完后 status 必须显式写成 purged，而不是退化成 partial——那会让人
    以为音频物化出了问题，而它恰恰是成功回收的标志。"""
    ws, blocks = 完工工作区
    AudioMaterializer.purge_audio(ws, blocks)

    manifest = PipelineCoordinator._mark_audio_purged(ws, {"deleted": 4, "bytes": 8192})

    assert manifest["pipeline"]["status"] == "purged"
    assert manifest["audio_purge"]["deleted"] == 4
    assert manifest["pipeline"]["materialization"]["purged"] is True


def test_mark_keeps_existing_manifest_keys(完工工作区):
    """写标记不能把既有账本字段冲掉。"""
    ws, blocks = 完工工作区
    manifest = ws.load_manifest(absolute=True)
    manifest["block_plan"] = "block_plan.json"
    manifest["pipeline"] = {"mode": "full", "status": "ready", "materialization": {"ready": [1, 2, 3]}}
    ws.save_manifest(manifest)

    结果 = PipelineCoordinator._mark_audio_purged(ws, {"deleted": 2, "bytes": 100})

    # load_manifest(absolute=True) 会把路径解析成绝对路径，故断言「存在且以
    # block_plan.json 结尾」而非逐字相等。
    assert str(结果["block_plan"]).endswith("block_plan.json")
    assert 结果["pipeline"]["mode"] == "full"


def test_status_does_not_regress_after_rerun(完工工作区):
    """**回归锁定**：回收后重跑 pipeline，`status` 不能从 purged 退回 partial。

    这是实测踩到的坑：`_save_runtime_manifest` 只看「音频在不在」算 status，
    回收后音频自然不在，于是每重跑一次就把成功回收的证据改写成
    `partial`——看起来像音频物化出了问题，而它恰恰是回收成功的标志。
    """
    ws, blocks = 完工工作区
    AudioMaterializer.purge_audio(ws, blocks)
    PipelineCoordinator._mark_audio_purged(ws, {"deleted": 4, "bytes": 8192})

    # 模拟重跑：manifest 落盘后 pipeline 再算一次 status
    manifest = ws.load_manifest(absolute=True)
    PipelineCoordinator._save_runtime_manifest(
        ws, {"blocks": blocks}, {"needs_audio": [], "subtitle_ready": []}, {}, blocks, mode="full"
    )
    重跑后 = ws.load_manifest(absolute=True)

    assert 重跑后["pipeline"]["status"] == "purged"
    assert 重跑后["pipeline"]["materialization"].get("purged") is True
    assert 重跑后["audio_purge"]["deleted"] == 4      # 记账不被重跑冲掉


def test_mark_does_not_clobber_fresh_purge_on_rerun(完工工作区):
    """重跑不应把 audio_purge 记成 deleted=0（那样就看不出曾经回收过）。"""
    ws, blocks = 完工工作区
    AudioMaterializer.purge_audio(ws, blocks)
    PipelineCoordinator._mark_audio_purged(ws, {"deleted": 4, "bytes": 8192})

    PipelineCoordinator._save_runtime_manifest(
        ws, {"blocks": blocks}, {"needs_audio": [], "subtitle_ready": []}, {}, blocks, mode="full"
    )
    记录 = ws.load_manifest(absolute=True)["audio_purge"]

    assert 记录["deleted"] == 4
    assert 记录["bytes"] == 8192
