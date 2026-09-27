"""The language model behind one small interface, chosen by LLM_PROVIDER.

Only two things are asked of a model: a tool-calling session for gathering evidence, and
a JSON answer for the recommendation. Nothing it returns is trusted until code has
validated it (see agent.py).
"""

from dataclasses import dataclass
from typing import Protocol

import httpx
from google import genai
from google.genai import errors, types

from ap_agent.config import Settings

# Marks the rules engine's draft inside a recommendation prompt.
DRAFT_OPEN, DRAFT_CLOSE = "<draft>", "</draft>"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema


@dataclass(frozen=True)
class ToolRequest:
    name: str
    args: dict


@dataclass(frozen=True)
class Reply:
    text: str
    tool_requests: list[ToolRequest]


class LLMError(Exception):
    """The model could not be reached or gave no usable answer."""


class ToolSession(Protocol):
    def send(self, message: str) -> Reply: ...

    def send_tool_results(self, results: list[tuple[ToolRequest, dict]]) -> Reply: ...


class LLM(Protocol):
    model_id: str

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession: ...

    def generate_json(self, system: str, prompt: str, schema: dict) -> str: ...


class GeminiLLM:
    def __init__(self, api_key: str, model: str):
        self.model_id = model
        # The SDK retries overload and rate-limit responses itself, with backoff.
        retry = types.HttpRetryOptions(
            attempts=3, initial_delay=1.0, max_delay=8.0, http_status_codes=[429, 500, 503, 504]
        )
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=60_000, retry_options=retry)
        )

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession:
        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.parameters
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[types.Tool(function_declarations=declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        return _GeminiSession(self._client.chats.create(model=self.model_id, config=config))

    def generate_json(self, system: str, prompt: str, schema: dict) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = _call(
            lambda: self._client.models.generate_content(
                model=self.model_id, contents=prompt, config=config
            )
        )
        return _text(response)


class _GeminiSession:
    def __init__(self, chat):
        self._chat = chat

    def send(self, message: str) -> Reply:
        return self._reply(message)

    def send_tool_results(self, results: list[tuple[ToolRequest, dict]]) -> Reply:
        parts = [types.Part.from_function_response(name=r.name, response=out) for r, out in results]
        return self._reply(parts)

    def _reply(self, message) -> Reply:
        response = _call(lambda: self._chat.send_message(message))
        requests = [ToolRequest(c.name, dict(c.args or {})) for c in response.function_calls or []]
        return Reply(text=_text(response), tool_requests=requests)


def _call(send):
    try:
        return send()
    except (errors.APIError, httpx.HTTPError) as e:
        raise LLMError(f"model call failed: {e}") from e


def _text(response: types.GenerateContentResponse) -> str:
    """The answer text, leaving out function calls and the model's thinking."""
    content = response.candidates[0].content if response.candidates else None
    parts = content.parts if content and content.parts else []
    return "".join(p.text for p in parts if p.text and not p.thought)


class EchoLLM:
    """Offline stand-in for tests and LLM_PROVIDER=fake: asks for no extra lookups and
    accepts the rules engine's draft recommendation unchanged."""

    model_id = "fake-echo"

    def tool_session(self, system: str, tools: list[ToolSpec]) -> ToolSession:
        return _NoToolsSession()

    def generate_json(self, system: str, prompt: str, schema: dict) -> str:
        return prompt.split(DRAFT_OPEN, 1)[1].split(DRAFT_CLOSE, 1)[0]


class _NoToolsSession:
    def send(self, message: str) -> Reply:
        return Reply(text="No extra lookups needed.", tool_requests=[])

    def send_tool_results(self, results: list[tuple[ToolRequest, dict]]) -> Reply:
        return self.send("")


def make_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "fake":
        return EchoLLM()
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise ValueError("LLM_API_KEY is not set; it is needed for the Gemini model")
    return GeminiLLM(settings.llm_api_key.get_secret_value(), settings.llm_model)
