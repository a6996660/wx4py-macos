# -*- coding: utf-8 -*-
"""
wx4py - Python 微信自动化工具

面向 macOS 微信 4.x 的自动化工具，重点支持群聊 @ AI 自动回复。
"""

from ._version import __version__
from .ai import AIClient, AIConfig, AIResponder
from .client import WeChatClient
from .openclaw_client import (
    HybridResponder,
    OpenClawClient,
    OpenClawConfig,
    OpenClawError,
    OpenClawNotFoundError,
)
from .features.messaging.forwarder import (
    ForwardPayload,
    ForwardRuleHandler,
    ForwardTarget,
    GroupForwardRule,
)
from .features.messaging.listener import MessageEvent, WeChatGroupListener
from .features.messaging.processor import (
    AsyncCallbackHandler,
    CallbackHandler,
    ForwardAction,
    MessageAction,
    MessageHandler,
    ReplyAction,
    WeChatGroupProcessor,
)
from .core.exceptions import (
    WeChatError,
    WeChatNotFoundError,
    WeChatNotConnectedError,
    ControlNotFoundError,
    TargetNotFoundError,
)

__author__ = "wx4py Team"

__all__ = [
    "WeChatClient",
    "AIClient",
    "AIConfig",
    "AIResponder",
    "OpenClawClient",
    "OpenClawConfig",
    "HybridResponder",
    "OpenClawError",
    "OpenClawNotFoundError",
    "MessageEvent",
    "WeChatGroupListener",
    "MessageAction",
    "ReplyAction",
    "ForwardAction",
    "MessageHandler",
    "CallbackHandler",
    "AsyncCallbackHandler",
    "WeChatGroupProcessor",
    "ForwardTarget",
    "ForwardPayload",
    "GroupForwardRule",
    "ForwardRuleHandler",
    "WeChatError",
    "WeChatNotFoundError",
    "WeChatNotConnectedError",
    "ControlNotFoundError",
    "TargetNotFoundError",
]
