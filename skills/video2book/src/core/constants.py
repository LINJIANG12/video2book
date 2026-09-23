#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""video2book 系统全局常量与单一事实源（SSOT）。

集中定义并发建议数、切片时长、超时阈值等核心常量，
避免在任务书、CLI、文档与自检脚本中出现分散硬编码与口径漂移。
"""

# 阶段一与阶段二并发建议角色数
DEFAULT_TRANSCRIBE_WORKERS: int = 3
DEFAULT_ARTICLE_WORKERS: int = 5
DEFAULT_NOTE_WORKERS: int = 3

# 音频切片与合并分块（分钟）
DEFAULT_BLOCK_MINUTES: float = 50.0
DEFAULT_CHUNK_MINUTES: float = 30.0


# MCP 服务参数推荐与默认值（与 omni-media 推荐配置对齐）
DEFAULT_MCP_CONCURRENCY: int = 5
DEFAULT_MAX_PAYLOAD_MB: int = 35
DEFAULT_SLICE_MINUTES: int = 30

# 子进程硬超时（秒）
DEFAULT_SUBPROCESS_TIMEOUT: int = 60
LONG_SUBPROCESS_TIMEOUT: int = 600
