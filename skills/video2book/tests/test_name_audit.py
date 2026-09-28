# -*- coding: utf-8 -*-
"""专名核对：区分「编造」与「音译还原」，只报不拦。

背景：阶段一的实体覆盖率测的是「长文有没有用这块的语料」，不是「用得对不对」。
实测反例——某长文写了 `git.nju.edu.cn`，而讲师原话只是「我的这个 remote 仓库是在 git 的」，
覆盖率 96.6% 全绿，域名却是编的（读者会照着访问一个不存在的地址）。

判定需要语义，所以这里只产出复核清单，并给每项附「逐字稿里最接近的串」：
- 编造：只找得到零散片段（`git.nju.edu.cn` → `git`）；
- 音译还原：找到的就是它的音讹形式（`Agentless` ← `agent list`）→ 属正确还原。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from src.core.name_audit import (  # noqa: E402
    article_proper_nouns,
    audit_block_names,
    looks_like_proper_noun,
    render_report,
)


# 实测的编造样本：长文有、逐字稿没有
INVENTED = (
    "把 change 变成一等公民。我的这个 remote 仓库是在 git.nju.edu.cn 上。"
    "jj（Jujutsu）与 Git 底层完全兼容。"
)
INVENTED_TRANSCRIPT = "我的这个remote仓库是在git的。我们就直接instruct agent。"

# 音译还原：长文写规范名，逐字稿是 ASR 音讹 —— 这是**正确**的还原，不是编造
RESTORED = "这个方法叫 Agentless，还有 Mem0 和 UGround。"
RESTORED_TRANSCRIPT = "这个方法叫 agent list ，还有 mom zero 和 you ground 。"

# 有依据：逐字稿里原样出现过
GROUNDED = "用 Git 做版本控制，配合 XGrammar 约束。"
GROUNDED_TRANSCRIPT = "用 git 做版本控制，配合 XGrammar 约束。"


@pytest.mark.parametrize("token, expected", [
    ("git.nju.edu.cn", True),      # 域名
    ("src/main.py", True),         # 路径
    ("Jujutsu", True),             # 首字母大写就够（曾因要求"首字母之后还有大写"而漏报)
    ("Mem0", True),
    ("vLLM", True),
    ("agent", False),              # 纯小写通用词
    ("token", False),
    ("2024", False),               # 纯数字
    ("OK", False),                 # 太短
])
def test_proper_noun_filter(token, expected):
    """宽进：宁可多列，也不漏掉编造的域名；但纯数字/纯小写通用词不进清单。"""
    assert looks_like_proper_noun(token) is expected, token


def test_article_proper_nouns_keeps_domain_shape():
    """域名必须整体抽出。`quality_gate._TOKEN_RE` 不含点与斜杠，会把它切成 4 个词、
    丢掉"这是个域名"的形态，所以本模块自己抽词。"""
    tokens = article_proper_nouns("仓库在 git.nju.edu.cn 上。")
    assert "git.nju.edu.cn" in tokens, dict(tokens)


def test_invented_domain_is_reported_with_weak_evidence():
    """编造域名必须报出，且「最接近的串」只给出零散片段——那正是复核者需要的证据。"""
    result = audit_block_names(INVENTED, INVENTED_TRANSCRIPT)
    by_token = {item["token"]: item for item in result["ungrounded"]}
    assert "git.nju.edu.cn" in by_token, result
    assert by_token["git.nju.edu.cn"]["nearest"] == "git", by_token["git.nju.edu.cn"]
    # 凭空安上的产品名：连近似串都没有，属最强信号
    assert "Jujutsu" in by_token and by_token["Jujutsu"]["nearest"] == "", by_token


def test_restored_transliteration_is_reported_with_its_phonetic_source():
    """音译还原也要报——但必须带出音讹来源，让人一眼判定"属正确还原"。

    这类若被机械拦掉，会把一批正确的还原全部误杀，所以判据只报不拦。
    """
    result = audit_block_names(RESTORED, RESTORED_TRANSCRIPT)
    by_token = {item["token"]: item for item in result["ungrounded"]}
    assert by_token["Agentless"]["nearest"] == "agent", by_token
    assert by_token["UGround"]["nearest"] == "ground", by_token
    assert by_token["Mem0"]["nearest"].startswith("mom"), by_token


def test_grounded_names_are_not_reported():
    """逐字稿里有依据的专名一律不列，否则清单会被正常产物淹没。"""
    result = audit_block_names(GROUNDED, GROUNDED_TRANSCRIPT)
    tokens = {item["token"] for item in result["ungrounded"]}
    assert "Git" not in tokens and "XGrammar" not in tokens, result


def test_items_without_any_near_match_sort_first():
    """完全没有近似串的项排最前：它们是"连读音都对不上"的最强可疑信号。"""
    result = audit_block_names(INVENTED, INVENTED_TRANSCRIPT)
    assert result["ungrounded"][0]["nearest"] == "", result["ungrounded"]


def test_render_report_states_it_is_report_only():
    """产出物必须自解释「只报不拦」与两种情形的分辨方法，否则复核者无从下手。"""
    text = render_report("测试课程", audit_block_names(INVENTED, INVENTED_TRANSCRIPT)["ungrounded"]
                         and [{"block_id": 1, "title": "T", "article": "a.md",
                               "transcript": "t.md", **audit_block_names(INVENTED, INVENTED_TRANSCRIPT)}])
    assert "只报不拦" in text and "音译还原" in text
    assert "git.nju.edu.cn" in text


def test_render_report_handles_clean_course():
    """全课程无待复核项时给出明确结论，不留空表。"""
    text = render_report("干净课程", [])
    assert "无需复核" in text
