# -*- coding: utf-8 -*-
"""自定义异常类"""


class WeChatError(Exception):
    """wx4py 基础异常类"""
    pass


class WeChatNotFoundError(WeChatError):
    """微信窗口未找到"""
    pass


class WeChatNotConnectedError(WeChatError):
    """微信未连接或未初始化"""
    pass


class UIAError(WeChatError):
    """UIAutomation 相关错误"""
    pass


class ControlNotFoundError(UIAError):
    """UI 控件未找到"""
    pass


class TargetNotFoundError(ControlNotFoundError):
    """目标聊天在搜索结果中未找到"""
    pass


class RegistryError(WeChatError):
    """注册表操作错误"""
    pass
