from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.ai import AIClient, AIConfig, AIResponder
from src.features.messaging.listener import MessageEvent
from src.web_search import SearchAugmentor, WebSearchConfig


pytestmark = pytest.mark.skipif(
    os.environ.get("WX4PY_RUN_REAL_WEB_SEARCH_TESTS") != "1",
    reason="真实 LLM/百度搜索链路测试默认跳过；设置 WX4PY_RUN_REAL_WEB_SEARCH_TESTS=1 后运行",
)


def _config_path() -> Path:
    return Path(os.environ.get("WX4PY_AI_CONFIG", "wx4py_ai_config.json")).expanduser()


def _load_real_responder() -> AIResponder:
    config_path = _config_path()
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    web_search_config = WebSearchConfig.from_dict(raw_config.get("web_search"))
    assert web_search_config.enabled, "web_search.enabled 必须为 true"
    assert web_search_config.api_key, "需要配置 web_search.api_key 或 BAIDU_API_KEY"

    return AIResponder(
        AIClient(AIConfig.from_file(str(config_path))),
        context_size=int(raw_config.get("ai_context_size", 8)),
        reply_on_at=True,
        max_reply_chars=int(raw_config.get("ai_max_reply_chars", 180)),
        web_search=SearchAugmentor(web_search_config),
    )


def _event(content: str, *, group: str = "真实链路测试群") -> MessageEvent:
    return MessageEvent(
        group=group,
        content=content,
        timestamp=1.0,
        group_nickname="豆角",
        sender_nickname="测试用户",
        is_at_me=True,
    )


def test_real_web_search_then_cached_followup_then_refresh_followup():
    responder = _load_real_responder()

    first_reply = responder(_event("@豆角 今日热搜"))
    assert first_reply.strip()
    assert "无法联网" not in first_reply
    assert "不能访问实时" not in first_reply

    cached_followup_reply = responder(_event("@豆角 第二条什么意思"))
    assert cached_followup_reply.strip()

    refresh_followup_reply = responder(_event("@豆角 来源是哪"))
    assert refresh_followup_reply.strip()


def test_real_web_search_disabled_skips_plugin():
    config_path = _config_path()
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    disabled_search_config = WebSearchConfig.from_dict({**raw_config.get("web_search", {}), "enabled": False})

    responder = AIResponder(
        AIClient(AIConfig.from_file(str(config_path))),
        context_size=2,
        reply_on_at=True,
        max_reply_chars=180,
        web_search=SearchAugmentor(disabled_search_config),
    )

    reply = responder(_event("@豆角 你好"))
    assert isinstance(reply, str)
