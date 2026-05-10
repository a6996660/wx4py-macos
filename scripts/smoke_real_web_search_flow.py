#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟微信消息，调用真实 LLM 和百度网络插件做冒烟测试。

默认读取 WX4PY_AI_CONFIG 或 ./wx4py_ai_config.json。
不会连接微信，只构造 MessageEvent 调用 AIResponder。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai import AIClient, AIConfig, AIResponder
from src.features.messaging.listener import MessageEvent
from src.web_search import SearchAugmentor, WebSearchConfig


def _config_path() -> Path:
    return Path(os.environ.get("WX4PY_AI_CONFIG", "wx4py_ai_config.json")).expanduser()


def _event(content: str, *, group: str = "真实链路测试群") -> MessageEvent:
    return MessageEvent(
        group=group,
        content=content,
        timestamp=1.0,
        group_nickname="豆角",
        sender_nickname="测试用户",
        is_at_me=True,
    )


def main() -> int:
    config_path = _config_path()
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    web_search_config = WebSearchConfig.from_dict(raw_config.get("web_search"))
    if not web_search_config.enabled:
        raise RuntimeError("web_search.enabled 当前为 false，请先在配置里启用网络插件")
    if not web_search_config.api_key:
        raise RuntimeError("缺少百度 API Key，请配置 web_search.api_key 或 BAIDU_API_KEY")

    responder = AIResponder(
        AIClient(AIConfig.from_file(str(config_path))),
        context_size=int(raw_config.get("ai_context_size", 8)),
        reply_on_at=True,
        max_reply_chars=int(raw_config.get("ai_max_reply_chars", 180)),
        web_search=SearchAugmentor(web_search_config),
    )

    messages = [
        ("联网首问", "@豆角 今日热搜"),
        ("普通追问，期望复用上轮百度结果", "@豆角 第二条什么意思"),
        ("核验追问，期望拼接上轮主题重新搜", "@豆角 来源是哪"),
        ("另一个群追问，期望不复用上个群缓存", "@豆角 第二条什么意思"),
    ]
    for label, content in messages[:3]:
        reply = responder(_event(content))
        print(f"\n[{label}] {content}\n{reply}")

    other_group_reply = responder(_event(messages[3][1], group="另一个真实链路测试群"))
    print(f"\n[{messages[3][0]}] {messages[3][1]}\n{other_group_reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
