# -*- coding: utf-8 -*-
"""通用 AI 调用模块。

目标：
    让自动回复场景只需要传入 base_url、api_format、model、api_key 即可使用。

支持格式：
    - completions: OpenAI-compatible /chat/completions
    - responses: OpenAI Responses API
    - anthropic: Anthropic-compatible /messages
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional

from .features.messaging.listener import MessageEvent

ApiFormat = Literal["completions", "responses", "anthropic"]
_MISSING = object()


DEFAULT_SYSTEM_PROMPT = """你正在微信群聊里聊天，请像微信里的真人朋友一样回复。
要求：
1. 回复要短、自然、口语化，优先 1 到 2 句话，不要长篇解释。
2. 不要使用 Markdown，不要标题、列表、加粗、引用块、代码块或表格。
3. 不要说自己是 AI、机器人或模型；被问到身份、模型、开发公司时，不要编造具体公司或模型名，不清楚就说不太清楚。
4. 不确定时就简短说明不知道或不太确定，绝对不要编造具体数据、来源、时间、公司、人名、政策或价格。
5. 如果消息不需要回复，可以只返回空字符串。
6. 用户消息里的“张三说:”表示发言人是张三，不是你的身份；不要冒充发言人。
7. 不要主动在回复开头 @ 对方，发送程序会自动添加 @。
8. 语气可以轻松一点，但不要油腻，不要每句都带感叹号。
9. 如果用户问“上一句/刚才说了什么”，只根据当前上下文回答；上下文没有就说没看到。
10. 不同微信群聊的上下文必须完全隔离，不要引用其他群聊里的任何消息。
11. 遇到天气、新闻、股价、价格、政策、赛事、日程等实时问题，如果你具备联网或检索能力，可以先查询再回答；如果查不到可靠信息，就直接说“我这边查不到准确信息”。
12. 联网查询后的回答也要适合微信聊天，简短说明结果即可，不要堆链接和大段资料。
"""


def _config_value(data: dict, key: str, env_prefix: str, default=_MISSING):
    env_key = f"{env_prefix}_{key}".upper()
    generic_env_key = f"AI_{key}".upper()
    if os.environ.get(env_key) not in (None, ""):
        return os.environ[env_key]
    if os.environ.get(generic_env_key) not in (None, ""):
        return os.environ[generic_env_key]
    value = data.get(key, default)
    if value in (None, "", _MISSING) and default is _MISSING:
        raise ValueError(f"AI 配置缺少必填项: {key}")
    if value is _MISSING:
        return None
    return value


def _optional_bool(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


@dataclass(frozen=True)
class AIConfig:
    """AI 接口配置。"""

    base_url: str
    model: str
    api_key: str
    api_format: ApiFormat = "completions"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.7
    max_tokens: int = 300
    timeout: float = 60.0
    enable_thinking: Optional[bool] = None

    @classmethod
    def from_file(
        cls,
        path: Optional[str] = None,
        *,
        provider: Optional[str] = None,
        env_prefix: Optional[str] = None,
    ) -> "AIConfig":
        """从本地 JSON 配置文件创建 AIConfig。

        默认读取当前工作目录的 ``wx4py_ai_config.json``，也可通过
        ``WX4PY_AI_CONFIG`` 指定路径。支持两种格式：

        1. 扁平格式：直接包含 base_url/api_key/model 等字段。
        2. providers 格式：{"default": "ark", "providers": {"ark": {...}}}

        环境变量可覆盖配置文件，前缀由 ``env_prefix`` 或 provider 推断。
        例如 provider="ark" 时会读取 ARK_API_KEY/ARK_BASE_URL/ARK_MODEL。
        """
        config_path = Path(
            path
            or os.environ.get("WX4PY_AI_CONFIG", "")
            or (Path.cwd() / "wx4py_ai_config.json")
        )
        if not config_path.exists():
            raise FileNotFoundError(f"AI 配置文件不存在: {config_path}")

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"AI 配置文件 JSON 格式错误: {config_path}: {exc}") from exc

        selected_provider = provider or data.get("default")
        if "providers" in data:
            providers = data.get("providers") or {}
            if not selected_provider:
                if len(providers) == 1:
                    selected_provider = next(iter(providers))
                else:
                    raise ValueError("AI 配置包含多个 providers，请设置 default 或传入 provider")
            try:
                data = providers[selected_provider]
            except KeyError as exc:
                raise ValueError(f"AI provider 不存在: {selected_provider}") from exc

        prefix = env_prefix or (str(selected_provider).upper() if selected_provider else "AI")
        return cls(
            base_url=_config_value(data, "base_url", prefix),
            model=_config_value(data, "model", prefix),
            api_key=_config_value(data, "api_key", prefix),
            api_format=_config_value(data, "api_format", prefix, default="completions"),
            system_prompt=_config_value(data, "system_prompt", prefix, default=DEFAULT_SYSTEM_PROMPT),
            temperature=float(_config_value(data, "temperature", prefix, default=0.7)),
            max_tokens=int(_config_value(data, "max_tokens", prefix, default=300)),
            timeout=float(_config_value(data, "timeout", prefix, default=60.0)),
            enable_thinking=_optional_bool(_config_value(data, "enable_thinking", prefix, default=None)),
        )


class AIClient:
    """轻量级 AI 客户端。"""

    def __init__(self, config: AIConfig):
        self.config = config
        self.api_format = self._normalize_api_format(config.api_format)
        self.url = self._build_endpoint(config.base_url, self.api_format)

    def chat(self, messages: List[dict], system_prompt: Optional[str] = None) -> str:
        """发送对话并返回文本回复。"""
        request = self._build_request(messages, system_prompt or self.config.system_prompt)
        headers = self._build_headers()

        http_request = urllib.request.Request(
            url=self.url,
            data=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_http_error(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.gaierror):
                raise RuntimeError(
                    f"AI 接口域名解析失败，请检查网络、DNS、代理或 base_url: {self.config.base_url}"
                ) from exc
            raise RuntimeError(f"AI 接口网络请求失败: {reason}") from exc

        result = self._extract_text(data)
        if not result:
            raise RuntimeError(f"AI 接口返回为空: {json.dumps(data, ensure_ascii=False)}")
        return self._sanitize_output(result)

    def _build_request(self, messages: List[dict], system_prompt: str) -> dict:
        if self.api_format == "completions":
            request = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *messages,
                ],
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
            }
            if self.config.enable_thinking is not None:
                request["enable_thinking"] = self.config.enable_thinking
            return request

        if self.api_format == "responses":
            return {
                "model": self.config.model,
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": system_prompt}],
                    },
                    *[
                        {
                            "role": message["role"],
                            "content": [{"type": "input_text", "text": message["content"]}],
                        }
                        for message in messages
                    ],
                ],
                "temperature": self.config.temperature,
                "max_output_tokens": self.config.max_tokens,
            }

        if self.api_format == "anthropic":
            return {
                "model": self.config.model,
                "system": system_prompt,
                "messages": messages,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
            }

        raise ValueError(f"不支持的 api_format: {self.api_format}")

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            "Authorization": f"Bearer {self.config.api_key}",
        }

        if self.api_format == "anthropic":
            headers.pop("Authorization", None)
            headers["x-api-key"] = self.config.api_key
            headers["anthropic-version"] = "2023-06-01"

        return headers

    def _extract_text(self, data: dict) -> str:
        if self.api_format == "completions":
            return (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

        if self.api_format == "responses":
            if data.get("output_text"):
                return data["output_text"]
            for item in data.get("output", []) or []:
                for content in item.get("content", []) or []:
                    if content.get("type") == "output_text" and content.get("text"):
                        return content["text"]
            return ""

        if self.api_format == "anthropic":
            return "\n".join(
                item.get("text", "")
                for item in data.get("content", []) or []
                if item.get("type") == "text" and item.get("text")
            )

        return ""

    def _format_http_error(self, status: int, body: str) -> str:
        lower = body.lower()
        if status in (401, 403) and any(word in lower for word in ("api key", "apikey", "auth", "unauthorized", "permission")):
            return f"AI 认证失败，请检查 api_key。HTTP {status}: {body}"
        if status == 404:
            return f"AI endpoint 不存在，请检查 base_url 或 api_format。URL={self.url} HTTP {status}: {body}"
        if "model" in lower and any(word in lower for word in ("not found", "invalid", "not exist", "unsupported")):
            return f"AI 模型不可用，请检查 model。HTTP {status}: {body}"
        return f"AI HTTP 请求失败。URL={self.url} HTTP {status}: {body}"

    @staticmethod
    def _normalize_api_format(api_format: str) -> ApiFormat:
        if api_format == "response":
            return "responses"
        if api_format not in {"completions", "responses", "anthropic"}:
            raise ValueError("api_format must be one of: completions, responses, anthropic")
        return api_format  # type: ignore[return-value]

    @staticmethod
    def _build_endpoint(base_url: str, api_format: ApiFormat) -> str:
        if not base_url or not base_url.strip():
            raise ValueError("base_url must not be empty")

        normalized = base_url.strip()
        if not normalized.lower().startswith(("http://", "https://")):
            normalized = f"https://{normalized}"
        normalized = normalized.rstrip("/")
        path = AIClient._get_url_path(normalized)

        if api_format == "completions":
            if AIClient._has_path_suffix(path, ["/chat/completions", "/v1/chat/completions", "/completions", "/v1/completions"]):
                return normalized
            if AIClient._has_path_suffix(path, ["/v1"]):
                return f"{normalized}/chat/completions"
            if path:
                return f"{normalized}/chat/completions"
            return f"{normalized}/v1/chat/completions"

        if api_format == "responses":
            if AIClient._has_path_suffix(path, ["/responses", "/v1/responses"]):
                return normalized
            if AIClient._has_path_suffix(path, ["/v1"]):
                return f"{normalized}/responses"
            return f"{normalized}/v1/responses"

        if api_format == "anthropic":
            if AIClient._has_path_suffix(path, ["/messages", "/v1/messages"]):
                return normalized
            if AIClient._has_path_suffix(path, ["/v1"]):
                return f"{normalized}/messages"
            return f"{normalized}/v1/messages"

        raise ValueError(f"不支持的 api_format: {api_format}")

    @staticmethod
    def _get_url_path(url: str) -> str:
        marker = "://"
        if marker not in url:
            return ""
        path_start = url.find("/", url.find(marker) + len(marker))
        return url[path_start:] if path_start >= 0 else ""

    @staticmethod
    def _has_path_suffix(path: str, suffixes: List[str]) -> bool:
        return any(path == suffix or path.endswith(suffix) for suffix in suffixes)

    @staticmethod
    def _sanitize_output(text: str) -> str:
        return str(text or "").strip().strip("\"'")


class AIResponder:
    """面向微信群自动回复的 AI 回调封装。"""

    def __init__(
        self,
        client: AIClient,
        *,
        context_size: int = 8,
        reply_on_at: bool = True,
        max_reply_chars: int = 180,
    ):
        self.client = client
        self.context_size = context_size
        self.reply_on_at = reply_on_at
        self.max_reply_chars = max(20, int(max_reply_chars))
        self.contexts: Dict[str, List[dict]] = {}

    def __call__(self, event: MessageEvent) -> str:
        if self.reply_on_at and not event.is_at_me:
            return ""

        raw_content = str(event.content or "").strip()
        content = self._strip_at(raw_content, event.group_nickname)
        if not content:
            return ""

        context_key = self._context_key(event)
        context = self.contexts.setdefault(context_key, [])
        context.append({"role": "user", "content": self._format_user_content(event, raw_content)})
        del context[:-self.context_size]

        messages = [self._build_group_scope_message(event, self.max_reply_chars), *context]
        reply = self._sanitize_reply(
            self.client.chat(messages),
            group_nickname=event.group_nickname,
            max_chars=self.max_reply_chars,
        )
        if reply:
            context.append({"role": "assistant", "content": reply})
            del context[:-self.context_size]
        return reply

    @staticmethod
    def _strip_at(content: str, nickname: Optional[str]) -> str:
        if not nickname:
            return content.strip()
        return (
            content
            .replace(f"@{nickname}\u2005", "")
            .replace(f"@{nickname}", "")
            .strip()
        )

    @staticmethod
    def _format_user_content(event: MessageEvent, content: str) -> str:
        sender = (event.sender_nickname or "").strip()
        if sender and sender not in {"我", "你"}:
            return f"{sender}说: {content}"
        return f"群成员说: {content}"

    @staticmethod
    def _context_key(event: MessageEvent) -> str:
        return f"{event.group}\n{event.group_nickname or ''}"

    @staticmethod
    def _build_group_scope_message(event: MessageEvent, max_reply_chars: int = 180) -> dict:
        nickname = event.group_nickname or "当前登录账号"
        return {
            "role": "system",
            "content": (
                f"当前只允许参考这个微信群的上下文。\n"
                f"群聊名：{event.group}\n"
                f"本账号在群里的昵称：{nickname}\n"
                f"本次回复请控制在 {max_reply_chars} 个中文字符以内，优先自然收尾，不要靠突然截断变短。\n"
                "如果用户问上一句、刚才、前面的人说了什么，只能回答本群上下文里的内容；"
                "不要使用其他群聊或服务端记忆。"
            ),
        }

    @staticmethod
    def _sanitize_reply(text: str, group_nickname: Optional[str] = None, max_chars: int = 180) -> str:
        """清理模型偶发的 Markdown 痕迹，让回复更像微信文本。"""
        lines = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = line.strip("*_`#> ")
            for prefix in ("- ", "* ", "• "):
                if line.startswith(prefix):
                    line = line[len(prefix):].strip()
            if len(line) > 2 and line[0].isdigit() and line[1] in {".", "、"}:
                line = line[2:].strip()
            lines.append(line)
        reply = " ".join(lines).strip()
        reply = reply.replace("**", "").replace("__", "").replace("`", "")
        if group_nickname:
            reply = (
                reply
                .replace(f"@{group_nickname}\u2005", "")
                .replace(f"@{group_nickname} ", "")
                .replace(f"@{group_nickname}", "")
            )
        max_chars = max(20, int(max_chars or 180))
        if len(reply) > max_chars:
            reply = AIResponder._trim_at_sentence_boundary(reply, max_chars)
        return reply.strip()

    @staticmethod
    def _trim_at_sentence_boundary(text: str, max_chars: int) -> str:
        """兜底截断时尽量停在自然标点，减少突兀感。"""
        if len(text) <= max_chars:
            return text
        clipped = text[:max_chars].rstrip()
        boundary = max(
            clipped.rfind("。"),
            clipped.rfind("！"),
            clipped.rfind("？"),
            clipped.rfind("."),
            clipped.rfind("!"),
            clipped.rfind("?"),
        )
        if boundary >= max(20, int(max_chars * 0.55)):
            return clipped[:boundary + 1].strip()
        return clipped.rstrip("，、；;：:, ") + "..."
