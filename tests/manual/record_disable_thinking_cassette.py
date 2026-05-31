#!/usr/bin/env python3
"""录制 task 4.6 的 cassette：真实 DeepSeek thinking-free 多轮 tool 循环。

凭据从环境变量读（**不读 cc-switch**，遵守 tasks 5.2 约定）：
    HOSTLENS_DEEPSEEK_TOKEN     DeepSeek anthropic 端点 token
    HOSTLENS_DEEPSEEK_BASE_URL  例如 https://api.deepseek.com/anthropic
    HOSTLENS_DEEPSEEK_MODEL     可选，默认 deepseek-v4-flash

把 2 轮 (turn1 tool_use → turn2 end_turn) 的真实 request/response 写进
tests/fixtures/cassettes/deepseek_disable_thinking_multiturn.jsonl，供
test_deepseek_disable_thinking_replay.py 离线回放。

⚠️ 本文件是手动录制工具，不是 pytest 用例（不以 test_ 开头，pytest 不收集）。
回放测试用的 model / system / tools / 首条 messages 必须与这里逐字一致，否则
PlaybackBackend 的 key（SHA256(model+messages+tools_count)）不命中。

用法：
    HOSTLENS_DEEPSEEK_TOKEN=... HOSTLENS_DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic \
        python tests/manual/record_disable_thinking_cassette.py
"""

# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend  # noqa: E402

CASSETTE = REPO / "tests" / "fixtures" / "cassettes" / "deepseek_disable_thinking_multiturn.jsonl"

# 与回放测试 (test_deepseek_disable_thinking_replay.py) 逐字一致的常量。
_MODEL = os.environ.get("HOSTLENS_DEEPSEEK_MODEL", "deepseek-v4-flash")
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


def _record(req_messages: list[dict], resp) -> dict:
    return {
        "request": {"model": _MODEL, "messages": req_messages, "tools_count": len(_TOOLS)},
        "response": json.loads(resp.model_dump_json()),
    }


async def main() -> int:
    token = os.environ.get("HOSTLENS_DEEPSEEK_TOKEN")
    base_url = os.environ.get("HOSTLENS_DEEPSEEK_BASE_URL")
    if not token or not base_url:
        print("set HOSTLENS_DEEPSEEK_TOKEN / HOSTLENS_DEEPSEEK_BASE_URL", file=sys.stderr)
        return 2

    backend = AnthropicAPIBackend(api_key=token, base_url=base_url, disable_thinking=True)
    records: list[dict] = []

    r1 = await backend.messages_create(
        model=_MODEL,
        system=_SYSTEM,
        messages=_FIRST_MESSAGES,
        tools=_TOOLS,
        max_tokens=1024,
        timeout=60.0,
    )
    t1 = [b.type for b in r1.content]
    print(f"[turn1] {t1} stop={r1.stop_reason}")
    assert "thinking" not in t1, f"turn1 leaked thinking: {t1}"
    records.append(_record(_FIRST_MESSAGES, r1))

    tool_use = next((b for b in r1.content if b.type == "tool_use"), None)
    if tool_use is None:
        print("[warn] turn1 没产生 tool_use；重试或换问句。")
        return 1

    second_messages = [
        *_FIRST_MESSAGES,
        {"role": "assistant", "content": [b.model_dump() for b in r1.content]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use.id, "content": _TOOL_RESULT_TEXT}
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
    t2 = [b.type for b in r2.content]
    print(f"[turn2] {t2} stop={r2.stop_reason}")
    assert "thinking" not in t2, f"turn2 leaked thinking: {t2}"
    records.append(_record(second_messages, r2))

    CASSETTE.parent.mkdir(parents=True, exist_ok=True)
    with CASSETTE.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"[written] {CASSETTE}  ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
