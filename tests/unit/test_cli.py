import json

import pytest
from typer.testing import CliRunner

from ap_agent import __version__
from ap_agent.cli import app
from ap_agent.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    # Run from an empty directory so a developer's local .env is never read.
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_config_prints_settings_as_json():
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["max_tool_calls"] == 8
    assert data["llm_provider"] == "gemini"


def test_config_never_prints_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@db:5432/x")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "sk-test-not-a-real-key" not in result.output
    assert "hunter2" not in result.output
    assert json.loads(result.output)["llm_api_key"] == "**********"
