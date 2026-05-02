# -*- coding: utf-8 -*-
"""剪贴板工具"""
import os
import struct


def _get_platform():
    """延迟导入 platform 模块，避免循环导入。"""
    from ..platform import platform
    return platform


def set_files_to_clipboard(file_paths):
    """
    将文件路径设置到剪贴板。

    这允许将文件粘贴到微信等应用程序的聊天输入框中。

    Args:
        file_paths: 单个文件路径字符串或文件路径列表

    Returns:
        bool: 成功时返回 True

    Raises:
        ValueError: 文件路径不存在时抛出
    """
    # 将单个字符串转换为列表
    if isinstance(file_paths, str):
        file_paths = [file_paths]

    # 验证文件路径
    valid_paths = []
    for path in file_paths:
        if os.path.exists(path):
            valid_paths.append(os.path.abspath(path))
        else:
            raise ValueError(f"File not found: {path}")

    if not valid_paths:
        return False

    platform = _get_platform()
    return platform.clipboard.set_files(valid_paths)


def set_text_to_clipboard(text: str) -> bool:
    """
    将文本设置到剪贴板。

    Args:
        text: 文本内容

    Returns:
        bool: 成功时返回 True
    """
    platform = _get_platform()
    return platform.clipboard.set_text(text)
