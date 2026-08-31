"""Tests for foreman.run.ForemanRun — the round loop, driven with test doubles.

Per issue #1241's acceptance criteria: "A foreman turn can be tested with a
scripted LLM and an in-memory journal — no patch of private functions."
FakeLLM/RecordingJournal/FakeHistory/ScriptedToolExecutor below replace the
105-line/13-patch setup test_foreman_poll_backoff.py used to need to drive
the pre-#1241 monolithic _run_foreman_ai.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from foreman.ports import LLMResult
from foreman.run import ForemanRun, RunConfig


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="text", text=text, model_dump=lambda: {"type": "text", "text": text}
    )


def _tool_use_block(name: str, tool_id: str, inp: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use",
        name=name,
        id=tool_id,
        input=inp,
        model_dump=lambda: {"type": "tool_use", "id": tool_id, "name": name, "input": inp},
    )


class FakeLLM:
    """Queue of canned responses, one per round.call()."""

    def __init__(self, responses: list[list]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def call(self, **kwargs) -> LLMResult:
        self.calls.append(kwargs)
        content = self._responses.pop(0)
        return LLMResult(content=content, stop_reason="end_turn", api_log_id=len(self.calls))


class ScriptedToolExecutor:
    """Returns canned tool_results for a batch of tool_use blocks."""

    def __init__(self, results_by_tool_use_id: dict[str, dict]) -> None:
        self._results = results_by_tool_use_id
        self.batches: list[list] = []

    async def exec(self, guild_id, tool_uses, *, user_id=None) -> list[dict]:
        self.batches.append(tool_uses)
        return [self._results[tu.id] for tu in tool_uses]


class RecordingJournal:
    """Appends every call to an in-memory list instead of touching WS/DB/Discord."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next_id = 1

    async def system(self, content) -> int:
        self.calls.append(("system", content))
        return self._alloc()

    async def human(self, content) -> int:
        self.calls.append(("human", content))
        return self._alloc()

    async def assistant_turn(self, blocks, *, api_log_id) -> int:
        self.calls.append(("assistant_turn", blocks))
        return self._alloc()

    async def tool_response_turn(self, results, *, parent_id) -> int:
        self.calls.append(("tool_response_turn", results, parent_id))
        return self._alloc()

    async def text(self, content) -> None:
        self.calls.append(("text", content))

    async def tool_use(self, tu) -> None:
        self.calls.append(("tool_use", tu))

    async def tool_result(self, result) -> None:
        self.calls.append(("tool_result", result))

    def _alloc(self) -> int:
        turn_id = self._next_id
        self._next_id += 1
        return turn_id


class FakeHistory:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self._messages = messages or []

    async def load_for_llm(self, guild_id, user_id) -> list[dict]:
        return list(self._messages)


def _run_config(**overrides) -> RunConfig:
    defaults = dict(guild_id="g1", user_id="u-1", task_id=None, trigger=None, max_rounds=10)
    defaults.update(overrides)
    return RunConfig(**defaults)


async def test_end_turn_with_no_tool_use_stops_after_one_round():
    llm = FakeLLM([[_text_block("all done")]])
    journal = RecordingJournal()
    run = ForemanRun(
        _run_config(),
        llm=llm,
        tools=ScriptedToolExecutor({}),
        journal=journal,
        history=FakeHistory(),
    )

    await run.execute(
        "do the thing",
        system_blocks=[{"type": "text", "text": "sys"}],
        state_preamble="",
        audit_system="sys",
    )

    assert len(llm.calls) == 1
    kinds = [c[0] for c in journal.calls]
    assert kinds == ["system", "human", "assistant_turn", "text"]
    assert journal.calls[-1] == ("text", "all done")


async def test_tool_use_round_dispatches_tools_and_continues():
    tu = _tool_use_block("create_task", "tu-1", {"name": "x"})
    llm = FakeLLM(
        [
            [tu],
            [_text_block("created it")],
        ]
    )
    tools = ScriptedToolExecutor(
        {"tu-1": {"type": "tool_result", "tool_use_id": "tu-1", "content": "task t-1 created"}}
    )
    journal = RecordingJournal()
    run = ForemanRun(_run_config(), llm=llm, tools=tools, journal=journal, history=FakeHistory())

    await run.execute(
        "create a task",
        system_blocks=[{"type": "text", "text": "sys"}],
        state_preamble="",
        audit_system="sys",
    )

    assert len(llm.calls) == 2
    assert tools.batches == [[tu]]
    kinds = [c[0] for c in journal.calls]
    assert kinds == [
        "system",
        "human",
        "assistant_turn",
        "tool_use",
        "tool_result",
        "tool_response_turn",
        "assistant_turn",
        "text",
    ]
    # tool_result is journaled before the tool_response_turn that bundles it
    tool_result_call = next(c for c in journal.calls if c[0] == "tool_result")
    assert tool_result_call[1]["content"] == "task t-1 created"


async def test_hitting_round_cap_forces_a_tool_free_wrap_up():
    tu = _tool_use_block("create_task", "tu-1", {})
    # Every scripted round returns a tool_use — max_rounds=2 exhausts the loop.
    llm = FakeLLM(
        [
            [tu],
            [tu],
            [_text_block("wrap-up summary")],
        ]
    )
    tools = ScriptedToolExecutor(
        {"tu-1": {"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"}}
    )
    journal = RecordingJournal()
    run = ForemanRun(
        _run_config(max_rounds=2), llm=llm, tools=tools, journal=journal, history=FakeHistory()
    )

    await run.execute(
        "loop forever",
        system_blocks=[{"type": "text", "text": "sys"}],
        state_preamble="",
        audit_system="sys",
    )

    # 2 normal rounds + 1 forced wrap-up round with tool_choice={"type": "none"}
    assert len(llm.calls) == 3
    assert llm.calls[-1]["tool_choice"] == {"type": "none"}
    assert ("text", "wrap-up summary") in journal.calls
    cap_note = next(c for c in journal.calls if c[0] == "text" and "safety cap" in c[1])
    assert "2-round safety cap" in cap_note[1]


async def test_history_is_seeded_from_history_port():
    llm = FakeLLM([[_text_block("ok")]])
    history = FakeHistory([{"role": "user", "content": "earlier message"}])
    run = ForemanRun(
        _run_config(),
        llm=llm,
        tools=ScriptedToolExecutor({}),
        journal=RecordingJournal(),
        history=history,
    )

    await run.execute(
        "follow up",
        system_blocks=[{"type": "text", "text": "sys"}],
        state_preamble="",
        audit_system="sys",
    )

    sent_messages = llm.calls[0]["messages"]
    # _inject_state_preamble prepends a (here empty) state block to the last
    # user turn's content, turning it into a list of text blocks.
    last_msg = sent_messages[-1]
    assert last_msg["role"] == "user"
    assert any(block.get("text") == "earlier message" for block in last_msg["content"])
