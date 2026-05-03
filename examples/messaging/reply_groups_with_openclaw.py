# -*- coding: utf-8 -*-
r"""接入 OpenClaw 做群聊自动回复（双引擎模式）。

使用步骤：
1. 先设置环境变量或配置文件：
   - `SILICONFLOW_API_KEY`（LLM 秒回用）
2. 确保 OpenClaw 已安装并可用：
   `openclaw --version`
3. 把 `GROUPS` 改成你要监听的群名称列表。
4. 运行：
   `python examples/messaging/reply_groups_with_openclaw.py`

说明：
- 普通 @ 消息走 LLM 秒回（如 "@机器人 你好"）
- 带前缀的消息走 OpenClaw agent（如 "@机器人 /claw 帮我查邮件"）
- OpenClaw 失败时自动降级到 LLM
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src import (
    AIClient,
    AIConfig,
    AIResponder,
    AsyncCallbackHandler,
    HybridResponder,
    OpenClawClient,
    OpenClawConfig,
    WeChatClient,
)


GROUPS = ["测试群1", "测试群2"]


def build_hybrid_responder() -> HybridResponder:
    """构建双引擎回复器。"""
    api_key = os.getenv("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置环境变量 SILICONFLOW_API_KEY")

    # LLM 客户端（秒回）
    ai = AIClient(
        AIConfig(
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            api_format="completions",
            model=os.getenv("SILICONFLOW_MODEL", "Pro/deepseek-ai/DeepSeek-V3.2"),
            api_key=api_key,
            temperature=0.7,
            max_tokens=300,
            enable_thinking=False,
        )
    )
    ai_responder = AIResponder(ai, context_size=8, reply_on_at=True)

    # OpenClaw 配置
    openclaw_cfg = OpenClawConfig(
        enabled=True,
        agent_id="main",
        prefixes=["/claw", "\\claw"],
        timeout=60,
        fallback_to_llm=True,
        session_per_group=True,
    )

    try:
        openclaw_client = OpenClawClient(openclaw_cfg)
    except Exception as exc:
        print(f"OpenClaw 初始化失败，将仅使用 LLM: {exc}")
        openclaw_cfg = OpenClawConfig(enabled=False)
        openclaw_client = None

    return HybridResponder(
        ai_responder,
        openclaw_client=openclaw_client,
        openclaw_config=openclaw_cfg,
    )


def main() -> None:
    with WeChatClient(auto_connect=True) as wx:
        responder = build_hybrid_responder()
        print("开始监听群消息…")
        print("- 普通 @ 消息: LLM 秒回")
        print("- /claw 前缀: OpenClaw agent（慢但能力强）")
        wx.process_groups(
            GROUPS,
            [
                AsyncCallbackHandler(
                    responder,
                    auto_reply=True,
                    reply_on_at=True,
                )
            ],
            block=True,
            tick=0.1,
            batch_size=8,
            tail_size=8,
        )


if __name__ == "__main__":
    main()
