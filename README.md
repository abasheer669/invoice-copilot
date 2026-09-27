# invoice-copilot

A small CLI-based AI agent that helps companies process supplier invoices. It gathers evidence, checks it against policy in code, recommends an outcome with citations, and pauses for a human before any decision is recorded.

## Prerequisites

- macOS or Linux
- [uv](https://docs.astral.sh/uv/) (`brew install uv`), which installs Python 3.13 when needed

## Setup

```sh
uv sync                     # create .venv from uv.lock
cp .env.example .env        # then set LLM_API_KEY
uv run ap --version
uv run ap config            # effective settings, secrets masked
```

## Tests and lint

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Layout

| Path | Contents |
| --- | --- |
| `src/ap_agent/` | Application code (`config.py`, `cli.py`) |
| `data/corpus/` | The 15 policy documents, including superseded, irrelevant and adversarial ones |
| `tests/unit/` | Offline unit tests |
