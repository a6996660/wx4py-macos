# -*- coding: utf-8 -*-
"""本地 Web 配置服务器。

基于 Python 标准库 http.server，零外部依赖。
仅绑定 127.0.0.1，随机端口，带 Token 校验。
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse


DEFAULT_CONFIG: Dict[str, Any] = {
    "_说明": "wx4py AI 群聊机器人配置",
    "groups": [],
    "reply_delay_range": [2, 5],
    "ai_queue_size": 0,
    "ai_context_size": 8,
    "ai_max_reply_chars": 180,
    "group_log_root": "logs/group_mentions",
    "error_log_root": "logs/errors",
    "default": "",
    "providers": {},
    "openclaw": {
        "enabled": False,
        "agent_id": "main",
        "prefixes": ["/claw", "\\claw"],
        "timeout": 60,
        "fallback_to_llm": True,
        "session_per_group": True,
        "placeholder_text": "让我想想…",
        "placeholder_reply_on_failure": "这个我直接来答吧",
        "cli_path": "",
        "gateway_host": "127.0.0.1",
        "gateway_port": 18789,
        "gateway_url": "",
        "gateway_token": "",
        "allow_insecure_private_ws": False,
        "file_support": False,
        "reset_commands": ["/new", "/reset"],
        "reset_reply": "🆕 已重置对话，开始新会话～",
        "session_map": {},
    },
    "file_monitor": {
        "enabled": False,
        "watch_dirs": [],
        "poll_interval": 5,
        "max_age_seconds": 600,
        "auto_discover": True,
    },
}


class ConfigHTTPServer(HTTPServer):
    """支持传入自定义参数的 HTTP 服务器。"""

    allow_reuse_address = True


class ConfigHandler(BaseHTTPRequestHandler):
    """配置页面请求处理器。"""

    def __init__(
        self,
        request,
        client_address,
        server,
        *,
        token: str,
        config_file: Path,
        launch_event: threading.Event,
    ):
        self._token = token
        self._config_file = config_file
        self._launch_event = launch_event
        super().__init__(request, client_address, server)

    def log_message(self, format: str, *args) -> None:
        # 静默访问日志，避免刷屏
        pass

    # ------------------------------------------------------------------
    # HTTP 方法分发
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_index()
        elif parsed.path == "/api/config":
            if not self._check_token():
                return
            self._serve_config()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._check_token():
            return
        if parsed.path == "/api/config":
            self._handle_save(should_launch=False)
        elif parsed.path == "/api/launch":
            self._handle_save(should_launch=True)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _check_token(self) -> bool:
        token = self.headers.get("X-Config-Token", "")
        if token != self._token:
            self._send_json(403, {"error": "Forbidden: invalid token"})
            return False
        return True

    def _send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # 路由处理
    # ------------------------------------------------------------------

    def _serve_index(self) -> None:
        html_path = Path(__file__).with_suffix("").parent / "static" / "index.html"
        try:
            html = html_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_json(500, {"error": "index.html not found"})
            return

        current_config = self._load_current_config()
        inject = (
            "<script>"
            f"window.__CONFIG_TOKEN__ = {json.dumps(self._token)};"
            f"window.__INITIAL_CONFIG__ = {json.dumps(current_config, ensure_ascii=False)};"
            "</script>"
        )
        html = html.replace("</head>", inject + "</head>")
        self._send_html(html)

    def _serve_config(self) -> None:
        config = self._load_current_config()
        self._send_json(200, config)

    def _handle_save(self, should_launch: bool = False) -> None:
        MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length <= 0:
            self._send_json(400, {"error": "Empty body"})
            return
        if content_length > MAX_BODY_SIZE:
            self._send_json(413, {"error": "Request body too large"})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"Invalid JSON: {exc}"})
            return

        errors = _validate_config(data)
        if errors:
            self._send_json(400, {"error": "Validation failed", "details": errors})
            return

        _save_config(self._config_file, data)

        if should_launch:
            self._launch_event.set()

        self._send_json(200, {"success": True, "launched": should_launch})

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    def _load_current_config(self) -> Dict[str, Any]:
        if self._config_file.exists():
            try:
                return json.loads(self._config_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        # 回退到 example 模板
        example_path = self._config_file.with_name("wx4py_ai_config.example.json")
        if example_path.exists():
            try:
                return json.loads(example_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return dict(DEFAULT_CONFIG)


# ------------------------------------------------------------------
# 配置验证
# ------------------------------------------------------------------

def _validate_config(data: Dict[str, Any]) -> Dict[str, str]:
    errors: Dict[str, str] = {}

    groups = data.get("groups")
    if not isinstance(groups, list) or not any(str(g).strip() for g in groups):
        errors["groups"] = "至少需要填写一个群聊名称"

    delay = data.get("reply_delay_range")
    if (
        not isinstance(delay, (list, tuple))
        or len(delay) != 2
        or not all(isinstance(x, (int, float)) for x in delay)
    ):
        errors["reply_delay_range"] = "必须是两个数字，例如 [2, 5]"
    elif float(delay[0]) >= float(delay[1]):
        errors["reply_delay_range"] = "最小值必须小于最大值"

    ai_queue_size = data.get("ai_queue_size")
    if not isinstance(ai_queue_size, int) or ai_queue_size < 0:
        errors["ai_queue_size"] = "必须是不小于 0 的整数"

    ai_context_size = data.get("ai_context_size")
    if not isinstance(ai_context_size, int) or ai_context_size < 1:
        errors["ai_context_size"] = "必须是不小于 1 的整数"

    ai_max_reply_chars = data.get("ai_max_reply_chars")
    if not isinstance(ai_max_reply_chars, int) or ai_max_reply_chars < 20:
        errors["ai_max_reply_chars"] = "必须是不小于 20 的整数"

    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors["providers"] = "至少需要配置一个 AI 服务提供商"
    else:
        default = str(data.get("default", "")).strip()
        if not default:
            errors["default"] = "请填写默认使用的提供商名称"
        elif default not in providers:
            errors["default"] = f"默认提供商 '{default}' 未在 providers 中定义"
        for name, cfg in providers.items():
            if not isinstance(cfg, dict):
                errors[f"providers.{name}"] = "配置必须是对象"
                continue
            for field in ("base_url", "model", "api_key"):
                if not str(cfg.get(field, "")).strip():
                    errors[f"providers.{name}.{field}"] = f"{field} 不能为空"

    openclaw = data.get("openclaw")
    if isinstance(openclaw, dict):
        mode = str(openclaw.get("mode", "")).strip().lower()
        if not mode:
            mode = "hybrid" if openclaw.get("enabled") else "llm"
        if mode not in {"llm", "hybrid", "openclaw"}:
            errors["openclaw.mode"] = "模式必须是 hybrid、llm 或 openclaw"
        openclaw_enabled = mode != "llm"
    else:
        openclaw_enabled = False
        mode = "llm"

    if isinstance(openclaw, dict) and openclaw_enabled:
        timeout = openclaw.get("timeout")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            errors["openclaw.timeout"] = "超时时间必须大于 0"
        prefixes = openclaw.get("prefixes")
        if mode == "hybrid" and (not isinstance(prefixes, list) or not prefixes):
            errors["openclaw.prefixes"] = "双引擎模式至少需要填写一个触发前缀"
        gateway_port = openclaw.get("gateway_port", 18789)
        if not isinstance(gateway_port, int) or gateway_port <= 0 or gateway_port > 65535:
            errors["openclaw.gateway_port"] = "Gateway 端口必须是 1-65535 的整数"
        gateway_url = str(openclaw.get("gateway_url", "")).strip()
        if gateway_url and not (
            gateway_url.startswith("ws://") or gateway_url.startswith("wss://")
        ):
            errors["openclaw.gateway_url"] = "Gateway URL 必须以 ws:// 或 wss:// 开头"

    return errors


# ------------------------------------------------------------------
# 配置保存
# ------------------------------------------------------------------

def _merge_with_comments(existing: Any, incoming: Any) -> Any:
    """递归合并配置，保留以 _ 开头的说明字段，并保留 existing 中 incoming 未覆盖的字段。"""
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return incoming

    result: Dict[str, Any] = {}
    # 保留说明字段
    for key, value in existing.items():
        if key.startswith("_"):
            result[key] = value

    # 合并 incoming 字段
    for key, value in incoming.items():
        if key.startswith("_"):
            continue
        if key in existing and isinstance(existing[key], dict) and isinstance(value, dict):
            result[key] = _merge_with_comments(existing[key], value)
        else:
            result[key] = value

    # 保留 existing 中但 incoming 中不存在的非说明字段（防止意外丢失）
    for key, value in existing.items():
        if key.startswith("_"):
            continue
        if key not in incoming:
            result[key] = value

    return result


def _save_config(config_file: Path, data: Dict[str, Any]) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有配置（含说明字段）
    existing: Dict[str, Any] = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    else:
        example_path = config_file.with_name("wx4py_ai_config.example.json")
        if example_path.exists():
            try:
                existing = json.loads(example_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    merged = _merge_with_comments(existing, data)
    config_file.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------
# 公开接口
# ------------------------------------------------------------------

def run_config_server(
    config_file: Path,
    *,
    timeout: Optional[float] = None,
) -> None:
    """启动本地配置服务器，阻塞直到用户点击"保存并启动"。

    Args:
        config_file: 目标配置文件路径。
        timeout: 最长等待秒数，None 表示无限等待。
    """
    token = secrets.token_urlsafe(32)
    launch_event = threading.Event()

    server = ConfigHTTPServer(
        ("127.0.0.1", 0),
        lambda request, client_address, srv: ConfigHandler(
            request,
            client_address,
            srv,
            token=token,
            config_file=config_file,
            launch_event=launch_event,
        ),
    )
    port = server.server_address[1]

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{port}/"
    print(f"\n🌐 配置界面已启动: {url}")
    print("   正在自动打开浏览器...\n")
    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass

    # 阻塞等待用户点击"保存并启动"
    launched = launch_event.wait(timeout=timeout)

    server.shutdown()
    server_thread.join(timeout=5)

    if not launched:
        raise RuntimeError("配置界面等待超时，未收到启动指令。")
