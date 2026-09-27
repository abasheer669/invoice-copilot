import time
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict

from ap_agent import tools
from ap_agent.config import Settings
from ap_agent.tools import NotFoundError, Tool, TransientError, invoke


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Decimal


class EchoOut(BaseModel):
    value: Decimal


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(tools, "BACKOFF_S", 0)


def settings(**overrides):
    return Settings(_env_file=None, tool_timeout_s=0.05, **overrides)


def echo_tool(call=lambda args: {"value": args.value}):
    return Tool("echo", "Returns its input.", EchoArgs, EchoOut, call)


def raises(error):
    def call(args):
        raise error

    return call


def test_valid_call_returns_validated_output():
    result = invoke(echo_tool(), {"value": "12.50"}, settings())
    assert result.ok
    assert result.attempts == 1
    assert result.data == {"value": "12.50"}


@pytest.mark.parametrize(
    "raw_args",
    [{}, {"value": "abc"}, {"value": "1", "sql": "drop table vendors"}],
    ids=["missing", "wrong-type", "extra-field"],
)
def test_invalid_args_never_reach_the_tool(raw_args):
    calls = []
    result = invoke(echo_tool(calls.append), raw_args, settings())
    assert (result.ok, result.error, result.attempts) == (False, "invalid_args", 0)
    assert calls == []


def test_slow_tool_times_out_after_bounded_retries():
    def slow(args):
        time.sleep(0.3)
        return {"value": args.value}

    result = invoke(echo_tool(slow), {"value": "1"}, settings())
    assert (result.ok, result.error, result.attempts) == (False, "timeout", 3)
    assert result.duration_ms < 300  # never waited for the slow call to finish


def test_injected_timeout_fails_every_attempt():
    result = invoke(echo_tool(), {"value": "1"}, settings(faults="echo:timeout"))
    assert (result.ok, result.error, result.attempts) == (False, "timeout", 3)


def test_injected_transient_fault_recovers_on_retry():
    result = invoke(echo_tool(), {"value": "1"}, settings(faults="echo:transient"))
    assert (result.ok, result.attempts) == (True, 2)


def test_persistent_transient_error_gives_up_after_retries():
    result = invoke(echo_tool(raises(TransientError())), {"value": "1"}, settings())
    assert (result.ok, result.error, result.attempts) == (False, "transient", 3)


def test_retry_count_comes_from_config():
    tool = echo_tool(raises(TransientError()))
    result = invoke(tool, {"value": "1"}, settings(tool_max_retries=0))
    assert result.attempts == 1


def test_not_found_is_not_retried():
    result = invoke(echo_tool(raises(NotFoundError())), {"value": "1"}, settings())
    assert (result.ok, result.error, result.attempts) == (False, "not_found", 1)


def test_output_that_breaks_the_contract_is_rejected():
    tool = echo_tool(lambda args: {"value": "not a number"})
    result = invoke(tool, {"value": "1"}, settings())
    assert (result.ok, result.error, result.data) == (False, "invalid_output", None)
