# -*- coding: utf-8 -*-
"""块级任务书导出、阶段一门禁与重复分集复用。

迁自 `scripts/selfcheck.py` 的三段行为断言（`check_task_file_export_end_to_end` /
`check_stage1_gate_ignores_task_files` / `check_dedup_reuses_without_subtitles`）。
三条契约各自防一种实测故障：

1. **任务书导出门禁**：逐字稿未就绪时任务书必须照样导出、并把「未就绪」写进正文。
   否则派发出去的任务书是一条死路——执行者拿到空路径，不是卡死就是凭空编内容。
2. **阶段一门禁以块为单位**：任务书（`模块XX_*_TASK.md`）与模块长文同目录、同前缀，
   体积同样远超 1000 字节门禁。按名字通配找长文时若不先排除它，阶段一会把
   「一个字都还没写」的工作区判成竣工。
3. **重复分集只复用分集逐字稿**：块级链路里长文按块产出，没有「一集一篇」可复用；
   同目录同前缀的转录任务书绝不能被当成复用源拷过去。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ── 脚手架复用：conftest 里的纯函数不是 fixture，需要自己导入。
# （conftest.py 允许作为模块导入，pytest 会把 tests/ 目录放进 sys.path；
#   这里再显式补一次，保证单独运行本文件时也能导入。）
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import pad_to, write_blocks  # noqa: E402

# ── queue_tracker 住在 scripts/（不在包的 pythonpath 上），按 selfcheck 的同一方式接入。
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from queue_tracker import scan_status  # noqa: E402

BLOCK_TITLE = "绪论与数制"
BLOCK_SPAN = "P01-P02"
ARTICLE_NAME = "模块01_绪论与数制_精读长文.md"
TASK_NAME = "模块01_绪论与数制_TASK.md"


def _block(blocks_factory):
    return blocks_factory(1, [1, 2], BLOCK_TITLE, span=BLOCK_SPAN)


def _gate_ws(make_parts, blocks_factory, *, task: str = "", article: str | None = None):
    """造一个「已装块」的工作区；可按需塞入任务书 / 模块长文。"""
    ws = make_parts((1, 2))
    write_blocks(ws, [_block(blocks_factory)])
    if task:
        (ws.articles_dir / TASK_NAME).write_text(task, encoding="utf-8")
    if article is not None:
        (ws.articles_dir / ARTICLE_NAME).write_text(article, encoding="utf-8")
    return ws


# ---------------------------------------------------------------------------
# 1) 任务书导出：未就绪也导出、就绪写明、长文宽容定位、归并采纳与拒绝
# ---------------------------------------------------------------------------

def test_export_task_without_transcript_still_writes_task(make_workspace, blocks_factory):
    """逐字稿缺失也要导出任务书——不导出就等于把这一块静默丢出流水线。"""
    from src.core.taskbook import export_block_article_task

    ws = make_workspace("自检_文件导出")
    task = export_block_article_task(
        ws, _block(blocks_factory), None, course_title="测试课程", article_type="学习"
    )
    assert task.name == TASK_NAME, f"模块长文任务书命名漂移: {task.name}"


def test_export_task_without_transcript_marks_not_ready(make_workspace, blocks_factory):
    """未就绪必须写在任务书上：执行者凭这一行才会停下回报，而不是编内容。"""
    from src.core.taskbook import export_block_article_task

    ws = make_workspace("自检_未就绪态")
    task = export_block_article_task(
        ws, _block(blocks_factory), None, course_title="测试课程", article_type="学习"
    )
    assert "**未就绪**" in task.read_text(encoding="utf-8"), "未就绪态未在任务书上写明"


def test_export_task_with_transcript_marks_ready(make_workspace, blocks_factory):
    """逐字稿落盘后任务书必须改口成「已就绪」，否则执行者永远不敢动笔。"""
    from src.core.taskbook import export_block_article_task

    ws = make_workspace("自检_就绪态")
    transcript = ws.subtitles_dir / "BLK01_P01-P02_逐字稿.md"
    transcript.write_text("[00:00:00] 语料。" + "\n" * 60, encoding="utf-8")
    task = export_block_article_task(
        ws, _block(blocks_factory), transcript, course_title="测试课程", article_type="学习"
    )
    assert "已就绪" in task.read_text(encoding="utf-8"), "就绪态未在任务书上写明"


def test_find_module_article_is_none_when_no_article(make_workspace, blocks_factory):
    """尚无长文时必须报「未就绪」：报空会让人以为这一块已经交过稿。"""
    from src.core.workspace import find_module_article

    ws = make_workspace("自检_无长文")
    assert find_module_article(ws.articles_dir, _block(blocks_factory)) is None


def test_find_module_article_ignores_task_file(make_workspace, blocks_factory):
    """任务书与长文同目录同前缀、体积同样达阈值：不排除它，空壳工作区就会被判已交稿。"""
    from src.core.workspace import find_module_article

    ws = make_workspace("自检_长文定位")
    block = _block(blocks_factory)
    (ws.articles_dir / TASK_NAME).write_text(pad_to("提示词"), encoding="utf-8")
    assert find_module_article(ws.articles_dir, block) is None, "任务书被当成了模块长文"


def test_find_module_article_finds_real_article(make_workspace, blocks_factory):
    """达标长文必须被精确定位到，且落点就是任务书声明的目标路径。"""
    from src.core.workspace import find_module_article, module_article_path

    ws = make_workspace("自检_长文命中")
    block = _block(blocks_factory)
    target = module_article_path(ws.articles_dir, block)
    target.write_text("正文" * 400, encoding="utf-8")
    assert find_module_article(ws.articles_dir, block) == target, "模块长文未被宽容定位到"


def test_find_module_article_tolerates_title_drift(make_workspace, blocks_factory):
    """块标题被 Agent 微调过（加了几个字）也必须找得到，否则会重复返工同一块。"""
    from src.core.workspace import find_module_article

    ws = make_workspace("自检_标题漂移")
    drifted = ws.articles_dir / "模块01_绪论与数制（含编码）_精读长文.md"
    drifted.write_text("正文" * 400, encoding="utf-8")
    found = find_module_article(ws.articles_dir, _block(blocks_factory))
    assert found is not None and found.name.startswith("模块01_"), f"宽容定位失败: {found}"


def test_notes_without_plan_returns_none(make_parts, blocks_factory):
    """没有归并规划时返回 None：工具层不做本地兜底归并，语义归并只能由 Agent 给。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    notes = P.notes([_block(blocks_factory)], ws.load_parts(),
                    course_title="测试课程", ws=ws)
    assert notes is None, "无归并时应返回 None 而非本地兜底结果"


