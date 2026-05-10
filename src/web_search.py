# -*- coding: utf-8 -*-
"""LLM 模式下的联网搜索增强。"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from .utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class WebSearchConfig:
    """网络搜索配置。"""

    enabled: bool = False
    provider: str = "baidu"
    api_key: str = ""
    count: int = 5
    timeout: float = 10.0
    auto_detect: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WebSearchConfig":
        if not isinstance(data, dict):
            return cls()
        provider = str(data.get("provider", "baidu") or "baidu").strip().lower()
        api_key = str(data.get("api_key", "") or "").strip() or os.environ.get("BAIDU_API_KEY", "").strip()
        return cls(
            enabled=bool(data.get("enabled", False)),
            provider=provider,
            api_key=api_key,
            count=_bounded_int(data.get("count", 5), default=5, minimum=1, maximum=50),
            timeout=_positive_float(data.get("timeout", 10), default=10.0),
            auto_detect=bool(data.get("auto_detect", True)),
        )


@dataclass(frozen=True)
class SearchPlan:
    """联网搜索计划。"""

    need_search: bool = False
    queries: List[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.queries is None:
            object.__setattr__(self, "queries", [])


@dataclass(frozen=True)
class CachedSearch:
    """最近一次联网搜索结果，用于追问复用。"""

    topic: str
    results_by_query: List[tuple[str, List[Dict[str, Any]]]]
    created_at: datetime
    last_reply: str = ""


class BaiduWebSearchClient:
    """百度千帆 AI Search 客户端。"""

    ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"

    def __init__(self, config: WebSearchConfig):
        self.config = config

    def search(self, query: str, *, count: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.config.api_key:
            raise RuntimeError("未配置百度搜索 API Key，请填写 web_search.api_key 或设置 BAIDU_API_KEY")

        top_k = _bounded_int(count if count is not None else self.config.count, default=self.config.count, minimum=1, maximum=50)
        logger.info("百度搜索请求: query=%s count=%d", query[:120], top_k)
        request_body = {
            "messages": [
                {
                    "content": query,
                    "role": "user",
                }
            ],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": top_k}],
            "search_filter": {},
        }
        request = urllib.request.Request(
            url=self.ENDPOINT,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "X-Appbuilder-From": "wx4py",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning("百度搜索 HTTP 错误: status=%s body=%s", exc.code, body[:500])
            raise RuntimeError(f"百度搜索 HTTP 请求失败: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.gaierror):
                raise RuntimeError("百度搜索域名解析失败，请检查网络、DNS 或代理") from exc
            raise RuntimeError(f"百度搜索网络请求失败: {reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"百度搜索请求超时（>{self.config.timeout:g}秒）") from exc

        if isinstance(data, dict) and data.get("code"):
            logger.warning("百度搜索业务错误: code=%s msg=%s", data.get("code"), data.get("message", "")[:500])
            raise RuntimeError(str(data.get("message") or data))
        references = data.get("references") if isinstance(data, dict) else None
        if not isinstance(references, list):
            logger.info("百度搜索返回无结果: query=%s", query[:120])
            return []
        logger.info("百度搜索返回: query=%s results=%d", query[:120], len(references))
        for idx, item in enumerate(references[:3], start=1):
            title = _first_text(item, "title", "name", "site_name") or "无标题"
            logger.debug("百度搜索结果[%d]: title=%s", idx, title[:80])
        return [item for item in references if isinstance(item, dict)]


class SearchAugmentor:
    """根据消息内容自动搜索，并生成可注入给 LLM 的上下文。"""

    REALTIME_KEYWORDS = (
        "天气",
        "今天",
        "现在",
        "当前",
        "最新",
        "新闻",
        "热搜",
        "股价",
        "股票",
        "美股",
        "A股",
        "港股",
        "行情",
        "大盘",
        "上证",
        "沪指",
        "深成指",
        "创业板",
        "指数",
        "收盘",
        "点位",
        "多少点",
        "大涨",
        "暴涨",
        "汇率",
        "价格",
        "政策",
        "比赛",
        "赛事",
        "赛程",
        "日程",
        "航班",
        "限行",
        "地震",
        "台风",
        "油价",
        "金价",
        "票房",
        "榜单",
    )
    STRONG_SEARCH_KEYWORDS = (
        "天气",
        "新闻",
        "热搜",
        "股价",
        "股票",
        "美股",
        "A股",
        "港股",
        "行情",
        "大盘",
        "上证",
        "沪指",
        "深成指",
        "创业板",
        "指数",
        "收盘",
        "点位",
        "多少点",
        "大涨",
        "暴涨",
        "汇率",
        "价格",
        "多少钱",
        "最便宜",
        "政策",
        "比赛",
        "赛事",
        "赛程",
        "航班",
        "限行",
        "地震",
        "台风",
        "油价",
        "金价",
        "票房",
        "榜单",
        "最新",
    )
    FOLLOWUP_SEARCH_KEYWORDS = (
        "什么时候",
        "哪天",
        "日期",
        "具体时间",
        "具体日期",
        "最近是指",
        "来源",
        "出处",
        "链接",
        "哪来的",
        "真实吗",
        "真的假的",
    )
    FOLLOWUP_REFERENCE_KEYWORDS = (
        "为什么",
        "咋回事",
        "怎么回事",
        "展开",
        "详细",
        "具体说说",
        "这个",
        "那个",
        "这条",
        "上一条",
        "第二条",
        "第三条",
        "第2条",
        "第3条",
        "什么意思",
        "意思是",
        "占比",
        "多少",
        "哪一个",
        "哪个",
    )
    FOLLOWUP_REFRESH_KEYWORDS = (
        "最新",
        "现在",
        "当前",
        "今天",
        "刚刚",
        "更新",
        "来源",
        "出处",
        "链接",
        "日期",
        "具体日期",
        "具体时间",
        "什么时候",
        "哪天",
        "真实吗",
        "真的假的",
        "核实",
        "核验",
    )

    def __init__(self, config: WebSearchConfig):
        self.config = config
        self.client = BaiduWebSearchClient(config)
        self._cache: Dict[str, CachedSearch] = {}

    def augment(
        self,
        query: str,
        *,
        messages: Optional[Sequence[dict]] = None,
        llm_client: Any = None,
        scope: Optional[str] = None,
    ) -> Optional[dict]:
        """返回一条 system 消息；无需搜索或搜索失败时返回 None。"""
        query = str(query or "").strip()
        scope_key = str(scope or "default")
        logger.info("联网搜索判断: query=%s enabled=%s", query[:120], self.config.enabled)
        if not self.config.enabled or not query:
            logger.debug("联网搜索跳过: enabled=%s query_empty=%s", self.config.enabled, not query)
            return None
        local_datetime_context = self.local_datetime_context(query)
        if local_datetime_context:
            logger.info("联网搜索改用本机时间上下文: query=%s", query[:120])
            return {
                "role": "system",
                "content": local_datetime_context,
            }

        cached_context = self.cached_followup_context(query, scope=scope_key)
        if cached_context:
            logger.info("联网搜索追问复用缓存: scope=%s query=%s", scope_key, query[:120])
            return {
                "role": "system",
                "content": cached_context,
            }
        if self.is_followup_query(query) and not self._has_strong_search_intent(query) and not self._cache.get(scope_key):
            logger.info("联网搜索跳过无上下文追问: scope=%s query=%s", scope_key, query[:120])
            return None

        plan = self.plan_search(query, messages=messages, llm_client=llm_client, scope=scope_key)
        logger.info(
            "联网搜索规划: original=%s need_search=%s queries=%s reason=%s",
            query[:120],
            plan.need_search,
            plan.queries,
            plan.reason[:160],
        )
        if not plan.need_search:
            return None

        try:
            results_by_query = []
            seen = set()
            for search_query in plan.queries:
                count = self._search_count_for_query(search_query)
                query_results = self.client.search(search_query, count=count)
                query_results = self._rank_results(query, query_results)
                unique_results = []
                for item in query_results:
                    dedupe_key = (
                        _first_text(item, "url", "link", "page_url", "refer_url")
                        or _first_text(item, "title", "name", "site_name")
                    )
                    if dedupe_key and dedupe_key in seen:
                        continue
                    if dedupe_key:
                        seen.add(dedupe_key)
                    unique_results.append(item)
                results_by_query.append((search_query, unique_results))
        except Exception as exc:
            logger.warning("网络搜索失败，降级为普通 LLM 回复: query=%s error=%s", query[:80], exc)
            return None

        result_count = sum(len(items) for _search_query, items in results_by_query)
        if result_count:
            self.remember_search(scope_key, topic=query, results_by_query=results_by_query)
        context = self.format_context(query, results_by_query, plan=plan)
        logger.info(
            "联网搜索上下文生成: original=%s queries=%s results=%d context_len=%d",
            query[:120],
            plan.queries,
            result_count,
            len(context),
        )
        return {
            "role": "system",
            "content": context,
        }

    def remember_search(
        self,
        scope: str,
        *,
        topic: str,
        results_by_query: List[tuple[str, List[Dict[str, Any]]]],
        last_reply: str = "",
    ) -> None:
        self._cache[str(scope or "default")] = CachedSearch(
            topic=_compact_topic(topic, max_len=120),
            results_by_query=results_by_query,
            created_at=datetime.now().astimezone(),
            last_reply=str(last_reply or "").strip(),
        )

    def remember_reply(self, scope: str, reply: str) -> None:
        scope_key = str(scope or "default")
        cached = self._cache.get(scope_key)
        if not cached:
            return
        self._cache[scope_key] = CachedSearch(
            topic=cached.topic,
            results_by_query=cached.results_by_query,
            created_at=cached.created_at,
            last_reply=str(reply or "").strip(),
        )

    def cached_followup_context(self, query: str, *, scope: str) -> str:
        if self._has_strong_search_intent(query):
            return ""
        if self.should_refresh_followup(query):
            return ""
        if not self.is_followup_query(query):
            return ""
        cached = self._cache.get(scope)
        if not cached:
            return ""
        age_seconds = (datetime.now().astimezone() - cached.created_at).total_seconds()
        if age_seconds > 1800:
            logger.info("联网搜索追问缓存过期: scope=%s age_seconds=%.0f", scope, age_seconds)
            return ""
        return self.format_context(
            query,
            cached.results_by_query,
            plan=SearchPlan(
                True,
                [search_query for search_query, _items in cached.results_by_query],
                f"当前问题是对上一轮联网结果的追问，优先复用缓存结果；上一轮主题: {cached.topic}",
            ),
            cached_topic=cached.topic,
            previous_reply=cached.last_reply,
        )

    def is_followup_query(self, query: str) -> bool:
        text = _normalize_query(query)
        if not text:
            return False
        if any(keyword in text for keyword in self.FOLLOWUP_REFERENCE_KEYWORDS):
            return True
        return len(text) <= 12 and any(term in text for term in ("它", "他", "她", "这个", "那个", "这", "那"))

    def should_refresh_followup(self, query: str) -> bool:
        text = _normalize_query(query)
        return any(keyword in text for keyword in self.FOLLOWUP_REFRESH_KEYWORDS)

    def plan_search(
        self,
        query: str,
        *,
        messages: Optional[Sequence[dict]] = None,
        llm_client: Any = None,
        scope: Optional[str] = None,
    ) -> SearchPlan:
        """让当前 LLM 判断是否需要联网；需要时直接使用完整原问题搜索。"""
        if not self.config.auto_detect:
            return SearchPlan(True, [query], "auto_detect=false，直接使用原问题搜索")

        if llm_client is not None:
            try:
                plan = self._plan_search_with_llm(query, messages=messages, llm_client=llm_client)
                if plan.need_search and self.should_refresh_followup(query):
                    followup_plan = self._followup_realtime_plan(query, messages, reason=plan.reason, scope=scope)
                    if followup_plan.need_search:
                        return followup_plan
                if not plan.need_search and self._has_strong_search_intent(query):
                    return self._fallback_plan(query, reason=f"LLM 判定无需搜索，但命中强实时关键词: {plan.reason}")
                if not plan.need_search:
                    followup_plan = self._followup_realtime_plan(query, messages, reason=plan.reason, scope=scope)
                    if followup_plan.need_search:
                        return followup_plan
                return plan
            except Exception as exc:
                logger.warning("联网搜索规划失败，回退关键词规则: query=%s error=%s", query[:80], exc)

        fallback = self._fallback_plan(query, reason="关键词规则 fallback")
        if fallback.need_search:
            return fallback
        return self._followup_realtime_plan(query, messages, reason=fallback.reason, scope=scope)

    def _plan_search_with_llm(
        self,
        query: str,
        *,
        messages: Optional[Sequence[dict]],
        llm_client: Any,
    ) -> SearchPlan:
        now = datetime.now().astimezone()
        context_text = self._planner_context(messages)
        user_prompt = (
            f"当前日期时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            "当前时区：Asia/Shanghai（UTC+8）\n"
            f"群聊最近上下文：\n{context_text}\n\n"
            f"当前用户消息：{query}\n\n"
            "群聊最近上下文只用于理解“刚才、最近、这个、它、那”等指代，不要把上下文里的旧问题当成当前问题。\n"
            "如果当前用户消息是在追问上一条实时新闻、行情、价格或搜索结论的时间、来源、具体日期，也应联网核验。\n"
            "请只判断是否需要联网辅助查询，不要生成或改写搜索词。"
        )
        raw = llm_client.chat(
            [{"role": "user", "content": user_prompt}],
            system_prompt=self._planner_system_prompt(),
        )
        data = _extract_json_object(raw)
        need_search = _as_bool(data.get("need_search"))
        reason = str(data.get("reason", "") or "").strip()
        return SearchPlan(need_search, [query] if need_search else [], reason)

    def _fallback_plan(self, query: str, *, reason: str) -> SearchPlan:
        needs = self.needs_search(query)
        logger.info("联网搜索关键词匹配: query=%s needs_search=%s auto_detect=%s", query[:120], needs, self.config.auto_detect)
        if not needs:
            return SearchPlan(False, [], reason)
        return SearchPlan(True, [query], reason)

    def needs_search(self, query: str) -> bool:
        if not self.config.auto_detect:
            return True
        text = str(query or "").lower()
        return any(keyword.lower() in text for keyword in self.REALTIME_KEYWORDS)

    def _has_strong_search_intent(self, query: str) -> bool:
        text = _normalize_query(query)
        return any(keyword in text for keyword in self.STRONG_SEARCH_KEYWORDS)

    def _followup_realtime_plan(
        self,
        query: str,
        messages: Optional[Sequence[dict]],
        *,
        reason: str,
        scope: Optional[str] = None,
    ) -> SearchPlan:
        normalized_query = _normalize_query(query)
        if not any(keyword in normalized_query for keyword in self.FOLLOWUP_SEARCH_KEYWORDS):
            return SearchPlan(False, [], reason)

        cached = self._cache.get(str(scope or "default"))
        topic = cached.topic if cached else ""
        if not topic:
            topic = self._recent_realtime_topic(messages)
        if not topic:
            return SearchPlan(False, [], reason)

        now = datetime.now().astimezone()
        search_query = f"{now.year}年{now.month}月{now.day}日 {topic} {query}"
        return SearchPlan(
            True,
            [search_query],
            f"当前问题是对上一轮实时信息的追问，需要联网核验日期/来源；原规划原因: {reason}",
        )

    def _recent_realtime_topic(self, messages: Optional[Sequence[dict]]) -> str:
        if not messages:
            return ""
        user_candidates: List[str] = []
        assistant_candidates: List[str] = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = " ".join(str(message.get("content", "")).split())
            if not content:
                continue
            normalized = _normalize_query(content)
            if any(keyword in normalized for keyword in self.STRONG_SEARCH_KEYWORDS):
                if role == "user":
                    user_candidates.append(content)
                else:
                    assistant_candidates.append(content)
        candidates = user_candidates or assistant_candidates
        if not candidates:
            return ""
        return _compact_topic(candidates[-1], max_len=80)

    def local_datetime_context(self, query: str) -> str:
        """时间/日期类问题直接使用本机时间，避免搜索引擎缓存导致错误。"""
        text = _normalize_query(query)
        datetime_patterns = (
            "现在几点",
            "几点了",
            "当前时间",
            "现在时间",
            "北京时间",
            "今天是哪一天",
            "今天几号",
            "今天星期几",
            "今天是星期几",
            "今天日期",
            "今天几月几日",
            "昨天是哪一天",
            "昨天几号",
            "昨天星期几",
            "昨天是星期几",
            "昨天日期",
            "昨天几月几日",
            "明天是哪一天",
            "明天几号",
            "明天星期几",
            "明天是星期几",
            "明天日期",
            "明天几月几日",
            "前天是哪一天",
            "前天几号",
            "前天星期几",
            "前天是星期几",
            "后天是哪一天",
            "后天几号",
            "后天星期几",
            "后天是星期几",
        )
        if not any(pattern in text for pattern in datetime_patterns):
            return ""

        # 同时问天气、价格、新闻等实时外部信息时仍走搜索。
        external_keywords = (
            "天气",
            "新闻",
            "热搜",
            "股价",
            "股票",
            "汇率",
            "价格",
            "多少钱",
            "政策",
            "比赛",
            "赛事",
            "赛程",
            "日程",
            "航班",
            "限行",
            "地震",
            "台风",
            "油价",
            "金价",
            "票房",
            "榜单",
        )
        if any(keyword in text for keyword in external_keywords):
            return ""

        now = datetime.now().astimezone()
        date_lines = [self._relative_date_line("今天", now, 0)]
        if "昨天" in text:
            date_lines.append(self._relative_date_line("昨天", now, -1))
        if "明天" in text:
            date_lines.append(self._relative_date_line("明天", now, 1))
        if "前天" in text:
            date_lines.append(self._relative_date_line("前天", now, -2))
        if "后天" in text:
            date_lines.append(self._relative_date_line("后天", now, 2))
        return (
            "当前问题只是在询问日期或时间，不需要联网搜索。"
            "请直接基于下面的本机当前时间回答，不要引用搜索结果，也不要说无法联网。\n"
            f"当前本机时间：{now.strftime('%Y年%m月%d日 %H:%M:%S')}\n"
            + "\n".join(date_lines)
            + "\n"
            "时区：北京时间/Asia/Shanghai（UTC+8）\n"
            "回复要像微信群消息，简短自然。"
        )

    @staticmethod
    def _relative_date_line(label: str, now: datetime, day_offset: int) -> str:
        target = now + timedelta(days=day_offset)
        weekday = "一二三四五六日"[target.weekday()]
        return f"{label}：{target.strftime('%Y年%m月%d日')}，星期{weekday}"

    def format_context(
        self,
        query: str,
        results_by_query: List[tuple[str, List[Dict[str, Any]]]],
        *,
        plan: Optional[SearchPlan] = None,
        cached_topic: str = "",
        previous_reply: str = "",
    ) -> str:
        all_results = [item for _search_query, items in results_by_query for item in items]
        if not all_results:
            return (
                "已尝试联网搜索，但没有拿到可靠结果。"
                "请不要编造实时数据；如果问题依赖实时信息，请简短说明查不到准确信息。"
                "不要说自己没有搜索或没有联网，因为搜索已经执行过，只是没有可靠结果。"
            )

        lines = [
            "重要：本轮已经启用联网搜索。最终回复必须完全来自下面的百度搜索结果。",
            "你不是在重新回答问题，而是在把百度搜索结果转换成适合微信群发送的文本。",
            "只允许做这些处理：去掉 Markdown/链接噪音、合并重复项、按用户问题裁剪长度、改成自然微信口吻。",
            "禁止补充搜索结果以外的事实、判断、背景、原因或你自己的推断；禁止把结果改写成搜索结果里没有的新结论。",
            "百度结果能直接回答时，尽量保留原文、原排名、原数值、原日期和原标题；只有原文太长时才压缩。",
            "压缩时也必须保留关键原文信息，比如排名、热度、标题、日期、价格、点位，不能改成泛泛概括。",
            "如果搜索结果没有直接答案，就说“百度结果里没看到准确信息”，不要编造。",
            "不要说自己无法联网、离线、不能访问实时数据或不能查询价格。",
            self._answer_style_instruction(query),
            "如果这是追问，并且提供了“上一轮已发给用户的回复”，用户说的“第一条/第二条/这个/那个”优先指上一轮回复里的条目。",
            "天气问题如果没有明确城市，不要默认使用本机位置，应简短追问城市。",
            f"用户原问题：{query}",
            f"追问对应的上一轮主题：{cached_topic}" if cached_topic else "",
            f"上一轮已发给用户的回复：{previous_reply}" if previous_reply else "",
            f"搜索规划原因：{(plan.reason if plan else '') or '未提供'}",
            f"实际搜索词：{', '.join(search_query for search_query, _items in results_by_query)}",
            "搜索结果：",
        ]
        lines = [line for line in lines if line]
        index = 1
        for search_query, items in results_by_query:
            lines.append(f"搜索词：{search_query}")
            for item in items:
                title = _first_text(item, "title", "name", "site_name") or "无标题"
                url = _first_text(item, "url", "link", "page_url", "refer_url")
                content = _first_text(
                    item,
                    "content",
                    "summary",
                    "answer",
                    "result",
                    "text",
                    "snippet",
                    "description",
                    "abstract",
                )
                date = _first_text(item, "date", "time", "page_time", "publish_time")
                parts = [f"{index}. {title}"]
                if date:
                    parts.append(f"时间：{date}")
                if url:
                    parts.append(f"链接：{url}")
                if content:
                    parts.append(f"摘要：{_truncate(content, self._content_limit_for_query(query))}")
                lines.append("\n".join(parts))
                index += 1
        return "\n\n".join(lines)

    @staticmethod
    def _planner_system_prompt() -> str:
        return (
            "你是联网判断器。你的任务是判断用户消息是否需要联网辅助。\n"
            "只返回一个 JSON 对象，不要 Markdown，不要解释。\n"
            "返回格式必须是：{\"need_search\": true/false, \"reason\": \"...\"}\n"
            "需要联网：天气、新闻、价格、股价、汇率、政策、赛事、航班、限行、最新事件、用户明确说查一下。\n"
            "不需要联网：普通聊天、写作、翻译、解释常识、只问当前时间或日期。\n"
            "追问规则：如果当前消息问“最近是指什么时候”“有具体日期吗”“来源是哪”“具体是哪天”等，"
            "且上下文里的上一轮主题是新闻、行情、价格、股价、政策等实时信息，应返回 need_search=true。\n"
            "注意：不要输出 queries、search_query、keywords 等字段；搜索会直接使用当前用户完整问题。"
        )

    @staticmethod
    def _planner_context(messages: Optional[Sequence[dict]]) -> str:
        if not messages:
            return "无"
        snippets = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            content = " ".join(str(message.get("content", "")).split())
            if not content:
                continue
            snippets.append(f"{role}: {_truncate(content, 180)}")
        return "\n".join(snippets[-6:]) or "无"

    def _search_count_for_query(self, query: str) -> int:
        normalized = _normalize_query(query)
        count = self.config.count
        if "新闻" in normalized or "大事" in normalized:
            count = max(count, 3)
        if "热搜" in normalized or "榜单" in normalized or "前十" in normalized or "top10" in normalized:
            count = max(count, 10)
        if any(word in normalized for word in ("股票", "股市", "a股", "大盘", "上证", "沪指", "深成指", "创业板", "指数", "点位", "收盘")):
            count = max(count, 3)
        return count

    @staticmethod
    def _answer_style_instruction(query: str) -> str:
        normalized = _normalize_query(query)
        if "热搜" in normalized or "榜单" in normalized or "前十" in normalized or "top10" in normalized:
            return (
                "用户要榜单/前十/热搜时，不要只概括；应按搜索结果列出最多 10 条，"
                "每条保留热搜词和可见热度/排名。没有足够 10 条就说明只查到这些。"
            )
        if "新闻" in normalized or "大事" in normalized:
            return (
                "用户问新闻/大事时，不要说“没看到大新闻”这类空泛判断；"
                "应从搜索结果中提炼 3 到 5 条具体新闻标题或事件，简短列出。"
            )
        return "回复要像微信群消息，优先 1 到 2 句话；可以给出最相关的价格、时间、来源或链接名称。"

    @staticmethod
    def _content_limit_for_query(query: str) -> int:
        normalized = _normalize_query(query)
        if "热搜" in normalized or "榜单" in normalized or "前十" in normalized or "top10" in normalized:
            return 900
        if "新闻" in normalized or "大事" in normalized:
            return 520
        return 360

    def _rank_results(self, original_query: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not results:
            return results
        scored = [
            (self._result_score(original_query, item), index, item)
            for index, item in enumerate(results)
        ]
        scored.sort(key=lambda row: (-row[0], row[1]))
        ranked = [item for score, _index, item in scored if score > -5]
        return ranked or results

    def _result_score(self, original_query: str, item: Dict[str, Any]) -> int:
        title = _first_text(item, "title", "name", "site_name")
        content = _first_text(item, "content", "summary", "snippet", "description", "abstract")
        text = _normalize_query(f"{title} {content}")
        query_text = _normalize_query(original_query)
        score = 0

        for keyword in _core_keywords(original_query):
            if keyword and keyword in text:
                score += 4

        if _is_market_index_query(query_text):
            market_good_terms = (
                "a股指数",
                "上证指数",
                "上证综合指数",
                "沪指",
                "深证成指",
                "深成指",
                "创业板指",
                "三大指数",
                "收盘",
                "点位",
            )
            for term in market_good_terms:
                if term in text:
                    score += 5
            if re.search(r"\d{3,5}\.\d{1,2}", f"{title} {content}"):
                score += 3
            market_bad_terms = (
                "个股",
                "最新价格",
                "条件选股",
                "自选股",
                "创新药",
                "医药",
                "基金",
            )
            for term in market_bad_terms:
                if term in text:
                    score -= 4
            if "东方财富" in title and not any(term in text for term in ("a股指数", "上证", "沪指", "深成指", "创业板")):
                score -= 5

        return score


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _positive_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start : end + 1]
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    raise ValueError(f"无法解析搜索规划 JSON: {_truncate(raw, 200)}")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "需要", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "不需要", "否"}:
            return False
    return bool(value)


def _first_text(data: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _truncate(text: str, max_len: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _compact_topic(text: str, *, max_len: int) -> str:
    topic = re_sub_mentions(str(text or ""))
    for prefix in ("user:", "assistant:", "群成员说:", "当前提问者说:"):
        if topic.startswith(prefix):
            topic = topic[len(prefix):].strip()
    if "说:" in topic:
        topic = topic.split("说:", 1)[1].strip()
    return _truncate(topic, max_len).strip(" ，,。；;：:")


def re_sub_mentions(text: str) -> str:
    parts = []
    for part in str(text or "").split():
        if part.startswith("@"):
            continue
        parts.append(part)
    return " ".join(parts).strip()


def _core_keywords(text: str) -> List[str]:
    normalized = _normalize_query(text)
    keywords = []
    important_terms = (
        "天气",
        "新闻",
        "热搜",
        "股价",
        "股票",
        "股市",
        "美股",
        "a股",
        "港股",
        "行情",
        "大盘",
        "上证",
        "沪指",
        "深成指",
        "创业板",
        "指数",
        "点位",
        "多少",
        "多少点",
        "收盘",
        "汇率",
        "价格",
        "多少钱",
        "最便宜",
        "政策",
        "比赛",
        "赛事",
        "赛程",
        "航班",
        "限行",
    )
    for term in important_terms:
        if term in normalized and term not in keywords:
            keywords.append(term)
    return keywords[:4]


def _query_covers_keywords(query: str, keywords: Sequence[str]) -> bool:
    normalized = _normalize_query(query)
    return all(keyword in normalized for keyword in keywords)


def _is_market_index_query(normalized_query: str) -> bool:
    has_market = any(term in normalized_query for term in ("a股", "大a", "大盘", "股市", "上证", "沪指", "指数"))
    asks_index = any(term in normalized_query for term in ("指数", "大盘", "多少", "点位", "多少点", "收盘"))
    return has_market and asks_index


def _normalize_query(query: str) -> str:
    return (
        str(query or "")
        .lower()
        .replace(" ", "")
        .replace("\u2005", "")
        .replace("\u00a0", "")
        .replace("，", "")
        .replace(",", "")
        .replace("。", "")
        .replace("？", "")
        .replace("?", "")
        .replace("！", "")
        .replace("!", "")
    )
