from app.tasks.dependencies import summary_deltas_to_persist


def test_summary_recovery_skips_already_persisted_deltas_before_resuming() -> None:
    assert summary_deltas_to_persist("第一段第二段", ("第一段", "第二段", "第三段")) == ("第三段",)


def test_completed_summary_does_not_replay_any_delta() -> None:
    assert summary_deltas_to_persist("完整总结", ("完整", "总结"), completed=True) == ()
