# -*- coding: utf-8 -*-
"""微信 UIAutomation 封装器"""

from __future__ import annotations

from ..platform import platform
from ..platform._base import PlatformControl

from .exceptions import ControlNotFoundError
from ..config import SEARCH_TIMEOUT
from ..utils.logger import get_logger

logger = get_logger(__name__)


class UIAWrapper:
    """UIAutomation 操作封装器"""

    def __init__(self, hwnd: int = None):
        """
        初始化 UIA 封装器。

        Args:
            hwnd: 可选的窗口句柄
        """
        self._root: PlatformControl = None
        if hwnd:
            self.bind(hwnd)

    def bind(self, hwnd: int) -> None:
        """
        通过窗口句柄绑定。

        Args:
            hwnd: 窗口句柄
        """
        self._root = platform.automation.control_from_handle(hwnd)
        if not self._root:
            raise ControlNotFoundError(f"无法从句柄 {hwnd} 获取 UIAutomation 控件")
        logger.debug(f"已绑定窗口: {self._root.Name}")

    @property
    def root(self) -> PlatformControl:
        """获取根控件"""
        return self._root

    def find_control(self, control_type: str = None, name: str = None,
                     class_name: str = None, automation_id: str = None,
                     timeout: float = None) -> PlatformControl:
        """
        按属性查找控件。

        Args:
            control_type: 控件类型（Button、Edit 等）
            name: 控件名称
            class_name: 控件类名
            automation_id: 自动化 ID
            timeout: 搜索超时时间（秒）

        Returns:
            找到的控件

        Raises:
            ControlNotFoundError: 控件未找到时抛出
        """
        timeout = timeout or SEARCH_TIMEOUT

        ctrl = platform.automation.find_control(
            self._root,
            control_type=control_type,
            name=name,
            class_name=class_name,
            automation_id=automation_id,
            search_depth=10,
            timeout=timeout,
        )
        if ctrl:
            return ctrl

        raise ControlNotFoundError(
            f"控件未找到: type={control_type}, name={name}, "
            f"class={class_name}, id={automation_id}"
        )

    def find_all_controls(self, control_type: str = None, **kwargs) -> list:
        """
        查找所有匹配的控件。

        Args:
            control_type: 控件类型
            **kwargs: 其他筛选参数

        Returns:
            控件列表
        """
        return platform.automation.find_all_controls(
            self._root,
            control_type=control_type,
            search_depth=10,
            **kwargs,
        )

    def click(self, control: PlatformControl) -> bool:
        """
        点击控件。

        Args:
            control: 要点击的控件

        Returns:
            bool: 成功时返回 True
        """
        try:
            platform.automation.click(control)
            logger.debug(f"已点击控件: {control.Name}")
            return True
        except Exception as e:
            logger.error(f"点击控件失败: {e}")
            return False

    def send_keys(self, control: PlatformControl, text: str) -> bool:
        """
        向控件发送按键。

        Args:
            control: 目标控件
            text: 要发送的文本

        Returns:
            bool: 成功时返回 True
        """
        try:
            platform.automation.send_keys(control, text)
            logger.debug(f"已发送按键到控件: {text[:20]}...")
            return True
        except Exception as e:
            logger.error(f"发送按键失败: {e}")
            return False
