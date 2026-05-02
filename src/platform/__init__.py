# -*- coding: utf-8 -*-
"""平台抽象层 - 平台检测、后端工厂与全局单例。

用法:
    from .platform import platform

    hwnd = platform.window_manager.find_wechat_window()
    platform.input.key_down(platform.input.VK_CONTROL)
"""

from __future__ import annotations

import sys
from typing import Optional

from ._base import PlatformBackend
from ..utils.logger import get_logger

logger = get_logger(__name__)

_platform_backend: Optional[PlatformBackend] = None


def get_backend() -> PlatformBackend:
    """根据当前运行平台返回对应的后端实例。

    - ``sys.platform == 'win32'`` → WindowsBackend
    - ``sys.platform == 'darwin'`` → MacOSBackend

    Returns:
        PlatformBackend: 平台后端实例
    """
    global _platform_backend
    if _platform_backend is not None:
        return _platform_backend

    if sys.platform == "win32":
        from ._windows import WindowsBackend

        logger.info("检测到 Windows 平台，加载 Windows 后端")
        _platform_backend = WindowsBackend()
    elif sys.platform == "darwin":
        from ._macos import MacOSBackend, check_accessibility_permission

        logger.info("检测到 macOS 平台，加载 macOS 后端")

        if not check_accessibility_permission():
            logger.warning(
                "macOS 辅助功能权限未授权！"
                "请在 系统偏好设置 → 隐私与安全性 → 辅助功能 中授权终端/Python。"
                "否则无法自动化控制微信窗口。"
            )

        _platform_backend = MacOSBackend()
    else:
        raise RuntimeError(
            f"不支持的操作系统: {sys.platform}。"
            f"wx4py 目前仅支持 Windows 和 macOS。"
        )

    return _platform_backend


# 全局后端单例
platform: PlatformBackend = get_backend()


def reload_backend() -> PlatformBackend:
    """强制重新加载平台后端（用于测试或在运行时切换平台检测）。"""
    global _platform_backend
    _platform_backend = None
    return get_backend()
