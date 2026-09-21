"""阶段一预算与派发阈值（唯一真相来源，全部可配置）。

规则（SKILL.md §1「阶段一派发纪律」的机器侧口径）：

1. **课程总时长 ≤ SERIAL_OK_SECONDS（默认 60 分钟）** → 主 Agent 可串行亲做；
2. **超过** → 必须派发；粒度二选一：
   - 默认 **一集一子智能体**；
   - **打包派发**：集数 ≥ `BATCH_MIN_EPISODES` 且单集预算 ≤ `BATCH_MAX_PREFILL_TOKENS` 时，
     建议 1 个子智能体连做 `BATCH_SIZE` 集（省派发协调开销；代价是单点失败面变大、返修粒度变粗）；
3. **窗口兜底**：实算音频 token > 窗口 × `WINDOW_USAGE_LIMIT`（默认 60%）时，即使不足 60 分钟也必须派发。

**为什么系数必须可配置**：同一集音频在不同宿主里占的 token 差 3 倍——仓库自带口径记录
Gemini 原生音频 ≈ 32 token/秒、OpenAI input_audio ≈ 100 token/秒。1M 窗口下，
1 小时音频 = 115k（Gemini）或 360k（OpenAI），结论完全不同。请按你宿主的实测口径设置：

    BVB_AUDIO_TOKENS_PER_SEC=32      # 默认；OpenAI 口径请设 100
    BVB_CONTEXT_WINDOW_TOKENS=1000000  # 默认 1M
"""

import statistics
from typing import Any, Dict, Iterable, Optional

from . import paths

ENV_AUDIO_TOKENS_PER_SEC = "BVB_AUDIO_TOKENS_PER_SEC"
ENV_CONTEXT_WINDOW_TOKENS = "BVB_CONTEXT_WINDOW_TOKENS"

# Gemini 系原生音频口径（16kHz 单声道）；OpenAI input_audio 约 100
DEFAULT_AUDIO_TOKENS_PER_SEC = 32.0
DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000

# 可串行亲做的总时长上限（分钟 → 秒）
SERIAL_OK_SECONDS = 60 * 60

# 窗口兜底占用比
WINDOW_USAGE_LIMIT = 0.60

# 任务书实测 13.4 KB ≈ 4k token（含语料清单与提示词）
TASK_BOOK_TOKENS = 4000

# 打包派发参数
BATCH_MIN_EPISODES = 15
BATCH_MAX_PREFILL_TOKENS = 40_000
BATCH_SIZE = 5

# 并发建议区间
MIN_WORKERS = 5
MAX_WORKERS = 6


def _env_float(name: str, default: float) -> float:
    """环境变量数值口径与工具层其余部分共用（见 `paths.env_float`）。"""
    return paths.env_float(name, default)


def audio_tokens_per_sec() -> float:
    """每秒钟音频占用的 token 数（宿主相关，可用环境变量覆盖）。"""
    return _env_float(ENV_AUDIO_TOKENS_PER_SEC, DEFAULT_AUDIO_TOKENS_PER_SEC)


def context_window_tokens() -> int:
    """宿主上下文窗口（token）。"""
    return int(_env_float(ENV_CONTEXT_WINDOW_TOKENS, float(DEFAULT_CONTEXT_WINDOW_TOKENS)))


def est_audio_tokens(seconds: float) -> int:
    """单集（或整门课）音频的 token 估算。"""
    return int(max(0.0, float(seconds or 0)) * audio_tokens_per_sec())


def est_episode_prefill_tokens(seconds: float, task_book_tokens: int = TASK_BOOK_TOKENS) -> int:
    """写出该集时需要一次装进上下文的预填量（音频 + 任务书）。"""
    return est_audio_tokens(seconds) + int(task_book_tokens)


def serial_ok(total_seconds: float) -> bool:
    """主 Agent 是否可以串行亲做（总时长与窗口兜底都满足）。"""
    if float(total_seconds or 0) > SERIAL_OK_SECONDS:
        return False
    return est_audio_tokens(total_seconds) <= context_window_tokens() * WINDOW_USAGE_LIMIT


def dispatch_required(total_seconds: float, episodes: int = 0) -> bool:
    """是否必须派发：超过可串行上限，或窗口占用超限。"""
    if episodes <= 1:
        # 单集课程即使很长也没有"跨集调度"可言，按串行处理（窗口兜底仍生效）
        return est_audio_tokens(total_seconds) > context_window_tokens() * WINDOW_USAGE_LIMIT
    return not serial_ok(total_seconds)


def suggest_workers(pending: int) -> int:
    """建议并发槽位（5~6，不超过待派发集数）。"""
    if pending <= 0:
        return 0
    return min(MAX_WORKERS, max(1, pending))


def suggest_batch(prefills: Iterable[float], episodes: int) -> int:
    """建议打包粒度：满足条件时返回 BATCH_SIZE，否则 1（一集一子智能体）。"""
    values = [float(v) for v in prefills]
    if episodes < BATCH_MIN_EPISODES or not values:
        return 1
    median_prefill = statistics.median(values)
    return BATCH_SIZE if median_prefill <= BATCH_MAX_PREFILL_TOKENS else 1


def describe(total_seconds: float = 0.0, episodes: int = 0, pending: int = 0,
             prefills: Optional[Iterable[float]] = None) -> Dict[str, Any]:
    """汇总当前配置与判定结果（供 --summary / 任务书 / 自检展示）。"""
    return {
        "audio_tokens_per_sec": audio_tokens_per_sec(),
        "context_window_tokens": context_window_tokens(),
        "serial_ok_seconds": SERIAL_OK_SECONDS,
        "window_usage_limit": WINDOW_USAGE_LIMIT,
        "total_audio_tokens": est_audio_tokens(total_seconds),
        "serial_ok": serial_ok(total_seconds),
        "dispatch_required": dispatch_required(total_seconds, episodes),
        "suggest_workers": suggest_workers(pending),
        "suggest_batch": suggest_batch(prefills or [], episodes),
    }
