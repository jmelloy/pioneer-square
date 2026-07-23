"""Unit tests for ECS detection in pioneer_worker.logging_config."""

from __future__ import annotations

from pioneer_worker import logging_config

_ECS_ENV_VARS = [
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_EXECUTION_ENV",
]


def _clear_ecs_env(monkeypatch):
    for name in _ECS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_running_on_ecs_false_when_no_env_vars(monkeypatch):
    _clear_ecs_env(monkeypatch)
    assert logging_config.running_on_ecs() is False
    assert logging_config.default_log_format() == "colored"


def test_running_on_ecs_true_for_metadata_uri(monkeypatch):
    _clear_ecs_env(monkeypatch)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/x")
    assert logging_config.running_on_ecs() is True
    assert logging_config.default_log_format() == "plain"


def test_running_on_ecs_true_for_execution_env(monkeypatch):
    _clear_ecs_env(monkeypatch)
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")
    assert logging_config.running_on_ecs() is True


def test_running_on_ecs_false_for_unrelated_execution_env(monkeypatch):
    _clear_ecs_env(monkeypatch)
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_Lambda_python3.11")
    assert logging_config.running_on_ecs() is False