def test_notes_without_plan_exports_merge_task_book(make_parts, blocks_factory):
    """返回 None 的同时必须导出归并任务书，否则这一轮没有任何人能拿到派发物。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    P.notes([_block(blocks_factory)], ws.load_parts(), course_title="测试课程", ws=ws)
    assert (ws.root_dir / "note_plan_TASK.md").exists(), "NOTE_PLAN_TASK 未落盘"


def test_notes_never_writes_note_plan_json(make_parts, blocks_factory):
    """`note_plan.json` 是 Agent 的语义产物，工具层不得代写（代写等于伪造归并结论）。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    P.notes([_block(blocks_factory)], ws.load_parts(), course_title="测试课程", ws=ws)
    assert not (ws.root_dir / "note_plan.json").exists(), "工具层不得创建 note_plan.json"


def test_notes_does_not_emit_topic_plan_task(make_parts, blocks_factory):
    """第一趟规划任务书（topic_plan_TASK）不得回流：教材已不再读模块规划。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    P.notes([_block(blocks_factory)], ws.load_parts(), course_title="测试课程", ws=ws)
    assert not (ws.root_dir / "topic_plan_TASK.md").exists(), "第一趟规划任务书不得回流"


def test_notes_adopts_valid_plan(make_parts, blocks_factory):
    """Agent 写出的合法归并必须被采纳，否则归并永远只是摆设。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    (ws.root_dir / "note_plan.json").write_text(json.dumps(
        [{"note_id": 1, "note_title": BLOCK_TITLE, "blocks": [1], "core_theme": "x"}],
        ensure_ascii=False), encoding="utf-8")
    notes = P.notes([_block(blocks_factory)], ws.load_parts(),
                    course_title="测试课程", ws=ws)
    assert notes is not None and len(notes) == 1, "合法归并未被采纳"


