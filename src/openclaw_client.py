# -*- coding: utf-8 -*-
"""OpenClaw 本地 Agent 调用封装。

通过 subprocess 调用 ``openclaw agent --json`` 获取 agent 回复（Gateway 模式，避免 --local 重复初始化开销）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union


class OpenClawError(RuntimeError):
    """OpenClaw 调用失败。"""


class OpenClawNotFoundError(OpenClawError):
    """OpenClaw CLI 未安装。"""


class OpenClawTimeoutError(OpenClawError):
    """OpenClaw 调用超时。"""


class OpenClawAgentError(OpenClawError):
    """OpenClaw agent 执行失败。"""


@dataclass(frozen=True)
class OpenClawResult:
    """OpenClaw agent 复合返回结果。"""

    text: str = ""
    file_paths: List[str] = field(default_factory=list)

    def has_files(self) -> bool:
        return bool(self.file_paths)


@dataclass(frozen=True)
class _OpenClawFileContext:
    """本次 OpenClaw 文件调用的本地上下文。"""

    started_at: float = 0.0
    original_file_path: str = ""
    workspace_input_path: str = ""


@dataclass(frozen=True)
class OpenClawConfig:
    """OpenClaw 调用配置。"""

    enabled: bool = False
    mode: str = "llm"
    agent_id: str = "main"
    prefixes: List[str] = field(default_factory=lambda: ["/claw", "\\claw"])
    timeout: float = 60.0
    fallback_to_llm: bool = True
    session_per_group: bool = True
    placeholder_text: str = "让我想想…"
    placeholder_reply_on_failure: str = "这个我直接来答吧"
    cli_path: str = ""  # 空字符串表示使用 PATH 查找
    file_support: bool = False  # 是否启用文件传递支持
    reset_commands: List[str] = field(default_factory=lambda: ["/new", "/reset"])
    reset_reply: str = "🆕 已重置对话，开始新会话～"

    def __post_init__(self) -> None:
        allowed_modes = {"llm", "hybrid", "openclaw"}
        mode = str(self.mode or "").strip().lower()
        if mode not in allowed_modes:
            mode = "hybrid" if self.enabled else "llm"
        if self.enabled and mode == "llm":
            mode = "hybrid"
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "enabled", mode != "llm")

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "OpenClawConfig":
        if not data:
            return cls(enabled=False)
        raw_mode = str(data.get("mode", "")).strip().lower()
        mode = raw_mode if raw_mode in {"llm", "hybrid", "openclaw"} else (
            "hybrid" if data.get("enabled", False) else "llm"
        )
        return cls(
            enabled=mode != "llm",
            mode=mode,
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
            file_support=bool(data.get("file_support", False)),
            reset_commands=list(data.get("reset_commands", ["/new", "/reset"])),
            reset_reply=str(data.get("reset_reply", "🆕 已重置对话，开始新会话～")),
        )


class OpenClawClient:
    """OpenClaw 本地 Agent 客户端。"""

    def __init__(self, config: OpenClawConfig):
        self.config = config
        self._cli = self._resolve_cli()
        self.last_file_context: Optional[_OpenClawFileContext] = None
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

    def run_agent(
        self,
        message: str,
        session_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> str:
        """同步调用 OpenClaw 本地 agent，返回回复文本。

        Args:
            message: 要发送给 agent 的消息内容。
            session_id: 可选的会话 ID，用于上下文隔离。
            file_path: 可选的附件文件路径，需要 OpenClaw CLI 支持 --file 参数。

        Returns:
            agent 的回复文本。

        Raises:
            OpenClawTimeoutError: 调用超时。
            OpenClawAgentError: agent 执行失败或返回无法解析。
        """
        result = self._run_agent_raw(message, session_id=session_id, file_path=file_path)
        return self._parse_result(result.stdout, result.stderr)

    def run_agent_full(
        self,
        message: str,
        session_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> OpenClawResult:
        """同步调用 OpenClaw 本地 agent，返回复合结果（含文件）。

        Args:
            message: 要发送给 agent 的消息内容。
            session_id: 可选的会话 ID，用于上下文隔离。
            file_path: 可选的附件文件路径。

        Returns:
            OpenClawResult: 包含文本回复和文件路径列表。

        Raises:
            OpenClawTimeoutError: 调用超时。
            OpenClawAgentError: agent 执行失败或返回无法解析。
        """
        result = self._run_agent_raw(message, session_id=session_id, file_path=file_path)
        return self._parse_result_full(result.stdout, result.stderr)

    def _run_agent_raw(
        self,
        message: str,
        session_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """执行 OpenClaw CLI 命令，返回原始 subprocess 结果。

        OpenClaw agent 没有 --file 参数，文件通过消息内容中的路径引用，
        由 agent 内置的 read 工具自行读取。为确保路径在白名单内，
        将文件复制到 OpenClaw workspace 后再引用。
        """
        # 处理文件：复制到 OpenClaw workspace，并在消息中嵌入路径
        effective_message = message
        call_started_at = time.time()
        self.last_file_context = None
        if file_path and self.config.file_support:
            workspace_input_path = ""
            try:
                workspace = Path.home() / ".openclaw" / "workspace"
                workspace.mkdir(parents=True, exist_ok=True)
                # 创建专用子目录避免污染 workspace 根目录
                dest_dir = workspace / ".wx4py_files"
                dest_dir.mkdir(exist_ok=True)
                src = Path(file_path)
                dest = dest_dir / src.name
                # 如果目标已存在，先尝试删除（避免文件被锁定导致 Permission denied）
                if dest.exists():
                    try:
                        dest.unlink()
                    except Exception:
                        pass
                shutil.copy2(str(src), str(dest))
                workspace_input_path = str(dest)
                # 使用相对于 workspace 的路径，agent 更容易解析
                rel_path = f".wx4py_files/{src.name}"
                effective_message = (
                    f"{message}\n\n"
                    f"附件：{src.name}\n"
                    f"路径：{rel_path}\n\n"
                    f"提示：如有修改或生成文件，请在回复中注明文件路径。"
                )
                _hybrid_logger.info(
                    "文件已复制到 OpenClaw workspace: %s -> %s",
                    file_path, dest,
                )
            except Exception as exc:
                _hybrid_logger.warning(
                    "复制文件到 OpenClaw workspace 失败: %s", exc,
                )
                # fallback：直接使用原始绝对路径，让 OpenClaw 尝试读取
                effective_message = (
                    f"{message}\n\n"
                    f"附件：{Path(file_path).name}\n"
                    f"路径：{file_path}\n\n"
                    f"提示：如有修改或生成文件，请在回复中注明文件路径。"
                )
            finally:
                self.last_file_context = _OpenClawFileContext(
                    started_at=call_started_at,
                    original_file_path=str(Path(file_path)),
                    workspace_input_path=workspace_input_path,
                )

        # 先尝试 Gateway 模式（无 --local，复用常驻服务，速度更快）
        result = self._exec_openclaw(
            effective_message, session_id, local=False
        )
        # Gateway 偶发返回空输出（服务瞬时不可用），fallback 到 --local 兜底
        if not result.stdout.strip():
            _hybrid_logger.warning(
                "Gateway 返回空输出，fallback 到 --local: session=%s", session_id
            )
            result = self._exec_openclaw(
                effective_message, session_id, local=True
            )
        return result

    def _exec_openclaw(
        self,
        effective_message: str,
        session_id: Optional[str],
        local: bool,
    ) -> subprocess.CompletedProcess:
        """执行一次 OpenClaw CLI 调用。"""
        cmd = [
            self._cli,
            "agent",
        ]
        if local:
            cmd.append("--local")
        cmd.extend([
            "--agent",
            self.config.agent_id,
            "--message",
            effective_message,
            "--json",
            "--timeout",
            str(int(self.config.timeout)),
        ])
        if session_id:
            cmd.extend(["--session-id", session_id])

        mode_label = "local" if local else "gateway"
        _hybrid_logger.info("OpenClaw CLI (%s): %s", mode_label, " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                timeout=self.config.timeout + 30,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenClawTimeoutError(
                f"OpenClaw agent ({mode_label}) 调用超时（>{self.config.timeout}秒）"
            ) from exc

    @staticmethod
    def _parse_result(stdout: bytes, stderr: bytes) -> str:
        """解析 OpenClaw agent 输出，提取回复文本。

        解析优先级：
        1. 尝试直接解析 stdout 为 JSON
        2. 尝试从 stderr 的最后一行非空行解析 JSON
        3. 尝试从 stderr 提取结构化错误
        4. 兜底抛出异常
        """
        result = OpenClawClient._parse_result_full(stdout, stderr)
        return result.text

    @staticmethod
    def _parse_result_full(stdout: bytes, stderr: bytes) -> OpenClawResult:
        """解析 OpenClaw agent 输出，提取复合结果（文本 + 文件）。

        解析优先级与 _parse_result 相同，但返回 OpenClawResult。
        """
        stdout_text = stdout.decode(errors="replace")

        # 1. 尝试直接解析 stdout 为 JSON
        if stdout.strip():
            try:
                data = json.loads(stdout)
                return OpenClawClient._extract_result_full(data)
            except (json.JSONDecodeError, KeyError):
                pass

            # 1b. stdout 可能混有 diagnostic 日志前缀，尝试截取 JSON 子串
            start = stdout_text.find("{")
            end = stdout_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = stdout_text[start : end + 1]
                try:
                    data = json.loads(candidate)
                    return OpenClawClient._extract_result_full(data)
                except (json.JSONDecodeError, KeyError):
                    pass

        stderr_text = stderr.decode(errors="replace")

        # 2. OpenClaw 实际把 JSON 输出到 stderr，前面混有 config warnings。
        if stderr_text.strip():
            start = stderr_text.find("{")
            end = stderr_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = stderr_text[start : end + 1]
                try:
                    data = json.loads(candidate)
                    return OpenClawClient._extract_result_full(data)
                except (json.JSONDecodeError, KeyError):
                    pass

        # 3. 尝试从 stderr 的最后一行非空行解析 JSON
        for line in reversed(stderr_text.strip().splitlines()):
            line_stripped = line.strip()
            if line_stripped.startswith("{") or line_stripped.startswith("["):
                try:
                    data = json.loads(line_stripped)
                    return OpenClawClient._extract_result_full(data)
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

        # 5. 兜底：记录原始输出前 200 字节以便排查
        _hybrid_logger.debug(
            "OpenClaw 解析失败: stdout=%s stderr=%s",
            _truncate(stdout_text, 200),
            _truncate(stderr_text, 200),
        )
        raise OpenClawAgentError(
            f"无法解析 OpenClaw 输出: stdout={_truncate(stdout_text, 200)} stderr={_truncate(stderr_text, 200)}"
        )

    @staticmethod
    def _extract_reply(data: dict) -> str:
        """从 JSON 数据中提取回复文本。

        兼容 OpenClaw ``--json`` 输出的多种可能格式：
        - ``{"payloads": [{"text": "..."}]}`` （本地 agent 实际格式）
        - ``{"reply": "..."}`` / ``{"text": "..."}`` （常见简写）
        - ``{"output": [{"text": "..."}]}`` （嵌套结构）
        """
        result = OpenClawClient._extract_result_full(data)
        return result.text

    @staticmethod
    def _extract_result_full(data: dict) -> OpenClawResult:
        """从 JSON 数据中提取复合结果（文本 + 文件路径）。

        兼容格式：
        - payloads 列表中同时包含 text 和 file 类型
        - 顶层 file_paths 列表
        - 传统纯文本格式（向后兼容）
        - Gateway 模式的嵌套结构 {"result": {"payloads": [...]}}
        """
        text_parts: List[str] = []
        file_paths: List[str] = []

        # Gateway 模式返回嵌套结构，先展开 result 层
        inner = data.get("result") if isinstance(data.get("result"), dict) else data

        # 1. OpenClaw 本地 agent payloads 格式
        if "payloads" in inner and isinstance(inner["payloads"], list):
            for payload in inner["payloads"]:
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type", "text")
                if ptype == "file" or payload.get("file_path"):
                    fp = payload.get("file_path") or payload.get("path")
                    if fp and isinstance(fp, str):
                        file_paths.append(fp.strip())
                elif ptype == "text" and payload.get("text"):
                    text_parts.append(str(payload["text"]).strip())
                elif payload.get("text"):
                    # 无 type 字段时默认为文本
                    text_parts.append(str(payload["text"]).strip())

        # 2. 顶层 file_paths 字段（在 inner 或 data 中）
        for src in (inner, data):
            if "file_paths" in src and isinstance(src["file_paths"], list):
                for fp in src["file_paths"]:
                    if isinstance(fp, str) and fp.strip():
                        file_paths.append(fp.strip())
                break

        # 3. 常见文本字段名（兜底文本提取）
        if not text_parts:
            for src in (inner, data):
                for key in ("reply", "text", "content", "message", "output_text"):
                    if key in src and isinstance(src[key], str):
                        text_parts.append(src[key].strip())
                        break
                if text_parts:
                    break

        # 4. 嵌套 output 列表
        if not text_parts:
            for src in (inner, data):
                if "output" in src and isinstance(src["output"], list):
                    for item in src["output"]:
                        if isinstance(item, dict):
                            for key in ("text", "content", "output_text"):
                                if key in item and isinstance(item[key], str):
                                    text_parts.append(item[key].strip())
                                    break
                        if text_parts:
                            break
                if text_parts:
                    break

        if not text_parts and not file_paths:
            raise KeyError(f"JSON 中未找到回复字段，顶层 keys: {list(data.keys())}")

        return OpenClawResult(
            text="\n".join(text_parts),
            file_paths=file_paths,
        )


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
import time
import uuid
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
        file_monitor=None,
        file_downloader=None,
        config_path: Optional[Path] = None,
    ):
        self.ai_responder = ai_responder
        self.openclaw_client = openclaw_client
        self.openclaw_config = openclaw_config or OpenClawConfig()
        self.file_monitor = file_monitor
        self.file_downloader = file_downloader
        self._config_path = config_path
        self._session_map: Dict[str, str] = {}
        self.reply_on_at = getattr(ai_responder, "reply_on_at", True)
        self._load_session_map()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def __call__(self, event: MessageEvent) -> Union[str, OpenClawResult]:
        """处理消息事件，返回回复文本或复合结果。"""
        if self.reply_on_at and not event.is_at_me:
            return ""

        raw_content = str(event.content or "").strip()
        content = AIResponder._strip_at(raw_content, event.group_nickname)
        if not content:
            return ""

        # 处理 session 重置指令（全局，无需 /claw 前缀）
        stripped_lower = content.strip().lower()
        reset_cmds_lower = [cmd.lower() for cmd in self.openclaw_config.reset_commands]
        if stripped_lower in reset_cmds_lower:
            self._reset_session(event.group)
            return self.openclaw_config.reset_reply

        has_prefix, clean_content = self._strip_prefix(content)
        use_openclaw = (
            self.openclaw_config.enabled
            and self.openclaw_client is not None
            and (
                self.openclaw_config.mode == "openclaw"
                or (self.openclaw_config.mode == "hybrid" and has_prefix)
            )
        )
        if self.openclaw_config.mode == "openclaw":
            clean_content = content

        # ---- OpenClaw 路径 ----
        if use_openclaw:
            session_id = self._get_session_id(event.group)
            _hybrid_logger.info(
                "OpenClaw 模式: group=%s session=%s content=%s",
                event.group,
                session_id,
                clean_content[:80],
            )
            file_path: Optional[str] = None
            if (
                self.file_monitor is not None
                and getattr(event, "attachment_name", None)
            ):
                resolved = self.file_monitor.resolve(event.attachment_name)
                if resolved:
                    file_path = resolved
                    _hybrid_logger.info(
                        "附件路径解析: name=%s -> path=%s",
                        event.attachment_name,
                        file_path,
                    )
                elif self.file_downloader is not None:
                    _hybrid_logger.info(
                        "附件未下载，尝试主动触发下载: name=%s",
                        event.attachment_name,
                    )
                    try:
                        downloaded = self.file_downloader.download(
                            event.attachment_name
                        )
                        if downloaded:
                            file_path = downloaded
                            _hybrid_logger.info(
                                "附件主动下载成功: name=%s -> path=%s",
                                event.attachment_name,
                                file_path,
                            )
                    except Exception as exc:
                        _hybrid_logger.warning(
                            "附件主动下载失败: name=%s, %s",
                            event.attachment_name,
                            exc,
                        )

            try:
                result = self.openclaw_client.run_agent_full(
                    clean_content,
                    session_id=session_id,
                    file_path=file_path,
                )
                cleaned_text = self._sanitize_reply(result.text)
                # 从文本回复中提取 OpenClaw workspace 中的文件路径
                # agent 有时只在文本中提及保存路径，未在 JSON file_paths 中返回
                extracted_paths = self._extract_file_paths_from_text(cleaned_text)
                all_file_paths = list(result.file_paths or [])
                for p in extracted_paths:
                    if p not in all_file_paths:
                        all_file_paths.append(p)

                # 兜底：如果清洗后文本为空（原始内容全是内部错误/路径），
                # 但成功生成了文件，返回友好提示而非空消息或内部报错
                if not cleaned_text and all_file_paths:
                    cleaned_text = "文件已处理完成，请查收~"

                # 兜底：文本和 JSON 都没返回文件路径时，扫描 workspace 最近文件
                if not all_file_paths and file_path:
                    file_context = getattr(
                        self.openclaw_client, "last_file_context", None
                    )
                    scanned = self._scan_workspace_recent_files(
                        file_path,
                        workspace_input_path=getattr(
                            file_context, "workspace_input_path", ""
                        ),
                        started_at=getattr(file_context, "started_at", 0.0),
                    )
                    for p in scanned:
                        if p not in all_file_paths:
                            all_file_paths.append(p)

                return OpenClawResult(
                    text=cleaned_text,
                    file_paths=all_file_paths,
                )
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

    def _load_session_map(self) -> None:
        """从配置文件的 openclaw.session_map 加载已有对照表。"""
        if self._config_path is None or not self._config_path.exists():
            return
        try:
            with self._config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            raw_map = cfg.get("openclaw", {}).get("session_map", {})
            if isinstance(raw_map, dict):
                self._session_map = {str(k): str(v) for k, v in raw_map.items()}
                _hybrid_logger.info(
                    "从配置加载 session_map: %d 条记录", len(self._session_map)
                )
        except Exception as exc:
            _hybrid_logger.debug("加载 session_map 失败: %s", exc)

    def _save_session_map(self) -> None:
        """将当前 session_map 写回配置文件（只改 openclaw.session_map）。"""
        if self._config_path is None or not self._config_path.exists():
            return
        try:
            with self._config_path.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "openclaw" not in cfg or not isinstance(cfg["openclaw"], dict):
                cfg["openclaw"] = {}
            cfg["openclaw"]["session_map"] = dict(self._session_map)
            with self._config_path.open("w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.write("\n")
            _hybrid_logger.info(
                "session_map 已保存到配置: %d 条记录", len(self._session_map)
            )
        except Exception as exc:
            _hybrid_logger.warning("保存 session_map 失败: %s", exc)

    def _get_session_id(self, group: str) -> Optional[str]:
        """按群聊生成稳定的 session ID，确保上下文隔离。"""
        if not self.openclaw_config.session_per_group:
            return None
        if group not in self._session_map:
            self._session_map[group] = hashlib.sha256(
                group.encode("utf-8")
            ).hexdigest()[:16]
            self._save_session_map()
        return self._session_map[group]

    def _reset_session(self, group: str) -> None:
        """重置指定群聊的 session，生成全新随机 ID 并清理 OpenClaw 端 session 文件。"""
        old_session: Optional[str] = None
        if group in self._session_map:
            old_session = self._session_map.pop(group)
            _hybrid_logger.info(
                "Session 已重置: group=%s old_session=%s", group, old_session
            )
        else:
            _hybrid_logger.info("Session 重置（无现存）: group=%s", group)

        # 清理 OpenClaw 端残留 session 文件，避免 trajectory 累积导致 compaction 超时
        if old_session:
            self._cleanup_openclaw_session_files(old_session)

        # 生成全新随机 session_id，确保 OpenClaw 把它当作全新会话
        new_session = uuid.uuid4().hex[:16]
        self._session_map[group] = new_session
        _hybrid_logger.info(
            "Session 已生成新 ID: group=%s new_session=%s", group, new_session
        )
        self._save_session_map()

    def _cleanup_openclaw_session_files(self, session_id: str) -> None:
        """删除 OpenClaw 端指定 session 的残留文件（jsonl / trajectory）。"""
        sessions_dir = (
            Path.home()
            / ".openclaw"
            / "agents"
            / self.openclaw_config.agent_id
            / "sessions"
        )
        suffixes = [".jsonl", ".trajectory.jsonl", ".trajectory-path.json"]
        for suffix in suffixes:
            target = sessions_dir / f"{session_id}{suffix}"
            if target.exists():
                try:
                    target.unlink()
                    _hybrid_logger.info(
                        "已清理 OpenClaw session 文件: %s", target.name
                    )
                except Exception as exc:
                    _hybrid_logger.warning(
                        "清理 OpenClaw session 文件失败: %s, %s", target.name, exc
                    )

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
        for attr in (
            "quoted_sender",
            "quoted_content",
            "attachment_name",
            "attachment_type",
        ):
            if hasattr(event, attr):
                kwargs[attr] = getattr(event, attr)
        return MessageEvent(**kwargs)

    @staticmethod
    def _extract_file_paths_from_text(text: str) -> List[str]:
        """从 OpenClaw 文本回复中提取文件路径并转为绝对路径。

        OpenClaw agent 有时只在文本中提及保存路径，而未在 JSON 的 file_paths
        字段中返回。此方法提取 ``.wx4py_files/`` 开头的相对路径，以及文本中
        出现的纯文件名（在 workspace 目录中反查实际存在性）。
        """
        if not text:
            return []
        import re
        paths: List[str] = []
        workspace = Path.home() / ".openclaw" / "workspace"

        # 阶段1：匹配显式路径 .wx4py_files/... 或 ~/.openclaw/workspace/...
        for match in re.finditer(
            r'(?:~/\.openclaw/workspace/)?(\.wx4py_files/[^\s"\'<>]+?\.[a-zA-Z0-9]{1,10})',
            text,
        ):
            rel_path = match.group(1)
            abs_path = str(workspace / rel_path)
            if os.path.isfile(abs_path) and abs_path not in paths:
                paths.append(abs_path)
                _hybrid_logger.info("从文本中提取到文件路径: %s", abs_path)
            else:
                _hybrid_logger.debug(
                    "文本路径提取后文件不存在或已重复: %s", abs_path
                )

        # 阶段2：匹配纯文件名（无路径前缀），在 workspace 及其子目录中反查
        # OpenClaw 有时会返回 "当前目录文件列表： 表单号_55.docx ..." 这种格式
        search_dirs = [workspace, workspace / ".wx4py_files"]
        for match in re.finditer(
            r'[^\s"\'<>/\\，。；：、！？）】》]+?\.[a-zA-Z0-9]{1,10}',
            text,
        ):
            filename = match.group(0)
            # 如果已在阶段1命中（以 .wx4py_files/ 开头），跳过
            if filename.startswith("."):
                continue
            for base_dir in search_dirs:
                candidate = base_dir / filename
                if candidate.is_file():
                    abs_path = str(candidate)
                    if abs_path not in paths:
                        paths.append(abs_path)
                        _hybrid_logger.info(
                            "从文本文件名反查提取到文件路径: %s", abs_path
                        )
                    break

        return paths

    def _scan_workspace_recent_files(
        self,
        original_file_path: Optional[str],
        workspace_input_path: str = "",
        started_at: float = 0.0,
        max_age_seconds: int = 120,
        max_files: int = 5,
    ) -> List[str]:
        """扫描 OpenClaw workspace 中最近修改的文件，作为 file_paths 兜底。

        OpenClaw 有时生成文件后既不返回 JSON file_paths，也不在文本中提及文件名。
        此方法通过文件修改时间反查最近生成/修改的文件。
        """
        workspace = Path.home() / ".openclaw" / "workspace"
        search_dirs = [workspace, workspace / ".wx4py_files"]
        now = time.time()
        original_ext = ""
        if original_file_path:
            original_ext = Path(original_file_path).suffix.lower()
        excluded_paths = {
            str(Path(p).resolve())
            for p in (original_file_path, workspace_input_path)
            if p
        }

        same_ext_candidates: List[tuple[float, str]] = []
        other_candidates: List[tuple[float, str]] = []

        for base_dir in search_dirs:
            if not base_dir.exists():
                continue
            try:
                for entry in base_dir.iterdir():
                    if not entry.is_file():
                        continue
                    # 跳过输入文件及其 workspace 副本，避免把原附件当结果发回去。
                    try:
                        entry_resolved = str(entry.resolve())
                    except Exception:
                        entry_resolved = str(entry)
                    if entry_resolved in excluded_paths:
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                        if started_at and mtime < started_at:
                            continue
                        age = now - mtime
                        if 0 <= age <= max_age_seconds:
                            item = (mtime, str(entry))
                            if original_ext and entry.suffix.lower() == original_ext:
                                same_ext_candidates.append(item)
                            else:
                                other_candidates.append(item)
                    except Exception:
                        continue
            except Exception:
                continue

        # 同扩展名优先，其他扩展名次之（支持格式转换场景）
        candidates = same_ext_candidates + other_candidates
        if not candidates:
            return []

        # 按修改时间倒序，取最新的
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = [p for _, p in candidates[:max_files]]
        _hybrid_logger.info(
            "Workspace 扫描兜底找到 %d 个最近文件: %s",
            len(selected),
            selected,
        )
        return selected

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        """清洗 OpenClaw 回复中的 Markdown 痕迹、JSON payloads 标记、内部错误和路径。"""
        if not text:
            return ""
        import re

        # 1. 过滤 OpenClaw 偶发的 JSON payloads 标记
        # 如：[{"type":"file","file_path":"..."}]
        text = re.sub(
            r'\[\s*\{[^{}]*"type"\s*:\s*"file"[^{}]*\}\s*\]',
            "",
            text,
        )

        # 2. 过滤内部 workspace 路径暴露（用户不应看到 .wx4py_files/）
        text = re.sub(r'\.wx4py_files/[^{\s}\n）]+', "", text)

        # 3. 过滤 OpenClaw 内部 tool 错误信息（避免把内部报错发给用户）
        error_patterns = [
            r"当前工具调用遇到[^，。]*错误[^，。]*[，。]?",
            r"无法直接读取文件内容[，。]?",
            r"模块加载错误[，。]?",
            r"请你确认文件是否正确上传[，。]?",
            r"或者重新尝试一下哦[，。]?",
            r"之前我已经帮你把原文件重命名为[，。]?",
        ]
        for pattern in error_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        # 4. 清理多余空白和孤立标点
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'[，。,\.]+\s*$', '', text).strip()

        # 复用 AIResponder 的清洗逻辑
        return AIResponder._sanitize_reply(text, group_nickname=None, max_chars=0)
