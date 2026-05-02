# -*- coding: utf-8 -*-
"""macOS 微信窗口管理。"""

import time

from .exceptions import WeChatNotFoundError
from .uia_wrapper import UIAWrapper
from ..config import OPERATION_INTERVAL
from ..platform import platform
from ..utils.logger import get_logger

logger = get_logger(__name__)

_MIN_AX_TREE_NODES = 5


def _count_ax_descendants(ctrl, max_depth: int = 4, limit: int = 20) -> int:
    """快速统计可访问性控件树节点数，用于健康检查。"""
    count = 0
    stack = [(ctrl, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count >= limit:
            return count
        if depth >= max_depth:
            continue
        try:
            for child in node.GetChildren() or []:
                stack.append((child, depth + 1))
        except Exception:
            continue
    return count


class WeChatWindow:
    """微信主窗口连接与激活管理器。"""

    def __init__(self):
        self._hwnd: int = None
        self._uia: UIAWrapper = None
        self._initialized = False

    def _activate_hwnd(self, hwnd: int) -> bool:
        """激活指定微信窗口；窗口不可见时先尝试通过 Dock/AppleScript 恢复。"""
        if not hwnd:
            return False

        if not platform.window_manager.is_visible(hwnd):
            if not platform.accessibility.restore_from_tray(hwnd):
                logger.warning("微信窗口不可见，且未能通过 macOS Dock/AppleScript 恢复。")
                return False
            time.sleep(0.5)
            refreshed = platform.window_manager.find_wechat_window()
            if refreshed:
                self._hwnd = refreshed
                hwnd = refreshed

        return platform.window_manager.bring_to_front(hwnd)

    def _try_click_login_button(self, hwnd: int) -> bool:
        """如果停留在登录界面，尝试点击“进入微信”。"""
        try:
            root = platform.automation.control_from_handle(hwnd)
            if not root:
                return False

            for control, _depth in platform.automation.walk_control(root, include_top=True, max_depth=10):
                if control.ControlTypeName != "ButtonControl":
                    continue
                if "进入微信" not in (control.Name or ""):
                    continue
                logger.info("检测到登录界面，尝试点击'进入微信'按钮...")
                try:
                    control.Click(simulateMove=False)
                except Exception:
                    rect = control.BoundingRectangle
                    if not rect:
                        return False
                    platform.input.mouse_click(rect.center_x, rect.center_y)
                return True
        except Exception as exc:
            logger.debug(f"尝试点击登录按钮失败: {exc}")
        return False

    def _wait_for_wechat_window(self, timeout: float = 30.0) -> int:
        """等待微信主窗口出现并返回窗口 ID。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            hwnd = platform.window_manager.find_wechat_window()
            if hwnd:
                return hwnd
            time.sleep(0.5)
        return 0

    def connect(self) -> bool:
        """连接 macOS 微信主窗口并初始化 AX 控件树。"""
        logger.info("正在检查辅助功能环境...")
        status = platform.accessibility.ensure_accessibility_environment()
        if status == "unauthorized":
            raise WeChatNotFoundError(
                "macOS 辅助功能权限未授权。请在 系统设置 → 隐私与安全性 → 辅助功能 "
                "中授权当前终端/Python/OpenClaw 后重试。"
            )
        if status == "granted_after_prompt":
            logger.info("macOS 辅助功能权限已获得授权")

        logger.info("正在查找微信窗口...")
        self._hwnd = platform.window_manager.find_wechat_window()
        if not self._hwnd:
            raise WeChatNotFoundError("未找到微信窗口，请确保 macOS 微信已打开并登录。")

        logger.info(f"找到微信窗口: HWND={self._hwnd}")
        self._activate_hwnd(self._hwnd)
        time.sleep(OPERATION_INTERVAL)

        if self._try_click_login_button(self._hwnd):
            self._hwnd = self._wait_for_wechat_window()
            if not self._hwnd:
                raise WeChatNotFoundError("已点击进入微信，但主窗口未出现。请确认微信已登录。")
            self._activate_hwnd(self._hwnd)
            time.sleep(0.5)

        logger.info("正在初始化 macOS Accessibility 控件树...")
        self._uia = UIAWrapper(self._hwnd)
        node_count = _count_ax_descendants(self._uia.root)
        logger.debug(f"AX 控件树健康检查: 节点数={node_count}")
        if node_count < _MIN_AX_TREE_NODES:
            logger.warning(
                f"AX 控件树较少（仅 {node_count} 个节点），"
                "请确认微信主窗口已打开且未最小化。"
            )

        self._initialized = True
        logger.info("成功连接到微信")
        return True

    def disconnect(self) -> None:
        """断开微信窗口连接。"""
        self._hwnd = None
        self._uia = None
        self._initialized = False
        logger.info("已断开微信连接")

    @property
    def hwnd(self) -> int:
        if not self._initialized:
            raise WeChatNotFoundError("未连接到微信")
        return self._hwnd

    @property
    def uia(self) -> UIAWrapper:
        if not self._initialized:
            raise WeChatNotFoundError("未连接到微信")
        return self._uia

    @property
    def is_connected(self) -> bool:
        return self._initialized and self._hwnd is not None

    @property
    def title(self) -> str:
        return platform.window_manager.get_title(self._hwnd) if self._hwnd else ""

    @property
    def class_name(self) -> str:
        return platform.window_manager.get_class(self._hwnd) if self._hwnd else ""

    def refresh(self) -> bool:
        self.disconnect()
        return self.connect()

    def activate(self) -> bool:
        return self._activate_hwnd(self._hwnd) if self._hwnd else False
