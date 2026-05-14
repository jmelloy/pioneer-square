"""Unit tests for pioneer_worker.cli startup validation."""

from __future__ import annotations

from pioneer_worker import cli


def _write_toml(tmp_path, body: str):
    p = tmp_path / "pioneer-worker.toml"
    p.write_text(body)
    return p


def test_cli_exits_when_no_repos_configured(tmp_path, capsys, monkeypatch):
    """No repos in config -> exit 2 before constructing the Worker."""
    monkeypatch.delenv("PIONEER_REPOS", raising=False)
    monkeypatch.delenv("PIONEER_ORG", raising=False)
    toml_path = _write_toml(
        tmp_path,
        'backend_url = "ws://x:1"\nguild_id = "g"\n',
    )
    rc = cli.main(["--config", str(toml_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no repos configured" in err.lower()


def test_cli_exits_when_repos_list_empty(tmp_path, capsys, monkeypatch):
    """Explicitly empty repos list also fails the check."""
    monkeypatch.delenv("PIONEER_REPOS", raising=False)
    monkeypatch.delenv("PIONEER_ORG", raising=False)
    toml_path = _write_toml(
        tmp_path,
        'backend_url = "ws://x:1"\nguild_id = "g"\n[github]\nrepos = []\n',
    )
    rc = cli.main(["--config", str(toml_path)])
    assert rc == 2
    assert "no repos configured" in capsys.readouterr().err.lower()


def test_cli_repos_check_passes_with_cli_override(tmp_path, monkeypatch):
    """A --repo override satisfies the check; we stub Worker.run to avoid network."""
    toml_path = _write_toml(
        tmp_path,
        'backend_url = "ws://x:1"\nguild_id = "g"\n',
    )

    captured = {}

    class _FakeWorker:
        def __init__(self, cfg):
            captured["repos"] = list(cfg.repos)

        async def run(self):
            return None

    monkeypatch.setattr(cli, "Worker", _FakeWorker)
    rc = cli.main(["--config", str(toml_path), "--repo", "owner/repo"])
    assert rc == 0
    assert captured["repos"] == ["owner/repo"]
