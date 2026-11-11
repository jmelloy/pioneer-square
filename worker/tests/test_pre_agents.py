from pioneer_worker.pre_agents import ENV_VAR, apply_pre_agents, resolve_prompt

ENV = {ENV_VAR: "from-env"}


def test_env_only():
    assert resolve_prompt({}, None, ENV) == "from-env"


def test_toml_beats_env():
    assert resolve_prompt({}, "from-toml", ENV) == "from-toml"


def test_task_beats_toml_and_env():
    assert resolve_prompt({"pre_agents_prompt": "task"}, "from-toml", ENV) == "task"


def test_empty_task_disables():
    assert resolve_prompt({"pre_agents_prompt": ""}, "from-toml", ENV) == ""


def test_non_string_task_value_ignored():
    assert resolve_prompt({"pre_agents_prompt": None}, "from-toml", ENV) == "from-toml"


def test_blank_toml_falls_through_to_env():
    assert resolve_prompt({}, "   ", ENV) == "from-env"


def test_nothing_set():
    assert resolve_prompt({}, None, {}) == ""
    assert resolve_prompt(None, None, {}) == ""


def test_strips_whitespace():
    assert resolve_prompt({}, "  hi \n", {}) == "hi"


def test_apply_prepends():
    assert apply_pre_agents("do it", {}, "pre", {}) == "pre\n\ndo it"


def test_apply_noop_without_prompt():
    assert apply_pre_agents("do it", {}, None, {}) == "do it"


def test_apply_whitespace_only_noop():
    assert apply_pre_agents("do it", {"pre_agents_prompt": "  "}, "x", {}) == "do it"
