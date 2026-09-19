"""B 站网页端接口的公共约定（单一真源）。

集中两样东西，避免它们在各调用点各写一份、改一处漏一处：

1. **浏览器级请求头**——`fetcher`（元数据 / 播放地址）、`parser`（详情接口）、`wbi`（导航接口）
   此前各自复制了一份 UA 与请求头字典，UA 版本一动就会漂移。
   注意**字段集合的差异是有意的**：导航接口历史上只发 6 个字段、不发 `Sec-Fetch-*`，
   合并时保持原样，不要"顺手补齐"——那会改变真实请求字节，可能与平台风控行为不符。
2. **可重试状态判定**——412（风控）与 5xx（服务端）可重试，其余状态直接报错。

有意**不**集中：退避曲线与重试次数。`fetcher` 用纯指数（无抖动、容忍任意 `Retry-After`
数值），`parser` 用 `1.5·2^n + 抖动`（只认整数 `Retry-After`）——两者都是既有行为，
统一会改变真实等待时间。需要调整时请各自修改，并同步更新本文档的说明。
"""

from typing import Dict

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 元数据 / 播放地址 / 详情接口使用的完整浏览器请求头
BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.bilibili.com",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# 导航接口（取 WBI 密钥）沿用最小集合：不发 Sec-Fetch-*
NAV_HEADERS: Dict[str, str] = {
    键: BROWSER_HEADERS[键]
    for 键 in ("User-Agent", "Referer", "Accept", "Accept-Language", "Origin", "Connection")
}


def is_retryable_status(status: int) -> bool:
    """412（风控）与 5xx（服务端异常）可重试；其余状态直接报错。"""
    return status == 412 or 500 <= status <= 599