def test_notes_rejects_plan_claiming_no_block(make_parts, blocks_factory):
    """认领 0 个块的归并非法：放过它会让整批块静默消失。"""
    from src.generator.topic_planner import SemanticTopicPlanner as P

    ws = make_parts((1, 2))
    (ws.root_dir / "note_plan.json").write_text(json.dumps(
        [{"note_id": 1, "note_title": "残缺", "blocks": []}], ensure_ascii=False),
        encoding="utf-8")
    notes = P.notes([_block(blocks_factory)], ws.load_parts(),
                    course_title="测试课程", ws=ws)
    assert notes is None, "未认领任何块的非法归并未被拒绝"


# ---------------------------------------------------------------------------
# 2) 阶段一门禁：以块为单位、不误认任务书、空壳单独报、达标才算完成
# ---------------------------------------------------------------------------

def test_scan_status_without_blocks_has_no_stage1_unit(make_parts):
    """无块清单就不做阶段一判定：旧链路的逐集长文产物已不存在，照它判会永远未完成。"""
    ws = make_parts((1, 2))
    (ws.articles_dir / TASK_NAME).write_text(pad_to("提示词"), encoding="utf-8")
    st = scan_status(ws.root_dir)
    assert st["stage1_unit"] == "none", f"无块清单却给出了判定单位: {st['stage1_unit']}"
    assert st["blocks_total"] == 0 and not st["is_stage1_complete"], "无块清单时门禁不得放行"


def test_scan_status_does_not_count_task_file_as_article(make_parts, blocks_factory):
    """任务书体积远超 1000 字节门禁，必须被排除，否则阶段一在零产出的工作区上报竣工。"""
    ws = _gate_ws(make_parts, blocks_factory, task=pad_to("提示词"))
    st = scan_status(ws.root_dir)
    assert st["blocks_total"] == 1 and st["blocks_done"] == [], f"任务书被误判为长文: {st}"
    assert not st["is_stage1_complete"], "尚无模块长文时阶段一门禁不得放行"


def test_scan_status_does_not_list_task_file_as_short_article(make_parts, blocks_factory):
    """任务书也不该进「过短长文」异常清单：那会把派发物报成需要返工的产物。"""
    ws = _gate_ws(make_parts, blocks_factory, task=pad_to("提示词"))
    assert 1 not in scan_status(ws.root_dir)["invalid_articles"], "任务书被当成了过短长文"


def test_scan_status_lists_shell_article_as_invalid(make_parts, blocks_factory):
    """「写了但不够 1000 字节」必须单列成异常：否则表现为「什么都没写」，无人可查。"""
    ws = _gate_ws(make_parts, blocks_factory, article="短")
    st = scan_status(ws.root_dir)
    assert 1 in st["invalid_articles"], "空壳模块长文未进异常清单"
    assert not st["is_stage1_complete"], "空壳长文不得放行"


def test_scan_status_counts_real_article_as_done(make_parts, blocks_factory):
    """达标长文落盘后才计入完成（一块一篇的放行判据）。"""
    ws = _gate_ws(make_parts, blocks_factory, article="正文" * 400)
    st = scan_status(ws.root_dir)
    assert len(st["blocks_done"]) == 1 and st["is_stage1_complete"], f"达标长文未被计入: {st}"


