from __future__ import annotations

import json
from typing import List

from src.ai import AIResponder
from src.features.messaging.listener import MessageEvent
from src.web_search import SearchAugmentor, WebSearchConfig


class FakeLLM:
    def __init__(self):
        self.final_calls: List[List[dict]] = []
        self.system_prompts: List[str] = []

    def chat(self, messages: List[dict], system_prompt=None) -> str:
        self.system_prompts.append(system_prompt or "")
        if "联网判断器" in (system_prompt or ""):
            user_prompt = messages[-1]["content"]
            if "今日热搜" in user_prompt or "今天有什么新闻" in user_prompt:
                return json.dumps({"need_search": True, "reason": "实时信息"})
            return json.dumps({"need_search": False, "reason": "不需要联网"})

        self.final_calls.append(messages)
        joined = "\n".join(message["content"] for message in messages)
        if "王楚钦" in joined:
            return "1. 王楚钦 1179248\n2. 母亲节 994565"
        if "百度结果里没看到" in joined:
            return "百度结果里没看到准确信息"
        return "普通回复"


class FakeSearchClient:
    def __init__(self):
        self.queries: List[str] = []

    def search(self, query: str, *, count=None):
        self.queries.append(query)
        return [
            {
                "title": "微博热搜榜",
                "content": "1 王楚钦 1179248\n2 母亲节 994565",
                "date": "2026-05-10",
                "url": "https://s.weibo.com/top/summary",
            }
        ]


def make_event(content: str, *, group: str = "项目测试") -> MessageEvent:
    return MessageEvent(
        group=group,
        content=content,
        timestamp=1.0,
        group_nickname="豆角",
        sender_nickname="丁某某",
        is_at_me=True,
    )


def make_responder(*, enabled: bool = True):
    llm = FakeLLM()
    augmentor = SearchAugmentor(WebSearchConfig(enabled=enabled, api_key="fake", count=2))
    search_client = FakeSearchClient()
    augmentor.client = search_client
    responder = AIResponder(
        llm,
        context_size=8,
        reply_on_at=True,
        max_reply_chars=180,
        web_search=augmentor,
    )
    return responder, llm, search_client, augmentor


def test_web_search_hit_uses_baidu_results_only():
    responder, llm, search_client, _augmentor = make_responder()

    reply = responder(make_event("@豆角 今日热搜"))

    assert search_client.queries == ["今日热搜"]
    assert "王楚钦" in reply
    assert len(llm.final_calls[-1]) == 3
    assert "最终回复必须完全来自下面的百度搜索结果" in llm.final_calls[-1][1]["content"]
    assert "搜索结果转写器" in llm.system_prompts[-1]


def test_web_search_disabled_skips_planning_and_search():
    responder, llm, search_client, _augmentor = make_responder(enabled=False)

    reply = responder(make_event("@豆角 今日热搜"))

    assert reply == "普通回复"
    assert search_client.queries == []
    assert not any("联网判断器" in prompt for prompt in llm.system_prompts)


def test_normal_followup_reuses_last_baidu_result_without_new_search():
    responder, _llm, search_client, _augmentor = make_responder()

    first_reply = responder(make_event("@豆角 今日热搜"))
    second_reply = responder(make_event("@豆角 第二条什么意思"))

    assert "母亲节" in first_reply
    assert "母亲节" in second_reply
    assert search_client.queries == ["今日热搜"]


def test_refresh_followup_searches_with_cached_topic():
    responder, _llm, search_client, _augmentor = make_responder()

    responder(make_event("@豆角 今日热搜"))
    responder(make_event("@豆角 来源是哪"))

    assert search_client.queries[0] == "今日热搜"
    assert len(search_client.queries) == 2
    assert "今日热搜" in search_client.queries[1]
    assert "来源是哪" in search_client.queries[1]


def test_search_cache_is_scoped_by_group():
    responder, _llm, search_client, _augmentor = make_responder()

    responder(make_event("@豆角 今日热搜", group="项目测试A"))
    responder(make_event("@豆角 第二条什么意思", group="项目测试B"))

    assert search_client.queries == ["今日热搜"]


def test_realtime_first_question_is_not_blocked_as_followup():
    responder, _llm, search_client, _augmentor = make_responder()

    responder(make_event("@豆角 今天大盘多少点", group="项目测试C"))

    assert search_client.queries == ["今天大盘多少点"]
