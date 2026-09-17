"""Compatibility contract shared with the independent omni-media MCP projects.

The MCP servers emit a machine-readable ``OMNI_STATUS`` comment. The skill does
not import either package; it only parses the versioned wire contract.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

STATUS_RE = re.compile(r"<!-- OMNI_STATUS: (\{.*?\}) -->")
CONTRACT_VERSION = 1


class ContractCompatibilityError(ValueError):
    """Raised when a status payload cannot be safely interpreted."""


def parse_status(text: str) -> Optional[Dict[str, Any]]:
    """Return the first OMNI_STATUS object, or None for legacy text without one."""
    match = STATUS_RE.search(text or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ContractCompatibilityError(f"OMNI_STATUS 不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractCompatibilityError("OMNI_STATUS 顶层必须是对象")
    return payload


def check_contract(status: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the major contract version; missing means legacy version 0."""
    if status is None:
        return None
    if "contract_version" not in status:
        return status
    raw_version = status["contract_version"]
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ContractCompatibilityError(
            f"contract_version 必须是整数，收到 {raw_version!r}"
        )
    major = raw_version
    if major != CONTRACT_VERSION:
        raise ContractCompatibilityError(
            f"OMNI_STATUS contract_version={raw_version} 与当前技能要求的 "
            f"主版本 {CONTRACT_VERSION} 不兼容；请升级对应的 omni-media MCP。"
        )
    return status


def parse_compatible_status(text: str) -> Optional[Dict[str, Any]]:
    """Parse and validate an OMNI_STATUS payload in one step."""
    return check_contract(parse_status(text))
