import pytest
from pydantic import ValidationError

from ap_agent.config import Settings


def test_defaults_match_design_budgets():
    s = Settings(_env_file=None)
    assert s.max_steps == 12
    assert s.max_tool_calls == 8
    assert s.tool_timeout_s == 3
    assert s.tool_max_retries == 2
    assert s.llm_api_key is None


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-other-model")
    monkeypatch.setenv("MAX_TOOL_CALLS", "4")
    s = Settings(_env_file=None)
    assert s.llm_model == "some-other-model"
    assert s.max_tool_calls == 4


def test_invalid_budget_is_rejected(monkeypatch):
    monkeypatch.setenv("MAX_TOOL_CALLS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_are_masked_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-a-real-key")
    s = Settings(_env_file=None)
    assert "sk-test-not-a-real-key" not in repr(s)
    assert "postgres:postgres" not in repr(s)
    assert s.llm_api_key.get_secret_value() == "sk-test-not-a-real-key"
