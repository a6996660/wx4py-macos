# -*- coding: utf-8 -*-
"""macOS 平台抽象接口。

这些接口保留了少量 UIAutomation 风格方法名，方便上层页面对象继续用
``ButtonControl``/``EditControl``/``Exists`` 这样的调用方式访问 macOS AX 控件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple


# ============================================================================
# 平台无关的数据类型
# ============================================================================

class _LazyControl:
    """延迟控件查找器，支持链式调用和 .Exists() 超时搜索。

    在首次属性访问或调用 .Exists() 时才执行实际查找。

    构造时仅记录搜索条件，不执行查找。调用 .Exists() 或访问任何
    UI 属性时触发 _resolve() 进行实际控件查找。
    """

    _NOT_FOUND = object()

    def __init__(
        self,
        backend: "AutomationEngine",
        parent: "PlatformControl",
        control_type: str,
        **search_kwargs,
    ):
        self._backend = backend
        self._parent = parent
        self._control_type = control_type
        self._search_kwargs = search_kwargs
        self._resolved = None       # None=未解析, PlatformControl=已找到, _NOT_FOUND=已查找未找到
        self._resolving = False     # 防递归标志

    @staticmethod
    def _normalize_search_kwargs(search_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """兼容历史调用里的 PascalCase 查找参数。"""
        key_map = {
            "Name": "name",
            "ClassName": "class_name",
            "AutomationId": "automation_id",
            "searchDepth": "search_depth",
            "SearchDepth": "search_depth",
            "maxSearchSeconds": "timeout",
        }
        normalized: Dict[str, Any] = {}
        for key, value in search_kwargs.items():
            normalized[key_map.get(key, key)] = value
        return normalized

    # —— 延迟解析 ——

    def _resolve(self):
        """执行实际控件查找，结果缓存。

        Returns:
            PlatformControl 实例（找到时）或 None（未找到）。
        """
        if self._resolved is self._NOT_FOUND:
            return None
        if self._resolved is not None:
            return self._resolved

        if self._resolving:
            return None

        self._resolving = True
        try:
            parent = self._parent
            if isinstance(parent, _LazyControl):
                parent = parent._resolve()
                if parent is None:
                    self._resolved = self._NOT_FOUND
                    return None

            # 构造搜索参数：将 name/class_name/automation_id 等关键字
            # 与构造时保存的 kwargs 合并，传递给后端 find_control
            result = self._backend.find_control(
                parent,
                control_type=self._control_type,
                **self._normalize_search_kwargs(self._search_kwargs),
            )
            if result is not None:
                self._resolved = result
                return result
            self._resolved = self._NOT_FOUND
            return None
        finally:
            self._resolving = False

    # —— Exists 兼容接口 ——

    def Exists(self, maxSearchSeconds: float = 0) -> bool:
        """检查控件是否存在，支持带超时的重试搜索。"""
        resolved = self._resolve()
        if resolved is not None:
            return True
        if maxSearchSeconds > 0:
            import time
            deadline = time.time() + maxSearchSeconds
            while time.time() < deadline:
                # 重置 resolved 状态以允许重新搜索
                if self._resolved is self._NOT_FOUND:
                    self._resolved = None
                if self._resolve() is not None:
                    return True
                time.sleep(0.2)
            return False
        return False

    # —— 属性/方法代理 ——

    def __getattr__(self, name: str):
        """将属性访问代理到已解析的 PlatformControl。

        当控件未找到时返回兼容的桩值/无操作函数。
        """
        # Python 内置属性不走代理
        if name.startswith("_"):
            raise AttributeError(name)

        resolved = self._resolve()
        if resolved is not None:
            return getattr(resolved, name)

        # —— 控件未找到时的桩行为 ——
        if name == "Name":
            return ""
        if name in ("ClassName", "AutomationId", "ControlTypeName"):
            return ""
        if name == "BoundingRectangle":
            return None
        if name == "IsSelected":
            return False
        if name in ("native",):
            return None

        # 工厂方法：未找到时返回一个同样无法解析的新 _LazyControl
        if name == "_find_child":
            return lambda control_type, **kw: _LazyControl(
                self._backend, self, control_type, **kw
            )

        # 通用方法 stub
        if name == "Exists":
            return lambda *a, **kw: False
        if name in ("GetChildren",):
            return lambda: []
        if name == "GetParentControl":
            return lambda: None
        if name == "GetRuntimeId":
            return lambda: ()
        if name == "GetPattern":
            return lambda pattern_id: None

        # Click, DoubleClick, SendKeys, SetFocus 等无操作方法
        return lambda *a, **kw: None

    # —— UIA 风格的控件查找工厂方法 ——
    # 无论当前控件是否已解析，工厂方法总是返回新的 _LazyControl，
    # 支持 chain call: root.ListControl(...).EditControl(...).Exists(...)

    def _find_child(self, control_type: str, **kwargs) -> "_LazyControl":
        return _LazyControl(self._backend, self, control_type, **kwargs)

    def ListControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("List", **kwargs)

    def EditControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Edit", **kwargs)

    def ButtonControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Button", **kwargs)

    def WindowControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Window", **kwargs)

    def GroupControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Group", **kwargs)

    def PaneControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Pane", **kwargs)

    def TextControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Text", **kwargs)

    def CheckBoxControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("CheckBox", **kwargs)

    def ListItemControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("ListItem", **kwargs)

    def CustomControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Custom", **kwargs)

    def DataGridControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("DataGrid", **kwargs)

    def TabControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Tab", **kwargs)

    def TreeControl(self, **kwargs) -> "_LazyControl":
        return self._find_child("Tree", **kwargs)

    def __repr__(self) -> str:
        try:
            resolved = self._resolve()
            if resolved is not None:
                return repr(resolved)
        except Exception:
            pass
        return f"<_LazyControl control_type={self._control_type!r} (not resolved)>"


class PlatformControl:
    """macOS AXUIElement 的轻量包装。"""

    def __init__(self, backend: "AutomationEngine", native: Any):
        self._backend = backend
        self._native = native

    @property
    def native(self) -> Any:
        """获取底层原生控件对象。"""
        return self._native

    @property
    def Name(self) -> str:
        return self._backend.get_name(self)

    @property
    def ClassName(self) -> str:
        return self._backend.get_class_name(self)

    @property
    def AutomationId(self) -> str:
        return self._backend.get_automation_id(self)

    @property
    def ControlTypeName(self) -> str:
        return self._backend.get_control_type_name(self)

    @property
    def BoundingRectangle(self):
        return self._backend.get_bounding_rectangle(self)

    @property
    def IsSelected(self) -> bool:
        return self._backend.get_is_selected(self)

    def Click(self, simulateMove: bool = True) -> None:
        self._backend.click(self, simulateMove=simulateMove)

    def DoubleClick(self, simulateMove: bool = True) -> None:
        self._backend.double_click(self, simulateMove=simulateMove)

    def SendKeys(self, text: str) -> None:
        self._backend.send_keys(self, text)

    def SetFocus(self) -> None:
        self._backend.set_focus(self)

    def GetChildren(self) -> List["PlatformControl"]:
        return self._backend.get_children(self)

    def GetParentControl(self) -> Optional["PlatformControl"]:
        return self._backend.get_parent(self)

    def GetRuntimeId(self) -> Tuple[int, ...]:
        return self._backend.get_runtime_id(self)

    def GetPattern(self, pattern_id: Any) -> Any:
        return self._backend.get_pattern(self, pattern_id)

    def Exists(self, maxSearchSeconds: float = 0) -> bool:
        return self._backend.exists(self, maxSearchSeconds)

    # --- UIA 风格的控件查找工厂方法 ---
    # 内部委托给 macOS 后端的 find_control()。

    def _find_child(self, control_type: str, **kwargs) -> "PlatformControl":
        """通用子控件查找，返回 PlatformControl 或包装的 'Control' 对象。

        返回的对象支持 .Exists(maxSearchSeconds=...)、.Name 等属性访问。
        如果控件不存在，返回一个 Exists() 返回 False 的桩对象。
        """
        return _LazyControl(self._backend, self, control_type, **kwargs)

    def ListControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("List", **kwargs)

    def EditControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Edit", **kwargs)

    def ButtonControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Button", **kwargs)

    def WindowControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Window", **kwargs)

    def GroupControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Group", **kwargs)

    def PaneControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Pane", **kwargs)

    def TextControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Text", **kwargs)

    def CheckBoxControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("CheckBox", **kwargs)

    def ListItemControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("ListItem", **kwargs)

    def CustomControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Custom", **kwargs)

    def DataGridControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("DataGrid", **kwargs)

    def TabControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Tab", **kwargs)

    def TreeControl(self, **kwargs) -> "PlatformControl":
        return self._find_child("Tree", **kwargs)

    def __repr__(self) -> str:
        try:
            return f"<PlatformControl name={self.Name!r} class={self.ClassName!r}>"
        except Exception:
            return "<PlatformControl (unknown)>"


class BoundingRect:
    """平台无关的矩形坐标。"""
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_x(self) -> int:
        return (self.left + self.right) // 2

    @property
    def center_y(self) -> int:
        return (self.top + self.bottom) // 2

    def __repr__(self) -> str:
        return f"BoundingRect(l={self.left}, t={self.top}, r={self.right}, b={self.bottom})"


# ============================================================================
# UIA Pattern 常量（保留历史数值，供 groups.py 的 Toggle/Value 封装使用）
# ============================================================================

class ToggleState:
    """Toggle 状态枚举。"""
    Off = 0
    On = 1
    Indeterminate = 2


class PatternId:
    """UIAutomation Pattern ID 常量。"""
    InvokePattern = 10000
    SelectionPattern = 10001
    ValuePattern = 10002
    RangeValuePattern = 10003
    ScrollPattern = 10004
    ExpandCollapsePattern = 10005
    GridPattern = 10006
    GridItemPattern = 10007
    MultipleViewPattern = 10008
    WindowPattern = 10009
    SelectionItemPattern = 10010
    DockPattern = 10011
    TablePattern = 10012
    TableItemPattern = 10013
    TextPattern = 10014
    TogglePattern = 10015
    TransformPattern = 10016
    ScrollItemPattern = 10017
    LegacyAccessiblePattern = 10018
    ItemContainerPattern = 10019
    VirtualizedItemPattern = 10020
    SyncInputPattern = 10021
    ObjectModelPattern = 10022
    AnnotationPattern = 10023
    TextPattern2 = 10024
    StylesPattern = 10025
    SpreadsheetPattern = 10026
    SpreadsheetItemPattern = 10027
    TransformPattern2 = 10028
    TextChildPattern = 10029
    DragPattern = 10030
    DropTargetPattern = 10031
    TextEditPattern = 10032
    CustomNavigationPattern = 10033


# ============================================================================
# 抽象接口
# ============================================================================


class WindowManager(ABC):
    """窗口管理抽象接口。

    负责查找、激活、枚举微信窗口。
    """

    @abstractmethod
    def find_wechat_window(self) -> Optional[int]:
        """查找微信主窗口句柄/ID。

        Returns:
            macOS 窗口 ID，未找到返回 None。
        """
        ...

    @abstractmethod
    def bring_to_front(self, hwnd: int) -> bool:
        """将窗口置于前台。

        Args:
            hwnd: 窗口句柄/ID

        Returns:
            成功返回 True。
        """
        ...

    @abstractmethod
    def get_title(self, hwnd: int) -> str:
        """获取窗口标题。"""
        ...

    @abstractmethod
    def get_class(self, hwnd: int) -> str:
        """获取窗口角色或进程名。"""
        ...

    @abstractmethod
    def is_visible(self, hwnd: int) -> bool:
        """检查窗口是否可见。"""
        ...

    @abstractmethod
    def minimize(self, hwnd: int) -> bool:
        """最小化窗口。"""
        ...

    @abstractmethod
    def enum_window_handles(self, callback: Callable, extra: Any = None) -> None:
        """枚举所有顶层窗口。

        Args:
            callback: 回调函数 callback(hwnd, extra) -> bool
            extra: 额外参数透传给回调
        """
        ...

    @abstractmethod
    def enum_child_window_handles(self, parent: int) -> List[int]:
        """枚举指定窗口的所有子窗口。

        Returns:
            子窗口句柄列表。
        """
        ...


class AutomationEngine(ABC):
    """UI Automation 引擎抽象接口。

    负责查找和操作微信 UI 控件。
    """

    @abstractmethod
    def control_from_handle(self, hwnd: int) -> Optional[PlatformControl]:
        """从窗口句柄/ID 创建根控件。

        Returns:
            PlatformControl 实例，失败返回 None。
        """
        ...

    @abstractmethod
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
        """在控件树中查找指定控件。

        Args:
            root: 根控件
            control_type: 控件类型过滤
            name: 控件名称过滤
            class_name: 控件类名过滤
            automation_id: 自动化 ID 过滤
            search_depth: 搜索深度
            timeout: 超时时间（秒）

        Returns:
            找到的控件，未找到返回 None（超时后仍找不到时抛异常）。
        """
        ...

    @abstractmethod
    def find_all_controls(
        self,
        root: PlatformControl,
        control_type: str = None,
        search_depth: int = None,
        **filters,
    ) -> List[PlatformControl]:
        """查找所有匹配的控件。"""
        ...

    @abstractmethod
    def walk_control(
        self,
        root: PlatformControl,
        include_top: bool = True,
        max_depth: int = 6,
    ) -> List[Tuple[PlatformControl, int]]:
        """遍历控件树。

        Returns:
            (control, depth) 的列表。
        """
        ...

    @abstractmethod
    def get_focused_control(self) -> Optional[PlatformControl]:
        """获取当前键盘焦点控件。"""
        ...

    # --- 控件属性读取 ---

    @abstractmethod
    def get_name(self, control: PlatformControl) -> str:
        ...

    @abstractmethod
    def get_class_name(self, control: PlatformControl) -> str:
        ...

    @abstractmethod
    def get_automation_id(self, control: PlatformControl) -> str:
        ...

    @abstractmethod
    def get_control_type_name(self, control: PlatformControl) -> str:
        ...

    @abstractmethod
    def get_bounding_rectangle(self, control: PlatformControl) -> Optional[BoundingRect]:
        ...

    @abstractmethod
    def get_is_selected(self, control: PlatformControl) -> bool:
        ...

    # --- 控件操作 ---

    @abstractmethod
    def click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        """点击控件。"""
        ...

    @abstractmethod
    def double_click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        """双击控件。"""
        ...

    @abstractmethod
    def send_keys(self, control: PlatformControl, text: str) -> bool:
        """向控件发送按键文本。"""
        ...

    @abstractmethod
    def set_focus(self, control: PlatformControl) -> None:
        """设置键盘焦点到控件。"""
        ...

    @abstractmethod
    def get_children(self, control: PlatformControl) -> List[PlatformControl]:
        """获取子控件列表。"""
        ...

    @abstractmethod
    def get_parent(self, control: PlatformControl) -> Optional[PlatformControl]:
        """获取父控件。"""
        ...

    @abstractmethod
    def get_runtime_id(self, control: PlatformControl) -> Tuple[int, ...]:
        """获取控件运行时唯一标识。"""
        ...

    @abstractmethod
    def get_pattern(self, control: PlatformControl, pattern_id: Any) -> Any:
        """获取 UIA Pattern（如 TogglePattern、ValuePattern）。

        macOS 端返回对应 NSAccessibility 属性封装。
        """
        ...

    @abstractmethod
    def exists(self, control: PlatformControl, max_search_seconds: float = 0) -> bool:
        """检查控件是否存在。"""
        ...


class InputSimulator(ABC):
    """输入模拟抽象接口。

    负责键盘、鼠标操作的平台实现。
    """

    @abstractmethod
    def key_down(self, key_code: int) -> None:
        """按下按键。"""
        ...

    @abstractmethod
    def key_up(self, key_code: int) -> None:
        """释放按键。"""
        ...

    @abstractmethod
    def send_combo(self, modifier: int, key: int, settle_time: float = 0.3) -> None:
        """发送组合键（如 Ctrl+C）。

        Args:
            modifier: 修饰键码（如 VK_CONTROL）
            key: 目标按键码
            settle_time: 操作后等待时间
        """
        ...

    @abstractmethod
    def mouse_click(self, x: int, y: int) -> None:
        """在屏幕坐标处点击。"""
        ...

    @abstractmethod
    def mouse_dblclick(self, x: int, y: int) -> None:
        """在屏幕坐标处双击。"""
        ...

    @abstractmethod
    def mouse_down(self, x: int, y: int) -> None:
        """在屏幕坐标处按下鼠标左键。"""
        ...

    @abstractmethod
    def mouse_up(self, x: int, y: int) -> None:
        """在屏幕坐标处释放鼠标左键。"""
        ...

    @abstractmethod
    def mouse_scroll(self, delta: int, steps: int, step_delay: float = 0.1) -> None:
        """在当前光标位置滚动鼠标滚轮。

        Args:
            delta: 滚动增量（正=向上，负=向下，通常 120 为一档）
            steps: 滚动步数
            step_delay: 步间延迟
        """
        ...

    @abstractmethod
    def set_cursor(self, x: int, y: int) -> None:
        """设置鼠标光标位置。"""
        ...

    # --- 通用虚拟键码常量 ---
    # 这些常量是上层跨模块约定，macOS 后端会映射为 CGEvent 键码。
    VK_CONTROL = 0x11
    VK_RETURN = 0x0D
    VK_TAB = 0x09
    VK_SPACE = 0x20
    VK_ESCAPE = 0x1B
    VK_SHIFT = 0x10
    VK_DELETE = 0x2E
    VK_V = 0x56
    VK_A = 0x41
    VK_C = 0x43
    VK_F = 0x46


class ClipboardManager(ABC):
    """剪贴板管理抽象接口。"""

    @abstractmethod
    def set_text(self, text: str) -> bool:
        """设置纯文本到剪贴板。"""
        ...

    @abstractmethod
    def set_files(self, file_paths) -> bool:
        """设置文件列表到剪贴板（用于粘贴文件）。

        Args:
            file_paths: 文件路径字符串或路径列表

        Returns:
            成功返回 True。
        """
        ...

    @abstractmethod
    def set_html(self, html: str) -> bool:
        """设置 HTML 格式内容到剪贴板（同时附带纯文本回退）。

        Args:
            html: HTML 内容

        Returns:
            成功返回 True。
        """
        ...

    @abstractmethod
    def get_text(self) -> str:
        """获取剪贴板中的纯文本。"""
        ...


class ProcessManager(ABC):
    """进程管理抽象接口。"""

    @abstractmethod
    def get_process_name(self, pid: int) -> str:
        """通过 PID 获取进程可执行文件路径。"""
        ...

    @abstractmethod
    def get_process_id(self, hwnd: int) -> int:
        """通过窗口句柄获取进程 ID。"""
        ...

    @abstractmethod
    def restart_process(self, hwnd: int) -> bool:
        """重启指定窗口对应的进程。

        Args:
            hwnd: 窗口句柄/ID

        Returns:
            成功返回 True。
        """
        ...

    @abstractmethod
    def get_window_pid(self, hwnd: int) -> int:
        """获取窗口所属进程的 PID。"""
        ...


class SystemAccessibility(ABC):
    """macOS 辅助功能权限抽象接口。"""

    @abstractmethod
    def check_accessibility_permission(self) -> bool:
        """检查当前进程是否拥有辅助功能权限。

        Returns:
            已授权返回 True。
        """
        ...

    @abstractmethod
    def ensure_accessibility_environment(self) -> str:
        """确保辅助功能环境已配置。

        Returns:
            "unchanged": 无需修改
            "fixed": 已修复但可能需要重启目标应用
            "unauthorized": 需要用户手动授权
        """
        ...

    @abstractmethod
    def restore_from_tray(self, hwnd: int) -> bool:
        """通过 Dock/AppleScript 恢复微信窗口。

        Args:
            hwnd: 当前微信窗口句柄（可能不可见）

        Returns:
            恢复成功返回 True。
        """
        ...


# ============================================================================
# 平台后端总入口
# ============================================================================


class PlatformBackend(ABC):
    """平台后端总入口抽象类。

    聚合所有子接口，由具体平台实现。
    """

    @property
    @abstractmethod
    def window_manager(self) -> WindowManager:
        ...

    @property
    @abstractmethod
    def automation(self) -> AutomationEngine:
        ...

    @property
    @abstractmethod
    def input(self) -> InputSimulator:
        ...

    @property
    @abstractmethod
    def clipboard(self) -> ClipboardManager:
        ...

    @property
    @abstractmethod
    def process(self) -> ProcessManager:
        ...

    @property
    @abstractmethod
    def accessibility(self) -> SystemAccessibility:
        ...

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台名称：'darwin'。"""
        ...
