"""Shared bounds and canonical encoding for the Pi adapter catalog."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


# These are control-plane transport bounds, not a model-visible tool limit.
PI_GATEWAY_ADAPTER_CATALOG_MAX_ENTRIES = 128
PI_GATEWAY_ADAPTER_CATALOG_MAX_BYTES = 128 * 1024


def canonical_adapter_catalog_bytes(
    entries: Sequence[Mapping[str, Any]],
) -> bytes:
    """Return deterministic UTF-8 JSON bytes for an adapter catalog."""

    return json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalized_adapter_service(service: str) -> str:
    """Match the Gateway's canonical service identity mapping."""

    return {
        "insight-cube-mcp": "insight-cube",
        "social-grow-mcp": "social-grow",
        "social-grow-content-mcp": "social-grow-content",
        "aktools-mcp": "aktools",
        "bilibili-mcp": "aktools",
    }.get(service, service)
