from pioneer_worker import runner_registry
from pioneer_worker.config import Config


def test_pi_runner_does_not_inherit_generic_worker_provider():
    cfg = Config(backend_url="http://backend", guild_id="g", provider="anthropic")

    runner = runner_registry.build(cfg)["pi"]

    assert runner.provider is None


def test_pi_runner_uses_explicit_pi_provider():
    cfg = Config(
        backend_url="http://backend",
        guild_id="g",
        provider="anthropic",
        pi_provider="bedrock",
    )

    runner = runner_registry.build(cfg)["pi"]

    assert runner.provider == "bedrock"
