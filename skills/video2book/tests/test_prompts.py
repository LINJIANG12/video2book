# -*- coding: utf-8 -*-
"""长文提示词风格契约（`src/prompts.py` + 任务书出口）。

防的是这几类真实故障（原 `scripts/selfcheck.py::check_article_prompt_types`）：

1. **工具层替用户猜风格**：用户没确认就用某一版开写，交付物不是他确认过的写法；
   更糟的是把任务书先落了盘，事后无法分辨哪一版被用过 → 未确认/未命中必须立刻终止（CLI exit 4）；
2. **半成品风格被当成已交付**：四种只登记、未定稿的形态若挂上提示词，会被正常派发；
3. **终止提示没有菜单**：用户只看到「类型不对」，不知道有哪些可选、该怎么传参；
4. **选错版本**：任务书抬头写了 A 版、注入的却是 B 版提示词（含回归到已被否决的扩张型写法）；
5. **块级链路被改回「让写作子智能体自己听音」**：任务书必须把块级逐字稿钉成唯一事实来源，
   并明确「逐字稿未就绪就停下」，否则会上演补写、编造与重复听音。

提示词正文的措辞本身属于文案（`selfcheck` 里那些「某句话必须在/不在」的断言），不在此迁移；
这里断言的是**行为**：谁能用、谁必须终止、终止时给出什么、以及任务书里注入了哪一版。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_text
from src.core.taskbook import export_block_article_task
from src.prompts import (
    ARTICLE_LEARNING_PROMPT,
    ARTICLE_LEGACY_PROMPT,
    ARTICLE_PROMPT_TYPES,
    IMPLEMENTED_ARTICLE_TYPES,
    ArticlePromptTypeError,
    render_article_prompt_menu,
    resolve_article_prompt,
)

COURSE_TITLE = "测试课程"
# 四种「只登记、未定稿」的视频形态：命中即终止，不得被派发。
UNIMPLEMENTED_KINDS = ("consulting", "interview", "review", "livestream")
# 文档承诺可由用户确认的两种风格（键 / 中文名都要认）。
ACCEPTED_FORMS = (
    ("learning", "learning"),
    ("学习", "learning"),
    ("legacy", "legacy"),
    ("旧版", "legacy"),
)


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

@pytest.fixture
def style_workspace(make_workspace):
    return make_workspace("style_gate")


@pytest.fixture
def task_block(blocks_factory):
    return blocks_factory(
        1, [1, 2], "绪论与数制", span="P01-P02", duration_min=46.0,
        audio="audio/_blocks/测试课_01_绪论与数制(P01-P02).m4a",
    )


@pytest.fixture
def block_transcript(style_workspace):
    return write_text(
        style_workspace.subtitles_dir / "BLK01_P01-P02_逐字稿.md",
        "# 逐字稿\n\n[00:00:00] 开场白。\n",
    )


def _export(ws, block, transcript_file, article_type: str) -> Path:
    return export_block_article_task(
        ws, block, transcript_file, course_title=COURSE_TITLE,
        article_type=article_type, page_titles={1: "绪论", 2: "数制"},
    )


def _literal_blocks(prompt: str) -> list:
    """提示词里**不含占位符**的整段。

    任务书会把 `{title}` / `{part_title}` / `{content}` 替换掉，其余条款逐字保留；
    于是「注入了哪一版」可以用逐字比对来判定，而不必把某一句话钉死在用例里。
    """
    return [b.strip() for b in prompt.split("\n\n") if b.strip() and "{" not in b]


def _exclusive_block(style_key: str) -> str:
    """取该风格独有、另一版没有的整段：用来判定「注入的确实是这一版」。"""
    own = set(_literal_blocks(resolve_article_prompt(style_key)["prompt"]))
    other_keys = [k for k in IMPLEMENTED_ARTICLE_TYPES if k != style_key]
    others = set()
    for key in other_keys:
        others |= set(_literal_blocks(resolve_article_prompt(key)["prompt"]))
    exclusive = sorted(own - others, key=len, reverse=True)
    assert exclusive, f"风格 {style_key} 没有任何独有条款，无法判定注入的是哪一版"
    return exclusive[0]


# ---------------------------------------------------------------------------
# 风格矩阵：谁能用、谁只登记
# ---------------------------------------------------------------------------

def test_only_learning_and_legacy_ship_prompts():
    """只有定稿的两版可以派发；给未定稿形态挂上提示词，等于是替用户签收了没背书的写法。"""
    assert IMPLEMENTED_ARTICLE_TYPES == ("learning", "legacy")


def test_learning_is_the_recommended_style():
    """推荐标记决定菜单里用户第一眼看到哪版；标错会把用户默认引到非推荐版。"""
    assert ARTICLE_PROMPT_TYPES["learning"].get("recommended") is True


@pytest.mark.parametrize("kind", sorted(ARTICLE_PROMPT_TYPES))
def test_every_registered_style_has_label_and_signals(kind: str):
    """每种登记形态都必须有中文名与适用信号：否则用户无法据以判断自己的片子属于哪一类。"""
    meta = ARTICLE_PROMPT_TYPES[kind]
    assert str(meta.get("label") or "").strip(), f"{kind} 缺少中文名"
    signals = meta.get("signals") or []
    assert signals, f"{kind} 缺少适用信号（用户无法据以选择）"
    assert all(str(s).strip() for s in signals), f"{kind} 的适用信号里有空条目"


@pytest.mark.parametrize("kind", UNIMPLEMENTED_KINDS)
def test_unimplemented_kind_is_registered_without_prompt(kind: str):
    """四种未定稿形态必须留在矩阵里（好让用户看到菜单）但**没有**提示词。"""
    assert kind in ARTICLE_PROMPT_TYPES
    assert not ARTICLE_PROMPT_TYPES[kind].get("prompt")
    assert kind not in IMPLEMENTED_ARTICLE_TYPES


# ---------------------------------------------------------------------------
# 解析：命中 / 未指定 / 未命中 / 未定稿
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", ACCEPTED_FORMS)
def test_resolve_accepts_key_and_chinese_label(raw: str, expected: str):
    """键与中文名都要认（用户会照菜单念中文）；解析结果须带可用提示词。"""
    resolved = resolve_article_prompt(raw)
    assert resolved["key"] == expected
    assert resolved["label"].strip()
    assert resolved["prompt"].strip()


@pytest.mark.parametrize("raw", ["", "   "])
def test_unspecified_style_terminates_with_menu(raw: str):
    """不指定风格即终止——工具层不许替用户挑一版，且必须把菜单摆出来让他选。"""
    with pytest.raises(ArticlePromptTypeError) as excinfo:
        resolve_article_prompt(raw)
    assert "菜单" in excinfo.value.report


@pytest.mark.parametrize("raw", ["网课", "learn", "乱写"])
def test_unknown_style_terminates_with_menu(raw: str):
    """拼错或不属于任何预设的形态必须终止，不得降级到默认版继续跑。"""
    with pytest.raises(ArticlePromptTypeError) as excinfo:
        resolve_article_prompt(raw)
    assert "菜单" in excinfo.value.report


@pytest.mark.parametrize("kind", UNIMPLEMENTED_KINDS)
def test_unimplemented_kind_terminates_with_menu(kind: str):
    """命中「已登记但提示词未提供」的形态：终止任务，并在报告里说明当前只提供哪两版。"""
    with pytest.raises(ArticlePromptTypeError) as excinfo:
        resolve_article_prompt(kind)
    err = excinfo.value
    assert err.type_key == kind
    assert "菜单" in err.report


# ---------------------------------------------------------------------------
# 风格菜单
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(ARTICLE_PROMPT_TYPES))
def test_menu_lists_every_style_with_its_flag(kind: str):
    """菜单必须给出每个风格的可复制参数名，否则用户知道有这版却传不出参数。"""
    assert f"--article-type {kind}" in render_article_prompt_menu()


def test_menu_marks_recommended_style():
    """菜单必须标出推荐项：否则用户在最关键的一次选择上没有任何依据。"""
    assert "推荐" in render_article_prompt_menu()


def test_menu_gives_copy_paste_usage():
    """菜单末尾要有一条可直接复制的完整命令，省掉用户翻文档拼参数。"""
    assert "--all --article-type" in render_article_prompt_menu()


# ---------------------------------------------------------------------------
# 任务书出口：未命中不落盘
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "consulting", "乱写"])
def test_rejected_style_writes_no_taskbook(style_workspace, task_block, block_transcript, bad: str):
    """风格未命中时连任务书都不许落盘：半份任务书留在磁盘上，事后无法分辨它属于哪一版。"""
    with pytest.raises(ArticlePromptTypeError):
        _export(style_workspace, task_block, block_transcript, bad)
    assert not list(style_workspace.articles_dir.glob("*_TASK.md")), "风格未命中却落了任务书"


# ---------------------------------------------------------------------------
# 任务书出口：抬头与提示词注入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("style_key", IMPLEMENTED_ARTICLE_TYPES)
def test_taskbook_header_records_selected_style(style_workspace, task_block, block_transcript, style_key: str):
    """抬头必须写明所选风格（中文名 + 键）：否则事后无从判断这份成品是按哪版写的。"""
    label = ARTICLE_PROMPT_TYPES[style_key]["label"]
    task = _export(style_workspace, task_block, block_transcript, style_key)
    text = task.read_text(encoding="utf-8")
    assert f"长文风格：{label}（{style_key}）" in text


def test_taskbook_is_named_by_block_and_title(style_workspace, task_block, block_transcript):
    """任务书主名由 `模块XX_<块标题>` 推出：标题漂移会让队列/对账找不到对应成品。"""
    task = _export(style_workspace, task_block, block_transcript, "learning")
    assert task.name == "模块01_绪论与数制_TASK.md"


@pytest.mark.parametrize("style_key", IMPLEMENTED_ARTICLE_TYPES)
def test_taskbook_injects_selected_prompt_verbatim(style_workspace, task_block, block_transcript, style_key: str):
    """所选风格的提示词条款必须**逐字**出现在任务书里（占位符以外的部分不许被改写）。"""
    prompt = resolve_article_prompt(style_key)["prompt"]
    text = _export(style_workspace, task_block, block_transcript, style_key).read_text(encoding="utf-8")
    blocks = _literal_blocks(prompt)
    assert blocks, "提示词里没有可逐字比对的条款，断言会变成空转"
    missing = [b for b in blocks if b not in text]
    assert not missing, f"任务书缺少 {style_key} 版提示词条款: {missing[:1]}"


def test_taskbook_does_not_inject_the_other_style(style_workspace, task_block, block_transcript):
    """选了「学习」就不能混进另一版独有条款——混版等于把两种写法缝在一起，口径失控。"""
    other_only = _exclusive_block("legacy")
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert other_only not in text


# ---------------------------------------------------------------------------
# 任务书出口：块级逐字稿链路
# ---------------------------------------------------------------------------

def test_taskbook_names_transcript_as_only_source_of_truth(style_workspace, task_block, block_transcript):
    """必须把块级逐字稿钉成唯一事实来源：否则写作子智能体会退回「凭常识补齐」。"""
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert "唯一事实来源" in text


def test_taskbook_stops_when_transcript_missing(style_workspace, task_block, block_transcript):
    """逐字稿缺失时必须让写作子智能体**停下并回报**，而不是去别处找音频补听。"""
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert "逐字稿未就绪" in text


def test_taskbook_flags_missing_transcript_state(style_workspace, task_block, block_transcript):
    """语料状态要如实反映磁盘：没转录就写「未就绪」，转录了就写「已就绪」。"""
    ready = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert "语料状态：逐字稿已就绪" in ready

    pending = _export(style_workspace, task_block, None, "learning").read_text(encoding="utf-8")
    assert "语料状态：逐字稿**未就绪**" in pending


def test_taskbook_does_not_ask_writer_to_listen_again(style_workspace, task_block, block_transcript):
    """块音频只作备查：让写作角色重听一遍，等于把转录成本再付一次且引入新失真。"""
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert "所属块音频（备查，不必再听）" in text


@pytest.mark.parametrize("episode_label", ["P01 绪论", "P02 数制"])
def test_taskbook_lists_covered_episodes(style_workspace, task_block, block_transcript, episode_label: str):
    """任务书要列出块覆盖的分集与名称：写作角色据此核对覆盖面，漏集会静默产出半篇。"""
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert episode_label in text


@pytest.mark.parametrize("removed", ["待听音切片清单", "read_audio", "本集逐字稿"])
def test_taskbook_does_not_reintroduce_old_audio_chain(style_workspace, task_block, block_transcript, removed: str):
    """旧链路产物（听音切片清单 / 让写作角色调听音工具 / 单集逐字稿口径）不得回流。"""
    text = _export(style_workspace, task_block, block_transcript, "learning").read_text(encoding="utf-8")
    assert removed not in text


# ---------------------------------------------------------------------------
# 端到端：CLI 未确认即终止（exit 4）
# ---------------------------------------------------------------------------

def test_cli_pipeline_without_confirmed_style_exits_4(run_cli, tmp_path: Path):
    """未确认风格必须在**动手之前**以 exit 4 终止，并给出可复制用法。

    这是「工具层不猜、不降级」的对外承诺；一旦越过这一步就会开始建工作区、拉音频，
    所以终止必须发生在落任何产物之前。
    """
    products = tmp_path / "products"
    products.mkdir()
    res = run_cli(
        "pipeline", "https://www.bilibili.com/video/BV1xx411c7mD", "--all",
        "--base-dir", str(products),
        "--sessdata", "test-sessdata",
        "--douyin-cookie", "test-douyin-cookie",
    )
    assert res.code == 4, res.norm
    assert "--article-type learning" in res.norm
    assert "--article-type legacy" in res.norm
    assert list(products.iterdir()) == [], "未确认风格却已经开始建工作区/落产物"


def test_learning_and_legacy_prompts_are_distinct():
    """两版提示词必须确实不同：否则「选择」只是摆设，用户以为换了写法实际没换。"""
    assert ARTICLE_LEARNING_PROMPT != ARTICLE_LEGACY_PROMPT
