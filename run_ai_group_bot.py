# -*- coding: utf-8 -*-
"""一键启动微信群 @ 自动回复机器人。

使用步骤：
1. 复制配置模板：
   cp wx4py_ai_config.example.json wx4py_ai_config.json
2. 编辑 wx4py_ai_config.json，填写 groups、api_key、model 等配置。
3. 启动：
   python3 run_ai_group_bot.py

说明：
- 只监听配置文件 groups 中列出的群。
- 只有群消息 @ 当前登录微信昵称时才会调用大模型并回复。
- 自动回复前会按 reply_delay_range 随机等待。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from src import AIClient, AIConfig, AIResponder, AsyncCallbackHandler, WeChatClient


DEFAULT_CONFIG_FILE = "wx4py_ai_config.json"


def _config_path() -> Path:
    """返回配置文件路径，支持 WX4PY_AI_CONFIG 覆盖默认路径。"""
    return Path(os.environ.get("WX4PY_AI_CONFIG", DEFAULT_CONFIG_FILE)).expanduser()


def _load_raw_config(path: Path) -> Dict[str, Any]:
    """读取原始 JSON 配置，用于取得 groups 和 reply_delay_range。"""
    if not path.exists():
        raise FileNotFoundError(
            f"找不到配置文件: {path}\n"
            f"请先执行: cp wx4py_ai_config.example.json {DEFAULT_CONFIG_FILE}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _load_groups(config: Dict[str, Any]) -> list[str]:
    """读取监听群聊白名单。"""
    groups = [str(item).strip() for item in config.get("groups", []) if str(item).strip()]
    if not groups:
        raise ValueError("配置文件缺少 groups，至少需要填写一个群聊名称。")
    return groups


def _load_reply_delay_range(config: Dict[str, Any]) -> Tuple[float, float]:
    """读取自动回复随机延迟范围，默认 5 到 18 秒。"""
    value = config.get("reply_delay_range", [5, 18])
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("reply_delay_range 必须是两个数字，例如 [5, 18]。")
    start, end = float(value[0]), float(value[1])
    return (min(start, end), max(start, end))


def _load_ai_queue_size(config: Dict[str, Any]) -> int:
    """读取大模型消息队列长度，0 表示不限制。"""
    value = int(config.get("ai_queue_size", 0))
    if value < 0:
        raise ValueError("ai_queue_size 不能小于 0。")
    return value


def _load_ai_context_size(config: Dict[str, Any]) -> int:
    """读取同群大模型上下文长度。"""
    value = int(config.get("ai_context_size", 8))
    if value < 1:
        raise ValueError("ai_context_size 不能小于 1。")
    return value


def _configure_group_log_root(config: Dict[str, Any]) -> str:
    """配置按群 @ 审计日志目录。"""
    value = str(config.get("group_log_root", "logs/group_mentions")).strip()
    if not value:
        value = "logs/group_mentions"
    os.environ["WECHAT_GROUP_MENTION_LOG_ROOT"] = value
    return value


def main() -> None:
    config_file = _config_path()
    raw_config = _load_raw_config(config_file)
    groups = _load_groups(raw_config)
    reply_delay_range = _load_reply_delay_range(raw_config)
    ai_queue_size = _load_ai_queue_size(raw_config)
    ai_context_size = _load_ai_context_size(raw_config)
    group_log_root = _configure_group_log_root(raw_config)

    # AIConfig.from_file 会读取 providers/default，并支持环境变量覆盖。
    ai = AIClient(AIConfig.from_file(str(config_file)))

    print(f"配置文件: {config_file}")
    print(f"监听群聊: {', '.join(groups)}")
    print(f"回复随机延迟: {reply_delay_range[0]:.1f} - {reply_delay_range[1]:.1f} 秒")
    print(f"大模型消息队列: {'不限制' if ai_queue_size == 0 else ai_queue_size}")
    print(f"同群上下文长度: {ai_context_size}")
    print(f"群聊审计日志: {group_log_root}")
    print("启动中：只有被 @ 时才会调用大模型回复。按 Ctrl+C 停止。")

    with WeChatClient(auto_connect=True) as wx:
        wx.process_groups(
            groups,
            [
                AsyncCallbackHandler(
                    AIResponder(ai, context_size=ai_context_size, reply_on_at=True),
                    auto_reply=True,
                    reply_on_at=True,
                    queue_size=ai_queue_size,
                )
            ],
            reply_delay_range=reply_delay_range,
            block=True,
        )


if __name__ == "__main__":
    main()
