# -*- coding: utf-8 -*-
"""macOS 平台后端入口。

本项目已收敛为 macOS-only。所有微信窗口、控件、输入和剪贴板操作
都通过 ``MacOSBackend`` 实现；在非 macOS 平台导入时会直接给出清晰错误。
"""

from __future__ import annotations

import sys
from typing import Optional

from ._base import PlatformBackend
from ..utils.logger import get_logger

logger = get_logger(__name__)

_platform_backend: Optional[PlatformBackend] = None


def get_backend() -> PlatformBackend:
    """返回 macOS 后端实例。"""
    global _platform_backend
    if _platform_backend is not None:
        return _platform_backend

    if sys.platform != "darwin":
        raise RuntimeError(
            f"不支持的操作系统: {sys.platform}。"
            "当前版本已移除 Windows 适配，仅支持 macOS 微信 4.x。"
        )

    from ._macos import MacOSBackend, check_accessibility_permission

    logger.info("检测到 macOS 平台，加载 macOS 后端")
    if not check_accessibility_permission():
        logger.warning(
            "macOS 辅助功能权限未授权！"
            "请在 系统设置 → 隐私与安全性 → 辅助功能 中授权终端/Python/OpenClaw。"
            "否则无法自动化控制微信窗口。"
        )

    _platform_backend = MacOSBackend()
    return _platform_backend


platform: PlatformBackend = get_backend()


def reload_backend() -> PlatformBackend:
    """强制重新加载平台后端，用于测试或重新检查权限状态。"""
    global _platform_backend
    _platform_backend = None
    return get_backend()
