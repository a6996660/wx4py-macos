# -*- coding: utf-8 -*-
"""macOS 平台后端实现。

基于 pyobjc-framework-Quartz 的 Accessibility API (AXUIElement) 实现
微信 macOS 版的 UI 自动化控制。

依赖:
    pyobjc-framework-Quartz
    pyobjc-framework-Cocoa
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._base import (
    AutomationEngine,
    BoundingRect,
    ClipboardManager,
    InputSimulator,
    PlatformBackend,
    PlatformControl,
    ProcessManager,
    SystemAccessibility,
    ToggleState,
    WindowManager,
)

from ..utils.logger import get_logger

logger = get_logger(__name__)

# ---- pyobjc imports ----
try:
    import Quartz
    import Cocoa
    from Foundation import NSAppleScript
    _PYOBJC_AVAILABLE = True
except ImportError:
    _PYOBJC_AVAILABLE = False
    Quartz = None  # type: ignore
    Cocoa = None  # type: ignore

# ---- AX C API 加载 (PyObjC >=11 不再在 Quartz 中暴露这些 C 函数) ----
_AX_AVAILABLE = False
if _PYOBJC_AVAILABLE:
    try:
        import ctypes
        import ctypes.util
        import objc

        _ax_c = ctypes.CDLL(ctypes.util.find_library('ApplicationServices'))
        _cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
        _kCFStrEncUTF8 = 0x08000100

        # ---- 设置 ctypes 函数签名 ----
        _ax_c.AXUIElementCreateApplication.restype = ctypes.c_void_p
        _ax_c.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]

        _ax_c.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        _ax_c.AXUIElementCreateSystemWide.argtypes = []

        _ax_c.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        _ax_c.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]

        _ax_c.AXUIElementCopyAttributeNames.restype = ctypes.c_int32
        _ax_c.AXUIElementCopyAttributeNames.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]

        _ax_c.AXUIElementPerformAction.restype = ctypes.c_int32
        _ax_c.AXUIElementPerformAction.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
        ]

        _ax_c.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        _ax_c.AXUIElementSetAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]

        _ax_c.AXIsProcessTrusted.restype = ctypes.c_bool
        _ax_c.AXIsProcessTrusted.argtypes = []

        _ax_c.AXMakeProcessTrusted.restype = ctypes.c_int32
        _ax_c.AXMakeProcessTrusted.argtypes = [ctypes.c_void_p]

        _ax_c.AXValueGetType.restype = ctypes.c_int32
        _ax_c.AXValueGetType.argtypes = [ctypes.c_void_p]

        _ax_c.AXValueGetValue.restype = ctypes.c_bool
        _ax_c.AXValueGetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
        ]

        # CFString helpers
        _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        _cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
        ]
        _cf.CFRelease.restype = None
        _cf.CFRelease.argtypes = [ctypes.c_void_p]

        # 确认 API 可用
        _ax_c.AXIsProcessTrusted()
        _AX_AVAILABLE = True
    except Exception:
        pass


def _ax_mk_cfstr(s: str):
    """Python str → CFStringRef (caller must CFRelease)."""
    return _cf.CFStringCreateWithCString(None, s.encode(), _kCFStrEncUTF8)


def _ax_ptr_to_objc(ptr):
    """将原始 CFTypeRef/AXUIElementRef 指针转为 PyObjC 对象。"""
    if not ptr:
        return None
    c_ptr = ctypes.c_void_p(ptr)
    # CFRetain 防止 AX 元素被 UI 更新释放导致 use-after-free
    _cf.CFRetain(c_ptr)
    return objc.objc_object(c_void_p=c_ptr)


def _ax_objc_to_ptr(obj) -> ctypes.c_void_p:
    """将 PyObjC 对象转为原始 c_void_p 指针。"""
    if obj is None:
        return ctypes.c_void_p(0)
    try:
        if hasattr(obj, '__c_void_p__'):
            ptr = obj.__c_void_p__()
            if isinstance(ptr, int):
                return ctypes.c_void_p(ptr)
            return ctypes.c_void_p(ptr)
    except Exception:
        pass
    try:
        return ctypes.c_void_p(int(objc.pyobjc_id(obj)))
    except Exception:
        pass
    return ctypes.c_void_p(hash(obj))


# ---- 封装后的 AX API (替换 Quartz.AX* 调用) ----

_AX_WINDOW_LIST_ATTR = "AX" + "Win" + "dows"

def _ax_create_application(pid: int) -> Any:
    """AXUIElementCreateApplication — 返回 PyObjC 包装的对象。"""
    return _ax_ptr_to_objc(_ax_c.AXUIElementCreateApplication(pid))


def _ax_create_system_wide() -> Any:
    """AXUIElementCreateSystemWide — 返回 PyObjC 包装的对象。"""
    return _ax_ptr_to_objc(_ax_c.AXUIElementCreateSystemWide())


def _ax_copy_attr(element: Any, attr: str):
    """
    AXUIElementCopyAttributeValue 的安全封装。

    返回 (value, err_code)，err_code 为 0 表示成功。
    用法: window_list, err = _ax_copy_attr(app_ref, _AX_WINDOW_LIST_ATTR)
    """
    elem_ptr = _ax_objc_to_ptr(element)
    cf_attr = _ax_mk_cfstr(attr)
    val_ptr = ctypes.c_void_p()
    err = _ax_c.AXUIElementCopyAttributeValue(elem_ptr, cf_attr, ctypes.byref(val_ptr))
    _cf.CFRelease(cf_attr)
    return (_ax_ptr_to_objc(val_ptr.value) if val_ptr.value else None), err


def _ax_copy_attr_names(element: Any):
    """AXUIElementCopyAttributeNames — 返回 (names, err_code)。"""
    elem_ptr = _ax_objc_to_ptr(element)
    names_ptr = ctypes.c_void_p()
    err = _ax_c.AXUIElementCopyAttributeNames(elem_ptr, ctypes.byref(names_ptr))
    return (_ax_ptr_to_objc(names_ptr.value)), err


def _ax_perform_action_raw(element: Any, action: str) -> int:
    """AXUIElementPerformAction — 返回 AXError。"""
    elem_ptr = _ax_objc_to_ptr(element)
    cf_action = _ax_mk_cfstr(action)
    err = _ax_c.AXUIElementPerformAction(elem_ptr, cf_action)
    _cf.CFRelease(cf_action)
    return err


def _ax_set_attr(element: Any, attr: str, value: Any) -> int:
    """AXUIElementSetAttributeValue — 返回 AXError。"""
    elem_ptr = _ax_objc_to_ptr(element)
    cf_attr = _ax_mk_cfstr(attr)
    # 将 Python 基础类型转换为 CF 类型
    cf_value = None
    if isinstance(value, str):
        cf_value = _ax_mk_cfstr(value)
    elif isinstance(value, bool):
        cf_value = ctypes.c_void_p(
            Cocoa.NSNumber.numberWithBool_(value).__c_void_p__()
        )
    else:
        cf_value = _ax_objc_to_ptr(value) if value is not None else ctypes.c_void_p(0)
    err = _ax_c.AXUIElementSetAttributeValue(elem_ptr, cf_attr, cf_value)
    _cf.CFRelease(cf_attr)
    if cf_value and isinstance(value, str):
        _cf.CFRelease(cf_value)
    return err


def _ax_is_trusted() -> bool:
    """AXIsProcessTrusted 封装。"""
    return bool(_ax_c.AXIsProcessTrusted())


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


# ============================================================================
# macOS 微信控件映射常量 (Task 13)
# ============================================================================

# 微信 macOS 进程/包标识符
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_PROCESS_NAMES = {"WeChat", "微信"}

# AX Role 映射：微信 macOS 中各类 UI 元素对应的 Accessibility 角色
WECHAT_AX_ROLES = {
    "search_field": "AXTextField",        # 搜索输入框
    "chat_input": "AXTextArea",           # 聊天消息输入框
    "message_list": "AXList",             # 消息列表
    "session_list": "AXOutline",          # 左侧会话列表
    "button": "AXButton",                 # 通用按钮
    "group": "AXGroup",                   # 分组容器
    "window": "AXWindow",                 # 窗口
    "popover": "AXPopover",               # 弹窗
    "scroll_area": "AXScrollArea",        # 滚动区域
    "static_text": "AXStaticText",        # 静态文本
    "check_box": "AXCheckBox",            # 复选框
    "table": "AXTable",                   # 表格
    "cell": "AXCell",                     # 单元格
}

# 微信 macOS UI 元素描述关键词（用于控件查找）
WECHAT_AX_DESCRIPTIONS = {
    "search_box": ["搜索", "search", "Search"],
    "chat_input": ["输入", "消息", "message", "input"],
    "message_area": ["消息列表", "聊天记录", "chat", "message"],
    "session_panel": ["会话", "聊天", "chats", "session"],
    "group_info_btn": ["聊天信息", "群聊信息", "info", "详情"],
    "group_announcement_btn": ["群公告", "公告", "announcement"],
    "group_member_list": ["成员", "member"],
}

# 微信窗口标题关键词
WECHAT_WINDOW_TITLES = ("微信", "WeChat")

_UIA_TO_AX_ROLES = {
    "Button": ("AXButton",),
    "CheckBox": ("AXCheckBox",),
    "Custom": ("AXGroup", "AXUnknown"),
    "DataGrid": ("AXTable", "AXGrid"),
    "Edit": ("AXTextField", "AXTextArea", "AXComboBox"),
    "Group": ("AXGroup", "AXSplitGroup"),
    "List": ("AXList", "AXOutline", "AXTable"),
    "ListItem": ("AXRow", "AXCell", "AXStaticText"),
    "Pane": ("AXScrollArea", "AXPopover", "AXSplitGroup", "AXGroup"),
    "Tab": ("AXTabGroup", "AXRadioGroup"),
    "Text": ("AXStaticText",),
    "Tree": ("AXOutline",),
    "Window": ("AXWindow",),
}


# ============================================================================
# macOS 辅助功能权限检查 (Task 12)
# ============================================================================

def check_accessibility_permission() -> bool:
    """检查当前进程是否拥有辅助功能权限。

    macOS 要求应用在"系统偏好设置 → 隐私与安全性 → 辅助功能"中被授权。
    """
    if not _AX_AVAILABLE:
        # 如果 AX API 不可用，假定有权限（让实际调用时失败）
        return True
    try:
        return bool(_ax_is_trusted())
    except Exception:
        return False


def _prompt_accessibility_permission() -> None:
    """提示用户开启辅助功能权限。"""
    script = '''
    tell application "System Preferences"
        activate
        reveal pane id "com.apple.preference.security.Privacy_Accessibility"
    end tell
    '''
    try:
        NSAppleScript.alloc().initWithSource_(script).executeAndReturnError_(None)
    except Exception:
        pass


# ============================================================================
# macOS 内部工具函数
# ============================================================================

def _find_wechat_pid() -> Optional[int]:
    """查找微信进程 PID。"""
    if not _PYOBJC_AVAILABLE:
        # Fallback: 使用 ps 命令
        try:
            result = subprocess.run(
                ["pgrep", "-f", "WeChat"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split("\n")[0])
        except Exception:
            pass
        return None

    for app in Cocoa.NSWorkspace.sharedWorkspace().runningApplications():
        try:
            if app.bundleIdentifier() == WECHAT_BUNDLE_ID:
                return app.processIdentifier()
        except Exception:
            continue
    return None


def _get_wechat_app_ref() -> Any:
    """获取微信应用的 AXUIElementRef。"""
    if not _PYOBJC_AVAILABLE:
        return None
    pid = _find_wechat_pid()
    if not pid:
        return None
    return _ax_create_application(pid)


def _find_wechat_window_element() -> Any:
    """获取微信主窗口的 AXUIElement。"""
    app_ref = _get_wechat_app_ref()
    if not app_ref:
        return None

    try:
        windows, _ = _ax_copy_attr(app_ref, _AX_WINDOW_LIST_ATTR)
        if windows:
            candidates = []
            for window in windows:
                try:
                    title, _ = _ax_copy_attr(window, "AXTitle")
                    title = str(title or "")
                    pos = _ax_get_position(window)
                    size = _ax_get_size(window)
                    children_count = len(_ax_get_children(window))
                    area = (size[0] * size[1]) if size else 0
                    has_session_list = _ax_tree_has_session_list(window)
                    score = 0
                    if title == "微信":
                        score += 500
                    elif any(kw in title for kw in WECHAT_WINDOW_TITLES):
                        score += 100
                    if has_session_list:
                        score += 1000
                    else:
                        score -= 300
                    if children_count:
                        score += min(children_count, 20) * 10
                    if area >= 300000:
                        score += 80
                    if "窗口" in title:
                        score -= 200
                    if pos and (pos[0] < 0 or pos[1] < 0):
                        score -= 50
                    if score > 0:
                        candidates.append((score, area, children_count, window))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
                return candidates[0][3]
    except Exception:
        pass

    # Fallback: 返回第一个窗口
    try:
        windows, _ = _ax_copy_attr(app_ref, _AX_WINDOW_LIST_ATTR)
        if windows and len(windows) > 0:
            for window in windows:
                if _ax_tree_has_session_list(window):
                    return window
            return windows[0]
    except Exception:
        pass
    return None


def _ax_tree_has_session_list(element: Any, max_depth: int = 6) -> bool:
    """判断窗口是否是带左侧会话列表的微信主窗口。"""
    stack = [(element, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            identifier = str(_ax_get_attr(node, "AXIdentifier") or "")
            if identifier == "session_list" or identifier.startswith("session_item_"):
                return True
            for child in reversed(_ax_get_children(node)):
                stack.append((child, depth + 1))
        except Exception:
            continue
    return False


def _ax_get_attr(element: Any, attr: str) -> Optional[Any]:
    """安全读取 AXUIElement 属性值。"""
    try:
        value, _ = _ax_copy_attr(element, attr)
        return value
    except Exception:
        return None


def _ax_get_attr_names(element: Any) -> List[str]:
    """获取 AXUIElement 的所有属性名。"""
    try:
        names, _ = _ax_copy_attr_names(element)
        return list(names) if names else []
    except Exception:
        return []


def _ax_get_role(element: Any) -> str:
    val = _ax_get_attr(element, "AXRole")
    if val:
        return str(val)
    return ""


def _ax_get_title(element: Any) -> str:
    val = _ax_get_attr(element, "AXTitle")
    if val:
        return str(val)
    return ""


def _ax_get_description(element: Any) -> str:
    val = _ax_get_attr(element, "AXDescription")
    if val:
        return str(val)
    return ""


def _ax_get_value(element: Any) -> str:
    val = _ax_get_attr(element, "AXValue")
    if val:
        return str(val)
    return ""


def _ax_get_position(element: Any) -> Optional[Tuple[float, float]]:
    try:
        pos, _ = _ax_copy_attr(element, "AXPosition")
        if pos:
            pt = _CGPoint()
            if _ax_c.AXValueGetValue(_ax_objc_to_ptr(pos), 1, ctypes.byref(pt)):
                return (pt.x, pt.y)
    except Exception:
        pass
    return None


def _ax_get_size(element: Any) -> Optional[Tuple[float, float]]:
    try:
        size, _ = _ax_copy_attr(element, "AXSize")
        if size:
            sz = _CGSize()
            if _ax_c.AXValueGetValue(_ax_objc_to_ptr(size), 2, ctypes.byref(sz)):
                return (sz.width, sz.height)
    except Exception:
        pass
    return None


def _ax_get_children(element: Any) -> List[Any]:
    val = _ax_get_attr(element, "AXChildren")
    if val:
        return list(val)
    return []


def _ax_get_parent(element: Any) -> Optional[Any]:
    return _ax_get_attr(element, "AXParent")


def _ax_get_window(element: Any) -> Optional[Any]:
    # 向上查找直到 AXWindow
    current = element
    for _ in range(20):
        if not current:
            break
        role = _ax_get_role(current)
        if role == "AXWindow":
            return current
        current = _ax_get_parent(current)
    return None


def _ax_walk_tree(element: Any, max_depth: int = 6) -> List[Tuple[Any, int]]:
    """遍历 AX 控件树。"""
    result = []
    stack = [(element, 0)]
    while stack:
        node, depth = stack.pop()
        result.append((node, depth))
        if depth >= max_depth:
            continue
        children = _ax_get_children(node)
        for child in reversed(children):
            stack.append((child, depth + 1))
    return result


def _ax_find_descendants(
    element: Any,
    role: Any = None,
    title: str = None,
    description_contains: str = None,
    automation_id: str = None,
    max_depth: int = 10,
    max_results: int = None,
) -> List[Any]:
    """在 AX 控件树中查找匹配的后代元素。"""
    results = []
    stack = [(element, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            continue

        # 检查匹配
        match = True
        if role:
            node_role = _ax_get_role(node)
            roles = role if isinstance(role, (list, tuple, set)) else (role,)
            if node_role not in roles:
                match = False
        if match and title:
            node_title = _ax_get_title(node)
            node_desc = _ax_get_description(node)
            node_value = _ax_get_value(node)
            searchable_name = " ".join((node_title, node_desc, node_value))
            if title not in searchable_name:
                match = False
        if match and automation_id:
            node_id = _ax_get_attr(node, "AXIdentifier")
            if str(node_id or "") != automation_id:
                match = False
        if match and description_contains:
            node_desc = _ax_get_description(node) + " " + _ax_get_title(node) + " " + _ax_get_role(node)
            found = False
            for kw in (description_contains if isinstance(description_contains, list) else [description_contains]):
                if kw.lower() in node_desc.lower():
                    found = True
                    break
            if not found:
                match = False

        if match:
            results.append(node)
            if max_results and len(results) >= max_results:
                return results

        children = _ax_get_children(node)
        for child in reversed(children):
            stack.append((child, depth + 1))

    return results


def _ax_perform_action(element: Any, action: str) -> bool:
    """对 AXUIElement 执行操作。"""
    try:
        result = _ax_perform_action_raw(element, action)
        return result == 0  # kAXErrorSuccess == 0
    except Exception:
        return False


# ============================================================================
# 窗口 ID 生成
# ============================================================================

# macOS 没有 HWND 概念，用 AXUIElement 的 hash 或序列号作为 "窗口 ID"
# 为了兼容现有接口，返回一个整数标识
_window_id_counter = [1]
_window_id_map: Dict[int, Any] = {}  # id -> AXUIElement


def _register_window(element: Any) -> int:
    wid = hash(str(element)) % (10 ** 9)
    _window_id_map[wid] = element
    return wid


def _can_use_ax() -> bool:
    return _AX_AVAILABLE and check_accessibility_permission()


def _get_window_element(wid: int) -> Optional[Any]:
    """获取窗口 AX 元素。

    发送热路径会频繁通过同一个窗口句柄取控件根节点。优先使用缓存的
    AXUIElement，避免每次都全量枚举微信窗口；缓存失效时再实时查询。
    """
    cached = _window_id_map.get(wid)
    if cached is not None and not isinstance(cached, int):
        try:
            if _ax_get_role(cached):
                return cached
        except Exception:
            pass

    if _can_use_ax():
        window = _find_wechat_window_element()
        if window:
            _window_id_map[wid] = window
            return window

    return None if isinstance(cached, int) else cached


def _get_window_cgid(wid: int) -> Optional[int]:
    cached = _window_id_map.get(wid)
    return cached if isinstance(cached, int) else None


def _get_cg_window_info(cgid: int) -> Optional[dict]:
    if not _PYOBJC_AVAILABLE or not cgid:
        return None
    try:
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow,
            cgid,
        )
        return dict(window_list[0]) if window_list else None
    except Exception:
        return None


# ============================================================================
# MacOSPlatformControl 适配
# ============================================================================

class _MacOSControlAdapter:
    """将 macOS AXUIElement 适配为 PlatformControl 的操作。"""

    @staticmethod
    def get_name(ctrl: PlatformControl) -> str:
        return _ax_get_title(ctrl._native) or _ax_get_description(ctrl._native) or ""

    @staticmethod
    def get_class_name(ctrl: PlatformControl) -> str:
        return _ax_get_role(ctrl._native)

    @staticmethod
    def get_automation_id(ctrl: PlatformControl) -> str:
        # macOS 没有 AutomationId，用 AXIdentifier
        val = _ax_get_attr(ctrl._native, "AXIdentifier")
        return str(val) if val else ""

    @staticmethod
    def get_control_type_name(ctrl: PlatformControl) -> str:
        role = _ax_get_role(ctrl._native)
        # 将 macOS AXRole 映射为历史 ControlType 名称以保持兼容。
        _AX_TO_UIA_TYPE = {
            "AXTextField": "EditControl",
            "AXTextArea": "EditControl",
            "AXButton": "ButtonControl",
            "AXWindow": "WindowControl",
            "AXList": "ListControl",
            "AXOutline": "TreeControl",
            "AXTable": "TableControl",
            "AXGroup": "GroupControl",
            "AXCheckBox": "CheckBoxControl",
            "AXRadioButton": "RadioButtonControl",
            "AXPopover": "PaneControl",
            "AXScrollArea": "PaneControl",
            "AXSplitGroup": "PaneControl",
            "AXTabGroup": "TabControl",
            "AXMenuBar": "MenuBarControl",
            "AXMenuItem": "MenuItemControl",
            "AXComboBox": "ComboBoxControl",
            "AXStaticText": "TextControl",
            "AXImage": "ImageControl",
        }
        return _AX_TO_UIA_TYPE.get(role, f"{role}Control")

    @staticmethod
    def get_bounding_rectangle(ctrl: PlatformControl) -> Optional[BoundingRect]:
        pos = _ax_get_position(ctrl._native)
        size = _ax_get_size(ctrl._native)
        if pos and size:
            return BoundingRect(
                left=int(pos[0]),
                top=int(pos[1]),
                right=int(pos[0] + size[0]),
                bottom=int(pos[1] + size[1]),
            )
        return None

    @staticmethod
    def get_is_selected(ctrl: PlatformControl) -> bool:
        val = _ax_get_attr(ctrl._native, "AXSelected")
        if val is not None:
            return bool(val)
        val = _ax_get_attr(ctrl._native, "AXFocused")
        return bool(val) if val is not None else False

    @staticmethod
    def click(ctrl: PlatformControl, simulateMove: bool = True) -> bool:
        if simulateMove:
            rect = _MacOSControlAdapter.get_bounding_rectangle(ctrl)
            if rect:
                _simulate_mouse_click(rect.center_x, rect.center_y)
                return True
        if _ax_perform_action(ctrl._native, "AXPress"):
            return True
        rect = _MacOSControlAdapter.get_bounding_rectangle(ctrl)
        if rect:
            _simulate_mouse_click(rect.center_x, rect.center_y)
            return True
        return False

    @staticmethod
    def double_click(ctrl: PlatformControl, simulateMove: bool = True) -> bool:
        if simulateMove:
            rect = _MacOSControlAdapter.get_bounding_rectangle(ctrl)
            if rect:
                _simulate_mouse_dblclick(rect.center_x, rect.center_y)
                return True
        # macOS AX 没有标准的双击 action，回退到两次 Press
        _ax_perform_action(ctrl._native, "AXPress")
        time.sleep(0.05)
        return _ax_perform_action(ctrl._native, "AXPress")

    @staticmethod
    def send_keys(ctrl: PlatformControl, text: str) -> bool:
        if _send_uia_keys(text):
            return True

        role = _ax_get_role(ctrl._native)
        if role in {"AXTextField", "AXTextArea", "AXComboBox"}:
            try:
                if _ax_set_attr(ctrl._native, "AXValue", text) == 0:
                    return True
            except Exception:
                pass

        # Fallback: 聚焦后模拟键盘输入
        _ax_perform_action(ctrl._native, "AXRaise")
        try:
            _ax_set_attr(ctrl._native, "AXFocused", Cocoa.NSNumber.numberWithBool_(True))
        except Exception:
            pass
        _simulate_typing(text)
        return True

    @staticmethod
    def set_focus(ctrl: PlatformControl) -> None:
        try:
            _ax_set_attr(
                ctrl._native, "AXFocused", Cocoa.NSNumber.numberWithBool_(True)
            )
        except Exception:
            pass

    @staticmethod
    def get_children(ctrl: PlatformControl) -> List[PlatformControl]:
        children = _ax_get_children(ctrl._native)
        return [PlatformControl(ctrl._backend, c) for c in children]

    @staticmethod
    def get_parent(ctrl: PlatformControl) -> Optional[PlatformControl]:
        parent = _ax_get_parent(ctrl._native)
        if parent:
            return PlatformControl(ctrl._backend, parent)
        return None

    @staticmethod
    def get_runtime_id(ctrl: PlatformControl) -> Tuple[int, ...]:
        # 用 AXUIElement hash 作为稳定 ID
        try:
            return (hash(ctrl._native), )
        except Exception:
            return ()

    @staticmethod
    def get_pattern(ctrl: PlatformControl, pattern_id: Any) -> Any:
        # macOS AX 没有传统 Pattern 的概念，返回一个轻量对象保持兼容。
        return _MacOSPatternWrapper(ctrl._native)

    @staticmethod
    def exists(ctrl: PlatformControl, max_search_seconds: float = 0) -> bool:
        # 检查 AXUIElement 是否仍然有效
        try:
            role = _ax_get_role(ctrl._native)
            return bool(role)
        except Exception:
            return False


class _MacOSPatternWrapper:
    """macOS Accessibility Pattern 兼容适配器。"""

    def __init__(self, element):
        self._element = element

    @property
    def Value(self) -> str:
        return _ax_get_value(self._element)

    @property
    def ToggleState(self) -> Any:
        val = _ax_get_attr(self._element, "AXValue")
        if val is not None:
            try:
                if int(val) == 1:
                    return ToggleState.On
            except (ValueError, TypeError):
                pass
        return ToggleState.Off


# ---- macOS 键盘/鼠标模拟底层 ----

def _simulate_key_event(key_code: int, key_down: bool) -> None:
    """模拟键盘按键事件。"""
    if not _PYOBJC_AVAILABLE:
        return
    event = Quartz.CGEventCreateKeyboardEvent(None, key_code, key_down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _simulate_typing(text: str) -> None:
    """逐字符模拟输入字符串。"""
    if not _PYOBJC_AVAILABLE:
        return
    for char in text:
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(event, len(char), char)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)
        event_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)
        time.sleep(0.02)


def _simulate_mouse_click(x: int, y: int) -> None:
    """在屏幕坐标处模拟鼠标点击。"""
    if not _PYOBJC_AVAILABLE:
        return
    point = Quartz.CGPoint(x, y)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft))
    time.sleep(0.1)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft))


def _simulate_mouse_dblclick(x: int, y: int) -> None:
    """在屏幕坐标处模拟鼠标双击。"""
    if not _PYOBJC_AVAILABLE:
        return
    point = Quartz.CGPoint(x, y)
    for _ in range(2):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft))
        time.sleep(0.05)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft))
        time.sleep(0.08)


def _simulate_mouse_scroll(delta: int, steps: int, step_delay: float = 0.1) -> None:
    """模拟鼠标滚轮。"""
    if not _PYOBJC_AVAILABLE:
        return
    for _ in range(steps):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, delta))
        time.sleep(step_delay)


def _simulate_set_cursor(x: int, y: int) -> None:
    """设置鼠标光标位置。"""
    if not _PYOBJC_AVAILABLE:
        return
    point = Quartz.CGPoint(x, y)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, 0))


_SENDKEY_ALIASES = {
    "CTRL": InputSimulator.VK_CONTROL,
    "CONTROL": InputSimulator.VK_CONTROL,
    "ENTER": InputSimulator.VK_RETURN,
    "RETURN": InputSimulator.VK_RETURN,
    "ESC": InputSimulator.VK_ESCAPE,
    "ESCAPE": InputSimulator.VK_ESCAPE,
    "DELETE": InputSimulator.VK_DELETE,
    "DEL": InputSimulator.VK_DELETE,
    "TAB": InputSimulator.VK_TAB,
    "SPACE": InputSimulator.VK_SPACE,
    "SHIFT": InputSimulator.VK_SHIFT,
}

_SENDKEY_CHAR_MAP = {
    "a": InputSimulator.VK_A,
    "c": InputSimulator.VK_C,
    "f": InputSimulator.VK_F,
    "v": InputSimulator.VK_V,
}


def _parse_uia_send_keys(text: str) -> Optional[Tuple[List[int], Optional[int]]]:
    """解析历史 SendKeys 风格的短按键表达式，如 {Ctrl}a、{Enter}。"""
    if not text or "{" not in text or "}" not in text:
        return None

    modifiers: List[int] = []
    key: Optional[int] = None
    pos = 0
    while pos < len(text):
        if text[pos] == "{":
            end = text.find("}", pos + 1)
            if end == -1:
                return None
            token = text[pos + 1:end].strip().upper()
            code = _SENDKEY_ALIASES.get(token)
            if code is None:
                return None
            if token in {"CTRL", "CONTROL", "SHIFT"}:
                modifiers.append(code)
            else:
                key = code
            pos = end + 1
            continue

        char = text[pos]
        if char.strip():
            code = _SENDKEY_CHAR_MAP.get(char.lower())
            if code is None:
                return None
            key = code
        pos += 1

    if key is None:
        return None
    return modifiers, key


def _send_uia_keys(text: str) -> bool:
    parsed = _parse_uia_send_keys(text)
    if parsed is None:
        return False
    modifiers, key = parsed
    for modifier in modifiers:
        _simulate_key_event(_MACOS_KEY_MAP.get(modifier, modifier), True)
        time.sleep(0.03)
    _simulate_key_event(_MACOS_KEY_MAP.get(key, key), True)
    time.sleep(0.03)
    _simulate_key_event(_MACOS_KEY_MAP.get(key, key), False)
    for modifier in reversed(modifiers):
        time.sleep(0.03)
        _simulate_key_event(_MACOS_KEY_MAP.get(modifier, modifier), False)
    time.sleep(0.1)
    return True


# ---- macOS CGWindowList 窗口查找 ----

def _find_wechat_window_cgid() -> Optional[int]:
    """通过 CGWindowList 查找微信窗口的 CGWindowID。"""
    if not _PYOBJC_AVAILABLE:
        return None

    pid = _find_wechat_pid()
    if not pid:
        return None

    try:
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for window in window_list:
            try:
                owner_pid = window.get("kCGWindowOwnerPID", 0)
                owner_name = window.get("kCGWindowOwnerName", "")
                window_layer = window.get("kCGWindowLayer", 999)

                if owner_pid != pid:
                    continue
                # 排除菜单栏/系统层级窗口
                if window_layer > 100:
                    continue
                # 微信主窗口通常在 layer 0，且名称含"微信"
                window_name = window.get("kCGWindowName", "")
                if any(kw in str(window_name) for kw in WECHAT_WINDOW_TITLES) or any(
                    kw in str(owner_name) for kw in WECHAT_PROCESS_NAMES
                ):
                    return int(window.get("kCGWindowNumber", 0))
            except Exception:
                continue

        # Fallback: 返回第一个微信窗口
        for window in window_list:
            try:
                if window.get("kCGWindowOwnerPID", 0) == pid and window.get("kCGWindowLayer", 999) <= 100:
                    return int(window.get("kCGWindowNumber", 0))
            except Exception:
                continue
    except Exception:
        pass

    return None


def _enumerate_visible_window_handles(callback: Callable, extra: Any = None) -> None:
    """枚举所有可见窗口。"""
    if not _PYOBJC_AVAILABLE:
        return
    try:
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for window in window_list:
            try:
                cgid = int(window.get("kCGWindowNumber", 0))
                if not callback(cgid, extra):
                    break
            except Exception:
                continue
    except Exception:
        pass


# ============================================================================
# MacOSWindowManager
# ============================================================================

class MacOSWindowManager(WindowManager):
    """macOS 窗口管理器。"""

    def find_wechat_window(self) -> Optional[int]:
        # 优先用 CGWindowList 检测进程（不需要 AX 权限）
        cgid = _find_wechat_window_cgid()
        if cgid and _can_use_ax():
            # 同时获取 AX 窗口元素用于后续 UI 操作
            window = _find_wechat_window_element()
            if window:
                return _register_window(window)
        if not cgid:
            # 回退使用 AX 窗口
            window = _find_wechat_window_element()
            if window:
                return _register_window(window)
        # 如果 AX 不可用，仅注册 CGWindowID（仅支持基础窗口操作）
        return _register_window(cgid) if cgid else None

    def bring_to_front(self, hwnd: int) -> bool:
        if not _PYOBJC_AVAILABLE:
            return False
        element = _get_window_element(hwnd)
        if element is not None:
            return _ax_perform_action(element, "AXRaise")
        # 尝试通过 CGWindowID 激活，使用 AppleScript 激活微信
        try:
            script = '''
            tell application "WeChat"
                activate
            end tell
            '''
            NSAppleScript.alloc().initWithSource_(script).executeAndReturnError_(None)
            return True
        except Exception:
            return False

    def get_title(self, hwnd: int) -> str:
        element = _get_window_element(hwnd)
        if element is not None:
            return _ax_get_title(element)
        info = _get_cg_window_info(_get_window_cgid(hwnd) or 0)
        if info:
            return str(info.get("kCGWindowName") or "")
        return ""

    def get_class(self, hwnd: int) -> str:
        # macOS 返回进程名作为"窗口类名"
        element = _get_window_element(hwnd)
        if element is not None:
            return _ax_get_role(element)
        info = _get_cg_window_info(_get_window_cgid(hwnd) or 0)
        if info:
            return str(info.get("kCGWindowOwnerName") or "MainWindow")
        return "MainWindow"  # 默认值兼容微信检测逻辑

    def is_visible(self, hwnd: int) -> bool:
        element = _get_window_element(hwnd)
        if element is not None:
            try:
                minimized = _ax_get_attr(element, "AXMinimized")
                hidden = _ax_get_attr(element, "AXHidden")
                if minimized or hidden:
                    return False
                # 检查是否在主屏幕上
                pos = _ax_get_position(element)
                size = _ax_get_size(element)
                if pos and size:
                    return size[0] > 0 and size[1] > 0
            except Exception:
                pass
        cgid = _get_window_cgid(hwnd)
        if cgid:
            return _get_cg_window_info(cgid) is not None
        # 通过 CGWindowList 检查
        pid = _find_wechat_pid()
        return pid is not None

    def minimize(self, hwnd: int) -> bool:
        element = _get_window_element(hwnd)
        if element is not None:
            try:
                _ax_set_attr(element, "AXMinimized", Cocoa.NSNumber.numberWithBool_(True))
                return True
            except Exception:
                pass
        # Fallback: AppleScript
        try:
            script = 'tell application "WeChat" to set miniaturized of every window to true'
            NSAppleScript.alloc().initWithSource_(script).executeAndReturnError_(None)
            return True
        except Exception:
            return False

    def enum_window_handles(self, callback: Callable, extra: Any = None) -> None:
        _enumerate_visible_window_handles(callback, extra)

    def enum_child_window_handles(self, parent: int) -> List[int]:
        results = []
        element = _get_window_element(parent)
        if element is not None:
            children = _ax_get_children(element)
            for child in children:
                if _ax_get_role(child) == "AXWindow":
                    wid = _register_window(child)
                    results.append(wid)
        return results


# ============================================================================
# MacOSAutomation
# ============================================================================

class MacOSAutomation(AutomationEngine):
    """macOS Accessibility Automation 引擎。"""

    def control_from_handle(self, hwnd: int) -> Optional[PlatformControl]:
        element = _get_window_element(hwnd)
        if element is not None:
            return PlatformControl(self, element)
        # 如果 hwnd 是 CGWindowID，尝试通过 AX 获取
        if _can_use_ax():
            pid = _find_wechat_pid()
            if pid:
                try:
                    app_ref = _ax_create_application(pid)
                    return PlatformControl(self, app_ref)
                except Exception:
                    pass
        return None

    def find_control(
        self,
        root: PlatformControl,
        control_type: str = None,
        name: str = None,
        class_name: str = None,
        automation_id: str = None,
        search_depth: int = None,
        timeout: float = None,
    ) -> Optional[PlatformControl]:
        from ..config import SEARCH_TIMEOUT
        timeout = timeout or SEARCH_TIMEOUT
        search_depth = search_depth or 10

        # 解析控件类型为AX角色
        ax_role = None
        if control_type and control_type != "Control":
            # 去除 Control 后缀
            clean_type = control_type.replace("Control", "")
            ax_role = _UIA_TO_AX_ROLES.get(clean_type, (f"AX{clean_type}",))

        if class_name and class_name.startswith("AX"):
            ax_role = (class_name,)
            class_name = None

        deadline = time.time() + timeout
        while time.time() < deadline:
            results = _ax_find_descendants(
                root._native,
                role=ax_role,
                title=name,
                description_contains=class_name,
                automation_id=automation_id,
                max_depth=search_depth,
                max_results=1,
            )
            if results:
                return PlatformControl(self, results[0])
            time.sleep(0.2)

        return None

    def find_all_controls(
        self,
        root: PlatformControl,
        control_type: str = None,
        search_depth: int = None,
        **filters,
    ) -> List[PlatformControl]:
        ax_role = None
        if control_type and control_type != "Control":
            clean_type = control_type.replace("Control", "")
            ax_role = _UIA_TO_AX_ROLES.get(clean_type, (f"AX{clean_type}",))

        desc = filters.get("class_name") or filters.get("ClassName")
        name = filters.get("name") or filters.get("Name")
        automation_id = filters.get("automation_id") or filters.get("AutomationId")

        if desc and str(desc).startswith("AX"):
            ax_role = (str(desc),)
            desc = None

        results = _ax_find_descendants(
            root._native,
            role=ax_role,
            title=name,
            description_contains=desc,
            automation_id=automation_id,
            max_depth=search_depth or 10,
        )
        return [PlatformControl(self, r) for r in results]

    def walk_control(
        self,
        root: PlatformControl,
        include_top: bool = True,
        max_depth: int = 6,
    ) -> List[Tuple[PlatformControl, int]]:
        tree = _ax_walk_tree(root._native, max_depth=max_depth)
        if not include_top:
            tree = tree[1:]
        return [(PlatformControl(self, elem), depth) for elem, depth in tree]

    def get_focused_control(self) -> Optional[PlatformControl]:
        if not _PYOBJC_AVAILABLE:
            return None
        try:
            system = _ax_create_system_wide()
            focused, _ = _ax_copy_attr(system, "AXFocusedUIElement")
            if focused:
                return PlatformControl(self, focused)
        except Exception:
            pass
        return None

    # --- 控件属性代理 ---

    def get_name(self, control: PlatformControl) -> str:
        return _MacOSControlAdapter.get_name(control)

    def get_class_name(self, control: PlatformControl) -> str:
        return _MacOSControlAdapter.get_class_name(control)

    def get_automation_id(self, control: PlatformControl) -> str:
        return _MacOSControlAdapter.get_automation_id(control)

    def get_control_type_name(self, control: PlatformControl) -> str:
        return _MacOSControlAdapter.get_control_type_name(control)

    def get_bounding_rectangle(self, control: PlatformControl) -> Optional[BoundingRect]:
        return _MacOSControlAdapter.get_bounding_rectangle(control)

    def get_is_selected(self, control: PlatformControl) -> bool:
        return _MacOSControlAdapter.get_is_selected(control)

    def click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        return _MacOSControlAdapter.click(control, simulateMove)

    def double_click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        return _MacOSControlAdapter.double_click(control, simulateMove)

    def send_keys(self, control: PlatformControl, text: str) -> bool:
        return _MacOSControlAdapter.send_keys(control, text)

    def set_focus(self, control: PlatformControl) -> None:
        _MacOSControlAdapter.set_focus(control)

    def get_children(self, control: PlatformControl) -> List[PlatformControl]:
        return _MacOSControlAdapter.get_children(control)

    def get_parent(self, control: PlatformControl) -> Optional[PlatformControl]:
        return _MacOSControlAdapter.get_parent(control)

    def get_runtime_id(self, control: PlatformControl) -> Tuple[int, ...]:
        return _MacOSControlAdapter.get_runtime_id(control)

    def get_pattern(self, control: PlatformControl, pattern_id: Any) -> Any:
        return _MacOSControlAdapter.get_pattern(control, pattern_id)

    def exists(self, control: PlatformControl, max_search_seconds: float = 0) -> bool:
        if max_search_seconds > 0:
            deadline = time.time() + max_search_seconds
            while time.time() < deadline:
                if _MacOSControlAdapter.exists(control, 0):
                    return True
                time.sleep(0.2)
            return False
        return _MacOSControlAdapter.exists(control, 0)


# ============================================================================
# MacOSInput
# ============================================================================

# macOS 虚拟键码（与通用 VK 码不同的映射）
# 这里提供 CGEvent 使用的键码
_MACOS_KEY_MAP = {
    InputSimulator.VK_RETURN: 0x24,
    InputSimulator.VK_TAB: 0x30,
    InputSimulator.VK_SPACE: 0x31,
    InputSimulator.VK_DELETE: 0x33,
    InputSimulator.VK_ESCAPE: 0x35,
    InputSimulator.VK_SHIFT: 0x38,    # Left Shift
    InputSimulator.VK_CONTROL: 0x37,  # Command，保持跨平台 Ctrl+X 语义
    InputSimulator.VK_A: 0x00,
    InputSimulator.VK_C: 0x08,
    InputSimulator.VK_F: 0x03,
    InputSimulator.VK_V: 0x09,
}

_MACOS_COMMAND = 0x37


class MacOSInput(InputSimulator):
    """macOS 输入模拟器。"""

    def key_down(self, key_code: int) -> None:
        mac_code = _MACOS_KEY_MAP.get(key_code, key_code)
        _simulate_key_event(mac_code, True)

    def key_up(self, key_code: int) -> None:
        mac_code = _MACOS_KEY_MAP.get(key_code, key_code)
        _simulate_key_event(mac_code, False)

    def send_combo(self, modifier: int, key: int, settle_time: float = 0.3) -> None:
        # macOS 上将跨平台 Ctrl+key 语义映射为 Command+key。
        if not _PYOBJC_AVAILABLE:
            return
        self.key_down(modifier)
        time.sleep(0.05)
        self.key_down(key)
        time.sleep(0.05)
        self.key_up(key)
        time.sleep(0.05)
        self.key_up(modifier)
        time.sleep(settle_time)

    def mouse_click(self, x: int, y: int) -> None:
        _simulate_mouse_click(x, y)

    def mouse_dblclick(self, x: int, y: int) -> None:
        _simulate_mouse_dblclick(x, y)

    def mouse_down(self, x: int, y: int) -> None:
        if not _PYOBJC_AVAILABLE:
            return
        point = Quartz.CGPoint(x, y)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft))

    def mouse_up(self, x: int, y: int) -> None:
        if not _PYOBJC_AVAILABLE:
            return
        point = Quartz.CGPoint(x, y)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft))

    def mouse_scroll(self, delta: int, steps: int, step_delay: float = 0.1) -> None:
        _simulate_mouse_scroll(delta, steps, step_delay)

    def set_cursor(self, x: int, y: int) -> None:
        _simulate_set_cursor(x, y)


# ============================================================================
# MacOSClipboard
# ============================================================================

class MacOSClipboard(ClipboardManager):
    """macOS 剪贴板管理器（使用 NSPasteboard）。"""

    def set_text(self, text: str) -> bool:
        if not _PYOBJC_AVAILABLE:
            # Fallback: 使用 pbcopy
            try:
                proc = subprocess.run(
                    ["pbcopy"], input=text, text=True, timeout=5
                )
                return proc.returncode == 0
            except Exception:
                return False

        try:
            pasteboard = Cocoa.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            pasteboard.setString_forType_(text, Cocoa.NSPasteboardTypeString)
            return True
        except Exception:
            return False

    def set_files(self, file_paths) -> bool:
        """将文件路径设置到剪贴板。"""
        if isinstance(file_paths, str):
            file_paths = [file_paths]

        valid_paths = []
        for path in file_paths:
            if os.path.exists(path):
                valid_paths.append(os.path.abspath(path))
            else:
                return False

        if not valid_paths:
            return False

        if not _PYOBJC_AVAILABLE:
            # macOS: 使用 osascript 设置文件 URL
            try:
                urls = ", ".join(f'POSIX file "{p}"' for p in valid_paths)
                script = f'set the clipboard to {{{urls}}}'
                subprocess.run(["osascript", "-e", script], timeout=5)
                return True
            except Exception:
                return False

        try:
            pasteboard = Cocoa.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            nsurls = [Cocoa.NSURL.fileURLWithPath_(p) for p in valid_paths]
            pasteboard.writeObjects_(nsurls)
            return True
        except Exception:
            return False

    def set_html(self, html: str) -> bool:
        """设置 HTML 格式内容到剪贴板。"""
        if not _PYOBJC_AVAILABLE:
            # Fallback: 仅设置纯文本
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            return self.set_text(soup.get_text(separator="\n"))

        try:
            pasteboard = Cocoa.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()

            # 设置 HTML
            pasteboard.setString_forType_(html, Cocoa.NSPasteboardTypeHTML)

            # 附带纯文本
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            plain = soup.get_text(separator="\n")
            pasteboard.setString_forType_(plain, Cocoa.NSPasteboardTypeString)

            return True
        except Exception:
            return False

    def get_text(self) -> str:
        if not _PYOBJC_AVAILABLE:
            try:
                result = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=5
                )
                return result.stdout
            except Exception:
                return ""

        try:
            pasteboard = Cocoa.NSPasteboard.generalPasteboard()
            val = pasteboard.stringForType_(Cocoa.NSPasteboardTypeString)
            return str(val) if val else ""
        except Exception:
            return ""


# ============================================================================
# MacOSProcess
# ============================================================================

class MacOSProcess(ProcessManager):
    """macOS 进程管理器。"""

    def get_process_name(self, pid: int) -> str:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def get_process_id(self, hwnd: int) -> int:
        element = _get_window_element(hwnd)
        if element is not None:
            try:
                pid_val = _ax_get_attr(element, "AXProcessIdentifier")
                if pid_val:
                    return int(pid_val)
            except Exception:
                pass
        info = _get_cg_window_info(_get_window_cgid(hwnd) or 0)
        if info:
            try:
                return int(info.get("kCGWindowOwnerPID") or 0)
            except Exception:
                pass
        return _find_wechat_pid() or 0

    def get_window_pid(self, hwnd: int) -> int:
        return self.get_process_id(hwnd)

    def restart_process(self, hwnd: int) -> bool:
        pid = self.get_process_id(hwnd)
        app_path = self._get_wechat_app_path()
        if not pid and not app_path:
            return False

        try:
            if pid:
                subprocess.run(["kill", "-9", str(pid)], timeout=5)
                time.sleep(2.0)

            if app_path:
                subprocess.Popen(
                    ["open", "-a", app_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["open", "-a", "WeChat"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            time.sleep(2.0)
            return True
        except Exception:
            return False

    @staticmethod
    def _get_wechat_app_path() -> Optional[str]:
        candidates = [
            "/Applications/WeChat.app",
            os.path.expanduser("~/Applications/WeChat.app"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None


# ============================================================================
# MacOSSystemAccessibility
# ============================================================================

class MacOSSystemAccessibility(SystemAccessibility):
    """macOS 系统辅助功能管理。"""

    def check_accessibility_permission(self) -> bool:
        return check_accessibility_permission()

    def ensure_accessibility_environment(self) -> str:
        """确保 macOS 辅助功能环境已配置。

        Returns:
            "unchanged": 已授权
            "unauthorized": 需要用户手动授权
            "granted_after_prompt": 刚获得授权
        """
        if check_accessibility_permission():
            return "unchanged"

        # 尝试弹出系统偏好设置
        _prompt_accessibility_permission()

        # 等待用户授权（最多等 30 秒）
        for _ in range(60):
            time.sleep(0.5)
            if check_accessibility_permission():
                return "granted_after_prompt"

        return "unauthorized"

    def restore_from_tray(self, hwnd: int) -> bool:
        """通过 macOS 菜单栏/Dock 恢复微信窗口。

        使用 AppleScript 激活微信并取消最小化。
        """
        try:
            script = '''
            tell application "WeChat"
                activate
                reopen
            end tell
            '''
            NSAppleScript.alloc().initWithSource_(script).executeAndReturnError_(None)
            time.sleep(0.5)
            return True
        except Exception:
            pass
        return False


# ============================================================================
# macOS 后端总入口
# ============================================================================

class MacOSBackend(PlatformBackend):
    """macOS 平台后端。"""

    def __init__(self):
        if not _PYOBJC_AVAILABLE:
            logger.warning(
                "pyobjc-framework-Quartz 未安装。"
                "请运行: pip install pyobjc-framework-Quartz"
            )
        self._window_manager = MacOSWindowManager()
        self._automation = MacOSAutomation()
        self._input = MacOSInput()
        self._clipboard = MacOSClipboard()
        self._process = MacOSProcess()
        self._accessibility = MacOSSystemAccessibility()

    @property
    def window_manager(self) -> WindowManager:
        return self._window_manager

    @property
    def automation(self) -> AutomationEngine:
        return self._automation

    @property
    def input(self) -> InputSimulator:
        return self._input

    @property
    def clipboard(self) -> ClipboardManager:
        return self._clipboard

    @property
    def process(self) -> ProcessManager:
        return self._process

    @property
    def accessibility(self) -> SystemAccessibility:
        return self._accessibility

    @property
    def platform_name(self) -> str:
        return "darwin"