def test_scan_status_marks_covered_episodes_done(make_parts, blocks_factory):
    """集号是块覆盖的派生视图：块完成时其覆盖的集号必须同步标记完成。"""
    ws = _gate_ws(make_parts, blocks_factory, article="正文" * 400)
    st = scan_status(ws.root_dir)
    assert st["completed_count"] == 2 and st["pending_count"] == 0, \
        f"块覆盖的集号未被标记完成: {st['completed_count']}/{st['pending_count']}"


def test_check_stage1_strict_fails_when_task_file_is_the_only_output(
    run_cli, make_parts, blocks_factory
):
    """端到端：只有任务书、没有模块长文时，`check --stage1 --strict` 必须非零退出。"""
    ws = _gate_ws(make_parts, blocks_factory, task=pad_to("提示词"))
    result = run_cli("check", "--stage1", "--strict", "--dir", str(ws.root_dir))
    assert result.code == 1, f"任务书被当成长文放行了:\n{result.norm}"
    assert "[FAIL]" in result.norm, "未给出失败原因"


def test_check_stage1_default_is_hint_level(run_cli, make_parts, blocks_factory):
    """默认提示级：不加 --strict 时未就绪不得让整条命令失败（历史工作区不该被卡死）。"""
    ws = _gate_ws(make_parts, blocks_factory, task=pad_to("提示词"))
    result = run_cli("check", "--stage1", "--dir", str(ws.root_dir))
    assert result.code == 0, f"提示级却返回了非零码:\n{result.norm}"


def test_check_stage1_json_reports_no_article(run_cli, make_parts, blocks_factory):
    """JSON 口径里任务书同样不算长文：`no_article` 计数是脚本化巡检的判据。"""
    ws = _gate_ws(make_parts, blocks_factory, task=pad_to("提示词"))
    result = run_cli("check", "--stage1", "--json", "--dir", str(ws.root_dir))
    assert result.code == 0, result.norm
    report = json.loads(result.out)["reports"][0]
    assert report["total"] == 1 and report["no_article"] == 1, f"口径异常: {report}"


# ---------------------------------------------------------------------------
# 4) 依据级覆盖率：中文术语层 + 双层判据 + 口水词剔除
# ---------------------------------------------------------------------------
# 这三条契约各防一种实测故障：
# 1. **中文长文被误杀**：逐字稿是中文口语，讲师不念英文时，忠实却通篇中文
#    表述的长文抽不到任何实体 → 覆盖率 0 → 被判「没覆盖语料」。门禁反过来
#    惩罚了正确写法。
# 2. **口水词污染分母**：`ppt`/`sorry` 占着分母，逼写作者凑词进正文换覆盖率。
# 3. **碎片化**：中文若用滑窗切 n-gram，「函数依赖」会被切成「函数/数依/依赖」
#    重复计数，实体数暴涨、忠实文覆盖率被自己的碎片淹没。


def test_cn_entities_keep_terminology_and_drop_filler():
    """中文层要留下真术语、滤掉口水词；英文层不得把 ppt/sorry 算进分母。"""
    from src.core.quality_gate import extract_cn_entities, extract_entities

    # 显式 min_freq=2：这条测的是「抽得出真术语、滤得掉口水词」，
    # 不是默认门槛（默认 3 是给真实长逐字稿用的，短样本够不上）。
    cn = extract_cn_entities(
        "范式讲了范式，范式又回到范式。完全函数依赖也讲完全函数依赖。", min_freq=2
    )
    assert "范式" in cn, f"高频中文术语未被抽出: {list(cn)}"
    assert any("完全函数依赖" in t for t in cn), f"多字术语骨架丢失: {list(cn)}"
    # 停用字被剥掉：这些串不该以原样留在实体里
    assert not any(t in ("这个", "所以这个") for t in cn), f"口水串进了实体: {list(cn)}"

    en = extract_entities("ppt ppt sorry sorry select select where where", min_freq=2)
    assert "select" in en and "where" in en, f"真英文术语丢了: {list(en)}"
    assert "ppt" not in en and "sorry" not in en, f"口水词进了分母: {list(en)}"


