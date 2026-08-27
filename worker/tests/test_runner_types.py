from __future__ import annotations

import pytest
from pioneer_worker.runner_types import (  # pyright: ignore[reportMissingImports]
    RunResult,
    StopReason,
)


@pytest.mark.parametrize("raw", ["end_turn", "done"])
def test_run_result_rejects_stringly_stop_reasons(raw: str) -> None:
    with pytest.raises(TypeError):
        RunResult(True, raw)  # type: ignore[arg-type]


def test_with_stop_reason_downgrades_success() -> None:
    result = RunResult(True, StopReason.SUCCESS).with_stop_reason(StopReason.NO_CHANGES)

    assert result.success is False
    assert result.stop_reason is StopReason.NO_CHANGES
