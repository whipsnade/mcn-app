"""细粒度熔断器测试（设计文档 §11.2）。

熔断键 = service + internal_tool_name + SHA256(normalized_arguments)。
只阻止短时间内对相同调用（同 service + 同工具 + 同归一化参数）的重复撞击；
趋势工具失败不得封锁情感、地域、热帖或不同平台参数。
"""

from __future__ import annotations

import hashlib

from app.agent_runtime.circuit_breaker import FineGrainedCircuitBreaker

_TREND = "social_statistic_trend"


def _args(**overrides):
    base = {"keyword": "美妆", "platform": "xiaohongshu"}
    base.update(overrides)
    return base


def test_same_service_tool_args_open_after_threshold() -> None:
    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    arguments = _args()
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is True
    for _ in range(3):
        breaker.record_failure("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is False


def test_different_args_on_same_tool_do_not_trip() -> None:
    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    args_a = _args()
    args_b = _args(keyword="护肤")
    for _ in range(5):
        breaker.record_failure("insight-cube-mcp", _TREND, args_a)
    assert breaker.allow("insight-cube-mcp", _TREND, args_a) is False
    assert breaker.allow("insight-cube-mcp", _TREND, args_b) is True


def test_different_tool_does_not_trip() -> None:
    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    arguments = _args()
    for _ in range(5):
        breaker.record_failure("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is False
    assert breaker.allow("insight-cube-mcp", "social_statistic_user_profile", arguments) is True


def test_consecutive_trend_failures_do_not_block_sentiment_or_other_args() -> None:
    """连续让趋势工具失败超过旧 service threshold 后，情感工具和不同参数
    趋势调用仍必须放行（spec §11.2 的明确要求）。"""
    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    trend_args = _args()
    for _ in range(5):  # 超过旧 service threshold(3)
        breaker.record_failure("insight-cube-mcp", _TREND, trend_args)

    # 情感工具不受影响
    assert breaker.allow("insight-cube-mcp", "social_statistic_sentiment", trend_args) is True
    # 不同参数的趋势调用不受影响
    assert breaker.allow("insight-cube-mcp", _TREND, _args(platform="douyin")) is True
    # 相同调用被阻止
    assert breaker.allow("insight-cube-mcp", _TREND, trend_args) is False


def test_key_embeds_canonical_arguments_hash() -> None:
    breaker = FineGrainedCircuitBreaker()
    first = breaker.key("insight-cube-mcp", _TREND, {"a": 1, "b": [2, 3]})
    reordered = breaker.key("insight-cube-mcp", _TREND, {"b": [2, 3], "a": 1})
    different = breaker.key("insight-cube-mcp", _TREND, {"a": 1, "b": [2, 4]})
    other_tool = breaker.key("insight-cube-mcp", "social_statistic_hot_user", {"a": 1, "b": [2, 3]})

    assert first == reordered  # canonical JSON 排序
    assert first != different
    assert first != other_tool
    assert first.endswith(hashlib.sha256(b'{"a":1,"b":[2,3]}').hexdigest())
    assert "insight-cube-mcp" in first
    assert _TREND in first


def test_record_success_resets_open_state() -> None:
    breaker = FineGrainedCircuitBreaker(failure_threshold=3, reset_seconds=60)
    arguments = _args()
    for _ in range(3):
        breaker.record_failure("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is False
    breaker.record_success("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is True


def test_half_open_probe_failure_reopens_key_not_wedged() -> None:
    """半开探测失败（如外发前错误）后，record_failure 必须重新打开并清掉
    probe_in_flight；该键不得被永久卡死（Fix 1 (b)）。"""
    now = [100.0]
    breaker = FineGrainedCircuitBreaker(
        failure_threshold=3, reset_seconds=30.0, clock=lambda: now[0]
    )
    arguments = _args()

    # 打开熔断键
    for _ in range(3):
        breaker.record_failure("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is False

    # 越过复位窗口 → 半开探测放行
    now[0] += 40.0
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is True

    # 探测失败 → 重新打开（opened_at 刷新、probe_in_flight 清掉），非永久卡死
    breaker.record_failure("insight-cube-mcp", _TREND, arguments)
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is False

    # 越过新的复位窗口后，合法探测可继续
    now[0] += 40.0
    assert breaker.allow("insight-cube-mcp", _TREND, arguments) is True
