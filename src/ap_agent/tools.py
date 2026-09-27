"""The tool contract: validated input, a hard deadline, bounded retries, validated output.

Every tool call goes through `invoke`, which never raises for expected failures; it returns
a ToolResult saying what happened, so the caller can record it and decide what to do.
"""

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from ap_agent.config import Settings

ToolError = Literal["invalid_args", "timeout", "transient", "not_found", "invalid_output"]

BACKOFF_S = 0.2  # waits 0.2 s, then 0.4 s, ... between attempts


class TransientError(Exception):
    """A failure worth retrying, such as a dropped connection."""


class NotFoundError(Exception):
    """The record does not exist; retrying will not help."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    call: Callable[[Any], Any]


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: dict | None = None
    error: ToolError | None = None
    attempts: int
    duration_ms: int


class ToolCall(BaseModel):
    """One call as recorded in a run's evidence."""

    tool: str
    args: dict
    result: ToolResult
    by: Literal["code", "llm"] = "code"  # who asked for it


# Calls run on worker threads so the deadline holds even if a tool ignores its own timeouts.
_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


def invoke(tool: Tool, raw_args: dict, settings: Settings) -> ToolResult:
    started = time.monotonic()

    def finish(**fields: Any) -> ToolResult:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ToolResult(tool=tool.name, duration_ms=elapsed_ms, **fields)

    try:
        args = tool.input_model.model_validate(raw_args)
    except ValidationError:
        return finish(ok=False, error="invalid_args", attempts=0)

    fault = settings.faults.get(tool.name)
    error: ToolError = "transient"
    for attempt in range(1, settings.tool_max_retries + 2):
        if attempt > 1:
            time.sleep(BACKOFF_S * 2 ** (attempt - 2))
        future = _workers.submit(_call, tool, args, fault, attempt, settings.tool_timeout_s)
        try:
            out = future.result(timeout=settings.tool_timeout_s)
        except TimeoutError:
            error = "timeout"
            continue
        except TransientError:
            error = "transient"
            continue
        except NotFoundError:
            return finish(ok=False, error="not_found", attempts=attempt)
        try:
            data = tool.output_model.model_validate(out).model_dump(mode="json")
        except ValidationError:
            return finish(ok=False, error="invalid_output", attempts=attempt)
        return finish(ok=True, data=data, attempts=attempt)
    return finish(ok=False, error=error, attempts=attempt)


def _call(tool: Tool, args: BaseModel, fault: str | None, attempt: int, timeout_s: float) -> Any:
    if fault == "timeout":
        time.sleep(timeout_s + 0.1)  # answers just after the deadline
        return None
    if fault == "transient" and attempt == 1:
        raise TransientError(f"injected transient fault in {tool.name}")
    return tool.call(args)