def test_cn_entities_normalize_traditional_to_simplified():
    """转录源繁简混杂：不归一则「這個/这个」算两个词，长文照写也命中不了。"""
    from src.core.quality_gate import extract_cn_entities, normalize_script

    assert normalize_script("這個資料庫系統") == "这个资料库系统"
    ents = extract_cn_entities("資料庫系統講資料庫系統，資料庫系統很重要。")
    assert any("资料库系统" in t for t in ents), f"繁体术语未归一: {list(ents)}"


def _grounding_entry(make_parts, blocks_factory, *, transcript: str, article: str,
                     span: str = "P01-P02"):
    """落一份「块清单 + 块级逐字稿 + 模块长文」，跑一次依据级判定并返回结果。

    走真实 TaskWorkspace（而不是临时目录拼的 SimpleNamespace）：判定函数内部
    走 find_module_article / TaskWorkspace.block_path 定位，假的 ws 对象会让它
    找不到文章、返回 no_article，测的就不是覆盖率逻辑了。
    长文用 pad_to 补到产品阈值以上——find_module_article 按字节数判「已产出」，
    样本太短会被当成没交稿。
    """
    from src.core.quality_gate import check_grounding_block
    from src.core.workspace import TaskWorkspace, module_article_path

    ws = make_parts((1, 2))
    block = blocks_factory(1, [1, 2], "关系模型", span=span)
    write_blocks(ws, [block])
    ws.subtitles_dir.mkdir(parents=True, exist_ok=True)
    TaskWorkspace.block_path(ws, block).write_text(transcript, encoding="utf-8")
    ws.articles_dir.mkdir(parents=True, exist_ok=True)
    module_article_path(ws.articles_dir, block).write_text(
        pad_to(article), encoding="utf-8"
    )
    return check_grounding_block(ws, block, 2, 0.5)


def test_stage1_gate_accepts_faithful_chinese_article(make_parts, blocks_factory):
    """忠实但通篇中文表述的长文必须放行——这正是原门禁误杀的那一类。"""
    transcript = (
        "我们讲关系数据库的关系模型。关系的码就是候选码，候选码能唯一确定一个元组。"
        "关系的属性也叫字段，字段来自关系的域。范式有第一范式、第二范式、第三范式。"
        "完全函数依赖是第一范式到第二范式之间的关系，部分函数依赖决定第二范式。"
    ) * 6
    # 忠实复述讲法，但把英文例子改写成中文——英文层必然低，中文层应当救回
    article = (
        "关系数据库的核心是关系模型。关系里的码叫候选码，它能唯一确定一个元组。"
        "关系中的属性也叫字段，字段取自关系的域。范式体系包含第一范式、第二范式和第三范式。"
        "完全函数依赖与部分函数依赖是判断第二范式的关键。"
    ) * 6

    entry = _grounding_entry(
        make_parts, blocks_factory, transcript=transcript, article=article
    )
    assert entry["cn_entities"] > 0, f"中文层没有抽到任何实体: {entry}"
    assert entry["status"] == "ok", f"忠实中文长文被误杀: {entry}"


def test_stage1_gate_rejects_off_topic_article(make_parts, blocks_factory):
    """脱稿另写的文章：两层覆盖都低，必须拦下（中文层不是万能放行器）。"""
    transcript = (
        "今天讲查询优化。先算清楚代价，再谈快。选择运算先做，投影运算后做。"
        "连接有嵌套循环连接和排序合并连接，等值连接和不等值连接。代价估算看块数。"
    ) * 6
    article = "今天天气不错，我们去公园散步吧，顺便买点吃的喝的。"

    entry = _grounding_entry(
        make_parts, blocks_factory, transcript=transcript, article=article
    )
    assert entry["status"] == "low_coverage", f"脱稿文被放行了: {entry}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
