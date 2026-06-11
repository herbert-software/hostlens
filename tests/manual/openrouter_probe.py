#!/usr/bin/env python3
"""Probe OpenRouter /api/v1/messages endpoint for Anthropic-schema compatibility.

Three probes per model:
  auth        plain text turn — verifies x-api-key header is accepted
  tool_use    Anthropic tool_use schema — verifies non-Claude models handle it
  cache_ctrl  system block with cache_control ephemeral — verifies no 400/422

Credentials are read from .env.openrouter-probe (same dir, auto-loaded),
or from environment variables directly.  See .env.openrouter-probe for the
variable list.

Run:
    python tests/manual/openrouter_probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

_DEFAULT_BASE_URL = "https://openrouter.ai/api"

_TOOL = {
    "name": "get_weather",
    "description": "Get the weather for a city",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
}

_PASS = "\033[32m✓ PASS\033[0m"
_FAIL = "\033[31m✗ FAIL\033[0m"
_WARN = "\033[33m~ WARN\033[0m"


# ---------------------------------------------------------------------------
# .env loader (no extra dependency)
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with path.open() as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# Credential / model helpers
# ---------------------------------------------------------------------------


def _load_creds() -> tuple[str, str]:
    _load_dotenv(Path(__file__).parent / ".env.openrouter-probe")
    token = os.environ.get("HOSTLENS_OPENROUTER_TOKEN")
    base_url = os.environ.get("HOSTLENS_OPENROUTER_BASE_URL", _DEFAULT_BASE_URL)
    if not token:
        sys.exit("Missing HOSTLENS_OPENROUTER_TOKEN — fill in tests/manual/.env.openrouter-probe")
    return base_url, token


def _models() -> list[str]:
    raw = os.environ.get("HOSTLENS_OPENROUTER_MODELS", "")
    if not raw:
        sys.exit("Missing HOSTLENS_OPENROUTER_MODELS — fill in tests/manual/.env.openrouter-probe")
    return [m.strip() for m in raw.split(",") if m.strip()]


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


async def probe_auth(backend: AnthropicAPIBackend, model: str) -> bool:
    """Plain text turn — verifies x-api-key authentication is accepted."""
    try:
        resp = await backend.messages_create(
            model=model,
            system="You are a test assistant. Reply very briefly.",
            messages=[{"role": "user", "content": "Say: OK"}],
            tools=[],
            max_tokens=32,
            timeout=30.0,
        )
        types = [b.type for b in resp.content]
        ok = bool(resp.content)
        label = _PASS if ok else _FAIL
        print(f"  auth        {label}  stop={resp.stop_reason}  blocks={types}")
        return ok
    except Exception as exc:
        print(f"  auth        {_FAIL}  {type(exc).__name__}: {exc}")
        return False


async def probe_tool_use(backend: AnthropicAPIBackend, model: str) -> bool:
    """Anthropic tool_use schema — verifies model handles it correctly."""
    try:
        resp = await backend.messages_create(
            model=model,
            system="You are a weather assistant. ALWAYS call the get_weather tool.",
            messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
            tools=[_TOOL],
            max_tokens=256,
            timeout=30.0,
        )
        types = [b.type for b in resp.content]
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block is not None:
            print(
                f"  tool_use    {_PASS}  blocks={types}  "
                f"tool={tool_block.name}  input={tool_block.input}"
            )
            return True
        # Model responded but didn't call the tool — warn, not fail
        text = next((b.text for b in resp.content if b.type == "text"), "")
        print(
            f"  tool_use    {_WARN}  no tool_use block — model returned text instead\n"
            f"              blocks={types}  text={text[:80]!r}"
        )
        return False
    except Exception as exc:
        print(f"  tool_use    {_FAIL}  {type(exc).__name__}: {exc}")
        return False


async def probe_cache_control(backend: AnthropicAPIBackend, model: str) -> bool:
    """cache_control: ephemeral on system block — verifies no 400/422."""
    system = [
        {
            "type": "text",
            "text": "You are a test assistant. Reply very briefly.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    try:
        resp = await backend.messages_create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": "Say: OK"}],
            tools=[],
            max_tokens=32,
            timeout=30.0,
        )
        ok = bool(resp.content)
        label = _PASS if ok else _FAIL
        cc = resp.usage.cache_creation_input_tokens
        cr = resp.usage.cache_read_input_tokens
        print(f"  cache_ctrl  {label}  stop={resp.stop_reason}  cache_create={cc}  cache_read={cr}")
        return ok
    except Exception as exc:
        print(f"  cache_ctrl  {_FAIL}  {type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_model(base_url: str, token: str, model: str) -> dict[str, bool]:
    print(f"\n{'─' * 60}")
    print(f"MODEL : {model}")
    print(f"BASE  : {base_url}")
    print(f"{'─' * 60}")
    backend = AnthropicAPIBackend(api_key=token, base_url=base_url)
    results = {
        "auth": await probe_auth(backend, model),
        "tool_use": await probe_tool_use(backend, model),
        "cache_ctrl": await probe_cache_control(backend, model),
    }
    return results


def main() -> None:
    base_url, token = _load_creds()
    all_results: dict[str, dict[str, bool]] = {}
    for model in _models():
        all_results[model] = asyncio.run(run_model(base_url, token, model))

    print(f"\n{'═' * 60}")
    print("SUMMARY")
    print(f"{'═' * 60}")
    for model, res in all_results.items():
        row = "  ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in res.items())
        print(f"  {model:<40} {row}")


if __name__ == "__main__":
    main()
