"""Task 4.6: PlaybackBackend 回放真实 DeepSeek thinking-free 多轮序列。

cassette `tests/fixtures/cassettes/deepseek_disable_thinking_multiturn.jsonl` 由
`tests/manual` 的录制脚本对真实 DeepSeek 端点（`disable_thinking=True`）录制（**非手写**），
锁定「我方对 thinking-free 多轮（user→tool_use→tool_result→续轮）响应的解析」不回归。
CI 默认 replay，无需任何凭据 / 网络。

本测试用的 model / system / tools / 首条 messages 必须与录制脚本逐字一致，否则
PlaybackBackend 的 key（SHA256(model+messages+tools_count)）不命中、抛 CassetteMiss。
"""

# ruff: noqa: RUF002, RUF003

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.agent.backend import MessageResponse
from hostlens.agent.backends.playback import PlaybackBackend

_CASSETTE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "cassettes"
    / "deepseek_disable_thinking_multiturn.jsonl"
)

# 与录制脚本逐字一致的常量（key 命中所需）。
_MODEL = "deepseek-v4-flash"
_SYSTEM = "You are Hostlens, a server inspection agent."
_TOOLS: list[dict] = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]
_FIRST_MESSAGES: list[dict] = [{"role": "user", "content": "What's the weather in Paris?"}]
_TOOL_RESULT_TEXT = "Sunny, 22C"


def _assert_thinking_free(resp: MessageResponse) -> list:
    types = [block.type for block in resp.content]
    assert types, "response had no content blocks"
    assert "thinking" not in types, f"unexpected thinking block: {types}"
    for btype in types:
        assert btype in ("text", "tool_use"), f"unexpected block type {btype}: {types}"
    return list(resp.content)


@pytest.mark.asyncio
async def test_deepseek_disable_thinking_multiturn_replays_thinking_free() -> None:
    backend = PlaybackBackend(cassette_path=_CASSETTE)

    # turn 1 —— 期望 tool_use，且无 thinking 块
    r1 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=_FIRST_MESSAGES,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    content1 = _assert_thinking_free(r1)
    tool_use = next((b for b in content1 if b.type == "tool_use"), None)
    assert tool_use is not None, "recorded turn1 应含 tool_use"

    # turn 2 —— 原样回传 assistant content（dict）+ tool_result 续轮，仍无 thinking
    second_messages = [
        *_FIRST_MESSAGES,
        {"role": "assistant", "content": [b.model_dump() for b in content1]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": _TOOL_RESULT_TEXT,
                }
            ],
        },
    ]
    r2 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=second_messages,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    _assert_thinking_free(r2)
