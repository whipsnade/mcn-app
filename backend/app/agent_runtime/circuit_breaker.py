"""细粒度熔断器（设计文档 §11.2）。

熔断键 = ``service + internal_tool_name + SHA256(normalized_arguments)``。
只阻止短时间内对**相同调用**（同 service + 同工具 + 同归一化参数）的重复撞击；
趋势工具失败不得封锁情感、地域、热帖或不同平台参数。

与 legacy ``DataTapTransport`` 的服务级熔断是**两层不同机制**：Agent 桥固定使用
本模块（transport 端 ``circuit_scope="none"``），禁止两层叠加。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.mcp_gateway.validation import canonical_json_bytes


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


class FineGrainedCircuitBreaker:
    """以 service+tool+参数哈希为键的熔断器；线程安全（asyncio 单线程也安全）。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if reset_seconds <= 0:
            raise ValueError("reset_seconds must be positive")
        self.failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._states: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def key(self, service: str, internal_tool_name: str, arguments: Mapping[str, Any]) -> str:
        """service + internal_tool_name + SHA256(canonical normalized arguments)。"""
        digest = hashlib.sha256(canonical_json_bytes(arguments)).hexdigest()
        return f"{service}\x00{internal_tool_name}\x00{digest}"

    def allow(self, service: str, internal_tool_name: str, arguments: Mapping[str, Any]) -> bool:
        """是否允许外发本次调用。熔断打开且未到复位窗口则拒绝；
        进入半开窗口只放行一个探测调用。"""
        key = self.key(service, internal_tool_name, arguments)
        with self._lock:
            state = self._states.get(key)
            if state is None or state.opened_at is None:
                return True
            if self._clock() - state.opened_at >= self._reset_seconds:
                if state.probe_in_flight:
                    return False
                state.probe_in_flight = True
                return True
            return False

    def record_success(
        self, service: str, internal_tool_name: str, arguments: Mapping[str, Any]
    ) -> None:
        key = self.key(service, internal_tool_name, arguments)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.failures = 0
            state.opened_at = None
            state.probe_in_flight = False

    def record_failure(
        self, service: str, internal_tool_name: str, arguments: Mapping[str, Any]
    ) -> None:
        key = self.key(service, internal_tool_name, arguments)
        with self._lock:
            state = self._states.setdefault(key, _BreakerState())
            state.failures += 1
            if state.probe_in_flight or state.failures >= self.failure_threshold:
                state.opened_at = self._clock()
                state.probe_in_flight = False


__all__ = ["FineGrainedCircuitBreaker"]
