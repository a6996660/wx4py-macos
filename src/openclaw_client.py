# -*- coding: utf-8 -*-
"""OpenClaw 本地 Agent 调用封装。

通过 subprocess 调用 ``openclaw agent --local --json`` 获取 agent 回复。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class OpenClawError(RuntimeError):
    """OpenClaw 调用失败。"""


class OpenClawNotFoundError(OpenClawError):
    """OpenClaw CLI 未安装。"""


class OpenClawTimeoutError(OpenClawError):
    """OpenClaw 调用超时。"""


class OpenClawAgentError(OpenClawError):
    """OpenClaw agent 执行失败。"""


@dataclass(frozen=True)
class OpenClawConfig:
    """OpenClaw 调用配置。"""

    enabled: bool = False
    agent_id: str = "main"
    prefixes: List[str] = field(default_factory=lambda: ["/claw", "\\claw"])
    timeout: float = 60.0
    fallback_to_llm: bool = True
    session_per_group: bool = True
    placeholder_text: str = "让我想想…"
    placeholder_reply_on_failure: str = "这个我直接来答吧"
    cli_path: str = ""  # 空字符串表示使用 PATH 查找

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "OpenClawConfig":
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=bool(data.get("enabled", False)),
            agent_id=str(data.get("agent_id", "main")),
            prefixes=list(data.get("prefixes", ["/claw", "\\claw"])),
            timeout=float(data.get("timeout", 60.0)),
            fallback_to_llm=bool(data.get("fallback_to_llm", True)),
            session_per_group=bool(data.get("session_per_group", True)),
            placeholder_text=str(data.get("placeholder_text", "让我想想…")),
            placeholder_reply_on_failure=str(
                data.get("placeholder_reply_on_failure", "这个我直接来答吧")
            ),
            cli_path=str(data.get("cli_path", "")),
        )


class OpenClawClient:
    """OpenClaw 本地 Agent 客户端。"""

    def __init__(self, config: OpenClawConfig):
        self.config = config
        self._cli = self._resolve_cli()
        self._check_binary()

    def _resolve_cli(self) -> str:
        """解析 OpenClaw CLI 可执行文件路径。"""
        if self.config.cli_path:
            return self.config.cli_path
        found = shutil.which("openclaw")
        if found:
            return found
        raise OpenClawNotFoundError(
            "OpenClaw CLI 未安装或不在 PATH。请执行: npm install -g openclaw"
        )

    def _check_binary(self) -> None:
        """验证 openclaw 命令是否可用。"""
        if not os.path.isfile(self._cli) and not shutil.which(self._cli):
            raise OpenClawNotFoundError(
                f"OpenClaw CLI 找不到: {self._cli}"
            )

    @staticmethod
    def check_health(cli_path: str = "") -> tuple[bool, str]:
        """启动时健康检查。

        Args:
            cli_path: OpenClaw CLI 可执行文件路径，空字符串表示使用 PATH 查找。

        Returns:
            (是否可用, 状态信息)
        """
        binary = cli_path if cli_path else shutil.which("openclaw")
        if not binary:
            return False, "OpenClaw CLI 未安装或不在 PATH"
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                timeout=10,
                check=True,
            )
            version = result.stdout.decode().strip().splitlines()[0]
            return True, version
        except Exception as exc:
            return False, f"OpenClaw 健康检查失败: {exc}"

    def run_agent(self, message: str, session_id: Optional[str] = None) -> str:
        """同步调用 OpenClaw 本地 agent，返回回复文本。

        Args:
            message: 要发送给 agent 的消息内容。
            session_id: 可选的会话 ID，用于上下文隔离。

        Returns:
            agent 的回复文本。

        Raises:
            OpenClawTimeoutError: 调用超时。
            OpenClawAgentError: agent 执行失败或返回无法解析。
        """
        cmd = [
            self._cli,
            "agent",
            "--local",
            "--agent",
            self.config.agent_id,
            "--message",
            message,
            "--json",
            "--timeout",
            str(int(self.config.timeout)),
        ]
        if session_id:
            cmd.extend(["--session-id", session_id])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.config.timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenClawTimeoutError(
                f"OpenClaw agent 调用超时（>{self.config.timeout}秒）"
            ) from exc

        return self._parse_result(result.stdout, result.stderr)

    @staticmethod
    def _parse_result(stdout: bytes, stderr: bytes) -> str:
        """解析 OpenClaw agent 输出，提取回复文本。

        解析优先级：
        1. 尝试直接解析 stdout 为 JSON
        2. 尝试从 stderr 的最后一行非空行解析 JSON
        3. 尝试从 stderr 提取结构化错误
        4. 兜底抛出异常
        """
        # 1. 尝试直接解析 stdout 为 JSON
        if stdout.strip():
            try:
                data = json.loads(stdout)
                return OpenClawClient._extract_reply(data)
            except (json.JSONDecodeError, KeyError):
                pass

        stderr_text = stderr.decode(errors="replace")

        # 2. OpenClaw 实际把 JSON 输出到 stderr，前面混有 config warnings。
        #    尝试从 stderr 中提取第一个 `{` 到最后一个 `}` 之间的完整 JSON。
        if stderr_text.strip():
            start = stderr_text.find("{")
            end = stderr_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = stderr_text[start : end + 1]
                try:
                    data = json.loads(candidate)
                    return OpenClawClient._extract_reply(data)
                except (json.JSONDecodeError, KeyError):
                    pass

        # 3. 尝试从 stderr 的最后一行非空行解析 JSON（单行 JSON 场景）
        for line in reversed(stderr_text.strip().splitlines()):
            line_stripped = line.strip()
            if line_stripped.startswith("{") or line_stripped.startswith("["):
                try:
                    data = json.loads(line_stripped)
                    return OpenClawClient._extract_reply(data)
                except (json.JSONDecodeError, KeyError):
                    pass

        # 4. 尝试从 stderr 提取结构化错误
        if "FailoverError" in stderr_text:
            raise OpenClawAgentError(
                f"OpenClaw agent 执行失败: {_truncate(stderr_text, 500)}"
            )
        if "missing scope" in stderr_text:
            raise OpenClawAgentError(
                f"OpenClaw 权限不足: {_truncate(stderr_text, 500)}"
            )
        if "timeout" in stderr_text.lower():
            raise OpenClawTimeoutError(
                f"OpenClaw agent 超时: {_truncate(stderr_text, 500)}"
            )

        # 5. 兜底
        raise OpenClawAgentError(
            f"无法解析 OpenClaw 输出: {_truncate(stderr_text, 500)}"
        )

    @staticmethod
    def _extract_reply(data: dict) -> str:
        """从 JSON 数据中提取回复文本。

        兼容 OpenClaw ``--json`` 输出的多种可能格式：
        - ``{"payloads": [{"text": "..."}]}`` （本地 agent 实际格式）
        - ``{"reply": "..."}`` / ``{"text": "..."}`` （常见简写）
        - ``{"output": [{"text": "..."}]}`` （嵌套结构）
        """
        # 1. OpenClaw 本地 agent 标准格式: payloads[0].text
        if "payloads" in data and isinstance(data["payloads"], list):
            for payload in data["payloads"]:
                if isinstance(payload, dict) and payload.get("text"):
                    return str(payload["text"]).strip()

        # 2. 顶层常见字段名
        for key in ("reply", "text", "content", "message", "output_text"):
            if key in data and isinstance(data[key], str):
                return data[key].strip()

        # 3. 嵌套 output 列表
        if "output" in data and isinstance(data["output"], list):
            for item in data["output"]:
                if isinstance(item, dict):
                    for key in ("text", "content", "output_text"):
                        if key in item and isinstance(item[key], str):
                            return item[key].strip()

        raise KeyError(f"JSON 中未找到回复字段，顶层 keys: {list(data.keys())}")


def _truncate(text: str, max_len: int) -> str:
    """截断文本，避免日志过长。"""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# HybridResponder — 双引擎 AI 回复器
# ---------------------------------------------------------------------------

import hashlib
from dataclasses import replace as dataclass_replace

from .ai import AIResponder
from .features.messaging.listener import MessageEvent
from .utils.logger import get_logger, log_error_audit

_hybrid_logger = get_logger(__name__ + ".hybrid")


class HybridResponder:
    """双引擎 AI 回复器：LLM 秒回 + OpenClaw agent 慢处理。

    根据消息前缀自动路由：
    - 无前缀 / 未匹配 → ``AIResponder``（秒回，像真人聊天）
    - 匹配前缀 → ``OpenClawClient.run_agent()``（慢但能力强）

    OpenClaw 失败时自动降级到 LLM，保证用户体验不中断。
    """

    def __init__(
        self,
        ai_responder: AIResponder,
        openclaw_client: Optional[OpenClawClient] = None,
        openclaw_config: Optional[OpenClawConfig] = None,
    ):
        self.ai_responder = ai_responder
        self.openclaw_client = openclaw_client
        self.openclaw_config = openclaw_config or OpenClawConfig()
        self._session_map: Dict[str, str] = {}
        self.reply_on_at = getattr(ai_responder, "reply_on_at", True)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def __call__(self, event: MessageEvent) -> str:
        """处理消息事件，返回回复文本。"""
        if self.reply_on_at and not event.is_at_me:
            return ""

        raw_content = str(event.content or "").strip()
        content = AIResponder._strip_at(raw_content, event.group_nickname)
        if not content:
            return ""

        has_prefix, clean_content = self._strip_prefix(content)

        # ---- OpenClaw 路径 ----
        if (
            has_prefix
            and self.openclaw_config.enabled
            and self.openclaw_client is not None
        ):
            session_id = self._get_session_id(event.group)
            _hybrid_logger.info(
                "OpenClaw 模式: group=%s session=%s content=%s",
                event.group,
                session_id,
                clean_content[:80],
            )
            try:
                reply = self.openclaw_client.run_agent(
                    clean_content, session_id=session_id
                )
                return self._sanitize_reply(reply)
            except OpenClawError as exc:
                _hybrid_logger.warning("OpenClaw 调用失败，准备降级: %s", exc)
                log_error_audit(
                    "openclaw_fallback",
                    {
                        "group": event.group,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "content": clean_content[:200],
                    },
                )
                if not self.openclaw_config.fallback_to_llm:
                    return self.openclaw_config.placeholder_reply_on_failure
                # 继续走 LLM 降级路径

        # ---- LLM 路径（含降级） ----
        if has_prefix and clean_content != content:
            # 前缀已去除，需要构造修改后的 event 避免 LLM 看到前缀
            event = self._replace_event_content(event, clean_content)
        return self.ai_responder(event)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _strip_prefix(self, content: str) -> tuple[bool, str]:
        """检测并去除 OpenClaw 前缀。

        Returns:
            (是否匹配前缀, 去除前缀后的内容)
        """
        stripped = content.strip()
        for prefix in self.openclaw_config.prefixes:
            if stripped.startswith(prefix):
                return True, stripped[len(prefix) :].strip()
        return False, content

    def _get_session_id(self, group: str) -> Optional[str]:
        """按群聊生成稳定的 session ID，确保上下文隔离。"""
        if not self.openclaw_config.session_per_group:
            return None
        if group not in self._session_map:
            self._session_map[group] = hashlib.sha256(
                group.encode("utf-8")
            ).hexdigest()[:16]
        return self._session_map[group]

    @staticmethod
    def _replace_event_content(event: MessageEvent, new_content: str) -> MessageEvent:
        """构造 content 被替换后的新事件（避免修改原对象）。"""
        # MessageEvent 可能不是标准 dataclass，直接通过构造函数重建
        kwargs = {
            "group": event.group,
            "content": new_content,
            "timestamp": event.timestamp,
            "group_nickname": event.group_nickname,
            "sender_nickname": event.sender_nickname,
            "is_at_me": event.is_at_me,
            "raw": None,
        }
        for attr in ("quoted_sender", "quoted_content"):
            if hasattr(event, attr):
                kwargs[attr] = getattr(event, attr)
        return MessageEvent(**kwargs)

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        """清洗 OpenClaw 回复中的 Markdown 痕迹，保持与 LLM 回复一致的风格。"""
        # 复用 AIResponder 的清洗逻辑
        return AIResponder._sanitize_reply(text, group_nickname=None, max_chars=0)

