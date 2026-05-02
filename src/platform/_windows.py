# -*- coding: utf-8 -*-
"""Windows 平台后端实现。

将现有 Windows 专有代码（win32api, uiautomation, 注册表, 托盘等）
封装为平台抽象层接口的具体实现。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import struct
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import win32api
import win32clipboard
import win32con
import win32gui
import win32process
import winreg

from ._base import (
    AutomationEngine,
    BoundingRect,
    ClipboardManager,
    InputSimulator,
    PlatformBackend,
    PlatformControl,
    ProcessManager,
    SystemAccessibility,
    TrayButtonData,
    WindowManager,
)

# ---- 内部导入 uiautomation ----
from ..core import uiautomation as uia

# ---- 日志 ----
from ..utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# Windows PlatformControl 适配
# ============================================================================

class _WindowsControlAdapter:
    """将 Windows uiautomation Control 适配为 PlatformControl 的操作。"""

    @staticmethod
    def get_name(ctrl: PlatformControl) -> str:
        try:
            return str(ctrl._native.Name or "")
        except Exception:
            return ""

    @staticmethod
    def get_class_name(ctrl: PlatformControl) -> str:
        try:
            return str(ctrl._native.ClassName or "")
        except Exception:
            return ""

    @staticmethod
    def get_automation_id(ctrl: PlatformControl) -> str:
        try:
            return str(ctrl._native.AutomationId or "")
        except Exception:
            return ""

    @staticmethod
    def get_control_type_name(ctrl: PlatformControl) -> str:
        try:
            return str(ctrl._native.ControlTypeName or "")
        except Exception:
            return ""

    @staticmethod
    def get_bounding_rectangle(ctrl: PlatformControl) -> Optional[BoundingRect]:
        try:
            r = ctrl._native.BoundingRectangle
            if r is None:
                return None
            return BoundingRect(
                left=int(r.left),
                top=int(r.top),
                right=int(r.right),
                bottom=int(r.bottom),
            )
        except Exception:
            return None

    @staticmethod
    def get_is_selected(ctrl: PlatformControl) -> bool:
        try:
            return bool(ctrl._native.IsSelected)
        except Exception:
            return False

    @staticmethod
    def click(ctrl: PlatformControl, simulateMove: bool = True) -> bool:
        try:
            ctrl._native.Click(simulateMove=simulateMove)
            return True
        except Exception:
            return False

    @staticmethod
    def double_click(ctrl: PlatformControl, simulateMove: bool = True) -> bool:
        try:
            ctrl._native.DoubleClick(simulateMove=simulateMove)
            return True
        except Exception:
            return False

    @staticmethod
    def send_keys(ctrl: PlatformControl, text: str) -> bool:
        try:
            ctrl._native.SendKeys(text)
            return True
        except Exception:
            return False

    @staticmethod
    def set_focus(ctrl: PlatformControl) -> None:
        try:
            ctrl._native.SetFocus()
        except Exception:
            pass

    @staticmethod
    def get_children(ctrl: PlatformControl) -> List[PlatformControl]:
        result = []
        try:
            for child in ctrl._native.GetChildren():
                result.append(PlatformControl(ctrl._backend, child))
        except Exception:
            pass
        return result

    @staticmethod
    def get_parent(ctrl: PlatformControl) -> Optional[PlatformControl]:
        try:
            parent = ctrl._native.GetParentControl()
            if parent:
                return PlatformControl(ctrl._backend, parent)
        except Exception:
            pass
        return None

    @staticmethod
    def get_runtime_id(ctrl: PlatformControl) -> Tuple[int, ...]:
        try:
            return tuple(ctrl._native.GetRuntimeId() or ())
        except Exception:
            return ()

    @staticmethod
    def get_pattern(ctrl: PlatformControl, pattern_id: Any) -> Any:
        try:
            return ctrl._native.GetPattern(pattern_id)
        except Exception:
            return None

    @staticmethod
    def exists(ctrl: PlatformControl, max_search_seconds: float = 0) -> bool:
        try:
            return ctrl._native.Exists(maxSearchSeconds=max_search_seconds)
        except Exception:
            return False


# ============================================================================
# WindowsWindowManager
# ============================================================================

class WindowsWindowManager(WindowManager):
    """Windows 窗口管理器。"""

    # 微信窗口评分
    WECHAT_EXE_NAMES = {"wechat.exe", "weixin.exe"}
    WECHAT_TEXT_HINTS = ("WeChat", "Weixin", "微信")

    def find_wechat_window(self) -> Optional[int]:
        return _find_wechat_window_native()

    def bring_to_front(self, hwnd: int) -> bool:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    def get_title(self, hwnd: int) -> str:
        return win32gui.GetWindowText(hwnd)

    def get_class(self, hwnd: int) -> str:
        return win32gui.GetClassName(hwnd)

    def is_visible(self, hwnd: int) -> bool:
        try:
            return win32gui.IsWindowVisible(hwnd) != 0
        except Exception:
            return False

    def minimize(self, hwnd: int) -> bool:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            return True
        except Exception:
            return False

    def enum_windows(self, callback: Callable, extra: Any = None) -> None:
        win32gui.EnumWindows(callback, extra)

    def enum_child_windows(self, parent: int) -> List[int]:
        result: List[int] = []

        def _cb(hwnd, _):
            result.append(hwnd)
            return True

        win32gui.EnumChildWindows(parent, _cb, 0)
        return result


# ---- 原生窗口查找（从 win32.py 迁移） ----

def _get_process_image_name(pid: int) -> str:
    """通过 pid 获取进程可执行文件完整路径。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    query_name.restype = ctypes.c_int

    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return ""

    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(1024)
        ok = query_name(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        close_handle(handle)


def _wechat_window_score(hwnd: int, title: str, class_name: str, exe_path: str) -> int:
    score = 0
    exe_name = os.path.basename(exe_path).lower()

    if exe_name in {"weixin.exe", "wechat.exe"}:
        score += 100
    if exe_name == "wechatappex.exe":
        score -= 200

    if class_name.startswith("Qt"):
        score += 30
    if "微信" in title:
        score += 10

    if not win32gui.IsWindowVisible(hwnd):
        score -= 20

    return score


def _find_wechat_window_native() -> Optional[int]:
    """原生查找微信主窗口句柄。"""
    candidates: List[Tuple[int, int, str, str, str]] = []

    def _enum_cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd) or ""
        class_name = win32gui.GetClassName(hwnd) or ""

        if ("微信" not in title) and (not class_name.startswith("Qt")) and ("WeChat" not in class_name):
            return True

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe_path = _get_process_image_name(pid)
        except Exception:
            pid = 0
            exe_path = ""

        score = _wechat_window_score(hwnd, title, class_name, exe_path)
        if score > -150:
            candidates.append((score, hwnd, title, class_name, exe_path))
        return True

    win32gui.EnumWindows(_enum_cb, None)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", None)
    if hwnd:
        return hwnd

    hwnd = win32gui.FindWindow(None, "微信")
    if hwnd:
        return hwnd

    return None


# ============================================================================
# WindowsAutomation
# ============================================================================

class WindowsAutomation(AutomationEngine):
    """Windows UIAutomation 引擎。"""

    def control_from_handle(self, hwnd: int) -> Optional[PlatformControl]:
        try:
            native = uia.ControlFromHandle(hwnd)
            if native:
                return PlatformControl(self, native)
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
        kwargs = {"searchDepth": search_depth or 10}

        if name:
            kwargs["Name"] = name
        if class_name:
            kwargs["ClassName"] = class_name
        if automation_id:
            kwargs["AutomationId"] = automation_id

        control_type = control_type or "Control"
        getter = getattr(root._native, f"{control_type}Control", None)
        if not getter:
            getter = root._native.Control

        native = getter(**kwargs)
        timeout = timeout or 5
        if native.Exists(maxSearchSeconds=timeout):
            return PlatformControl(self, native)
        return None

    def find_all_controls(
        self,
        root: PlatformControl,
        control_type: str = None,
        search_depth: int = None,
        **filters,
    ) -> List[PlatformControl]:
        getter = getattr(root._native, f"{control_type}Control", root._native.Control)
        kwargs = {"searchDepth": search_depth or 10, **filters}
        native = getter(**kwargs)
        if native.Exists():
            return [PlatformControl(self, c) for c in native.GetChildren()]
        return []

    def walk_control(
        self,
        root: PlatformControl,
        include_top: bool = True,
        max_depth: int = 6,
    ) -> List[Tuple[PlatformControl, int]]:
        result = []
        try:
            for control, depth in uia.WalkControl(
                root._native, includeTop=include_top, maxDepth=max_depth
            ):
                result.append((PlatformControl(self, control), depth))
        except Exception:
            pass
        return result

    def get_focused_control(self) -> Optional[PlatformControl]:
        try:
            native = uia.GetFocusedControl()
            if native:
                return PlatformControl(self, native)
        except Exception:
            pass
        return None

    # --- 控件代理到 _WindowsControlAdapter ---

    def get_name(self, control: PlatformControl) -> str:
        return _WindowsControlAdapter.get_name(control)

    def get_class_name(self, control: PlatformControl) -> str:
        return _WindowsControlAdapter.get_class_name(control)

    def get_automation_id(self, control: PlatformControl) -> str:
        return _WindowsControlAdapter.get_automation_id(control)

    def get_control_type_name(self, control: PlatformControl) -> str:
        return _WindowsControlAdapter.get_control_type_name(control)

    def get_bounding_rectangle(self, control: PlatformControl) -> Optional[BoundingRect]:
        return _WindowsControlAdapter.get_bounding_rectangle(control)

    def get_is_selected(self, control: PlatformControl) -> bool:
        return _WindowsControlAdapter.get_is_selected(control)

    def click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        return _WindowsControlAdapter.click(control, simulateMove)

    def double_click(self, control: PlatformControl, simulateMove: bool = True) -> bool:
        return _WindowsControlAdapter.double_click(control, simulateMove)

    def send_keys(self, control: PlatformControl, text: str) -> bool:
        return _WindowsControlAdapter.send_keys(control, text)

    def set_focus(self, control: PlatformControl) -> None:
        _WindowsControlAdapter.set_focus(control)

    def get_children(self, control: PlatformControl) -> List[PlatformControl]:
        return _WindowsControlAdapter.get_children(control)

    def get_parent(self, control: PlatformControl) -> Optional[PlatformControl]:
        return _WindowsControlAdapter.get_parent(control)

    def get_runtime_id(self, control: PlatformControl) -> Tuple[int, ...]:
        return _WindowsControlAdapter.get_runtime_id(control)

    def get_pattern(self, control: PlatformControl, pattern_id: Any) -> Any:
        return _WindowsControlAdapter.get_pattern(control, pattern_id)

    def exists(self, control: PlatformControl, max_search_seconds: float = 0) -> bool:
        return _WindowsControlAdapter.exists(control, max_search_seconds)


# ============================================================================
# WindowsInput
# ============================================================================

class WindowsInput(InputSimulator):
    """Windows 输入模拟器。"""

    def key_down(self, key_code: int) -> None:
        win32api.keybd_event(key_code, 0, 0, 0)

    def key_up(self, key_code: int) -> None:
        win32api.keybd_event(key_code, 0, win32con.KEYEVENTF_KEYUP, 0)

    def send_combo(self, modifier: int, key: int, settle_time: float = 0.3) -> None:
        self.key_down(modifier)
        time.sleep(0.05)
        self.key_down(key)
        time.sleep(0.05)
        self.key_up(key)
        time.sleep(0.05)
        self.key_up(modifier)
        time.sleep(settle_time)

    def mouse_click(self, x: int, y: int) -> None:
        self.set_cursor(x, y)
        time.sleep(0.2)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def mouse_dblclick(self, x: int, y: int) -> None:
        self.set_cursor(x, y)
        for _ in range(2):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.08)

    def mouse_down(self, x: int, y: int) -> None:
        self.set_cursor(x, y)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    def mouse_up(self, x: int, y: int) -> None:
        self.set_cursor(x, y)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def mouse_scroll(self, delta: int, steps: int, step_delay: float = 0.1) -> None:
        for _ in range(steps):
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            time.sleep(step_delay)

    def set_cursor(self, x: int, y: int) -> None:
        win32api.SetCursorPos((x, y))


# ============================================================================
# WindowsClipboard
# ============================================================================

class WindowsClipboard(ClipboardManager):
    """Windows 剪贴板管理器。"""

    def set_text(self, text: str) -> bool:
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            return True
        except Exception:
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    def set_files(self, file_paths) -> bool:
        """将文件以 CF_HDROP 格式设置到剪贴板。"""
        if isinstance(file_paths, str):
            file_paths = [file_paths]

        valid_paths = []
        for path in file_paths:
            if os.path.exists(path):
                valid_paths.append(os.path.abspath(path))
            else:
                return False  # 与原有行为一致

        if not valid_paths:
            return False

        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()

            offset = 20
            dropfiles_header = struct.pack("<LLLLL", offset, 0, 0, 0, 1)

            file_list = []
            for path in valid_paths:
                file_list.append(path.encode("utf-16le"))
                file_list.append(b"\x00\x00")
            file_list.append(b"\x00\x00")

            file_data = b"".join(file_list)
            hdrop_data = dropfiles_header + file_data

            win32clipboard.SetClipboardData(win32con.CF_HDROP, hdrop_data)
            return True
        except Exception:
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    def set_html(self, html: str) -> bool:
        """将 HTML 以 CF_HTML 格式设置到剪贴板（连带纯文本）。"""
        from bs4 import BeautifulSoup

        html_with_fragment = (
            "<!DOCTYPE html>\n<html>\n<head><meta charset=\"utf-8\"></head>\n<body>\n"
            "<!--StartFragment-->\n"
            f"{html}\n"
            "<!--EndFragment-->\n"
            "</body>\n</html>"
        )

        html_bytes = html_with_fragment.encode("utf-8")

        header_template = (
            "Version:0.9\r\n"
            "StartHTML:000000000\r\n"
            "EndHTML:{end_html:09d}\r\n"
            "StartFragment:000000000\r\n"
            "EndFragment:{end_fragment:09d}\r\n"
        )

        start_html = len(header_template.format(end_html=0, end_fragment=0).encode("utf-8"))
        end_html = start_html + len(html_bytes)

        start_fragment = html_with_fragment.find("<!--StartFragment-->")
        end_fragment = html_with_fragment.find("<!--EndFragment-->")

        if start_fragment != -1 and end_fragment != -1:
            start_fragment = start_html + start_fragment + len("<!--StartFragment-->")
            end_fragment = start_html + end_fragment

        header = (
            f"Version:0.9\r\n"
            f"StartHTML:{start_html:09d}\r\n"
            f"EndHTML:{end_html:09d}\r\n"
            f"StartFragment:{start_fragment:09d}\r\n"
            f"EndFragment:{end_fragment:09d}\r\n"
        )

        cf_html_data = header.encode("utf-8") + html_bytes

        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()

            cf_html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
            win32clipboard.SetClipboardData(cf_html_format, cf_html_data)

            soup = BeautifulSoup(html, "html.parser")
            plain_text = soup.get_text(separator="\n")
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, plain_text)

            return True
        except Exception:
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

    def get_text(self) -> str:
        try:
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return data if data else ""
            except Exception:
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return ""


# ============================================================================
# WindowsProcess
# ============================================================================

class WindowsProcess(ProcessManager):
    """Windows 进程管理器。"""

    def get_process_name(self, pid: int) -> str:
        return _get_process_image_name(pid)

    def get_process_id(self, hwnd: int) -> int:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid
        except Exception:
            return 0

    def get_window_pid(self, hwnd: int) -> int:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid
        except Exception:
            return 0

    def restart_process(self, hwnd: int) -> bool:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe_path = _get_process_image_name(pid)
            if not exe_path or not os.path.exists(exe_path):
                return False

            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            time.sleep(1.0)

            subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            return True
        except Exception:
            return False


# ============================================================================
# WindowsSystemAccessibility（注册表 + 屏幕阅读器 + 托盘恢复）
# ============================================================================

class WindowsSystemAccessibility(SystemAccessibility):
    """Windows 系统辅助功能管理。"""

    def check_accessibility_permission(self) -> bool:
        # Windows 没有运行时权限检查，辅助功能通过注册表/系统标志控制
        # 这里检查 RunningState 是否 >= 1
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Narrator\NoRoam",
                0,
                winreg.KEY_READ,
            )
            try:
                value, _ = winreg.QueryValueEx(key, "RunningState")
                winreg.CloseKey(key)
                return value >= 1
            except FileNotFoundError:
                winreg.CloseKey(key)
                return False
        except Exception:
            return True  # 如果读不到，不阻塞流程

    def ensure_accessibility_environment(self) -> str:
        """修复注册表 + 屏幕阅读器标志。

        Returns:
            "unchanged", "fixed_zero", "created_missing"
        """
        # Step 1: 修复注册表
        reg_result = self._check_and_fix_registry()

        # Step 2: 修复屏幕阅读器标志（不决定是否重启）
        try:
            self._ensure_screen_reader_flag()
        except Exception:
            pass

        return reg_result

    @staticmethod
    def _check_and_fix_registry() -> str:
        reg_path = r"SOFTWARE\Microsoft\Narrator\NoRoam"
        key_name = "RunningState"

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                reg_path,
                0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            )

            try:
                value, _ = winreg.QueryValueEx(key, key_name)

                if value == 0:
                    winreg.SetValueEx(key, key_name, 0, winreg.REG_DWORD, 1)
                    winreg.CloseKey(key)
                    return "fixed_zero"

                winreg.CloseKey(key)
                return "unchanged"

            except FileNotFoundError:
                winreg.SetValueEx(key, key_name, 0, winreg.REG_DWORD, 1)
                winreg.CloseKey(key)
                return "created_missing"

        except PermissionError as e:
            from ..core.exceptions import RegistryError
            raise RegistryError(f"访问注册表时权限被拒绝: {e}")
        except Exception as e:
            from ..core.exceptions import RegistryError
            raise RegistryError(f"访问注册表失败: {e}")

    @staticmethod
    def _ensure_screen_reader_flag() -> bool:
        SPI_GETSCREENREADER = 0x0046
        SPI_SETSCREENREADER = 0x0047
        SPIF_UPDATEINIFILE = 0x01
        SPIF_SENDCHANGE = 0x02

        pvParam = ctypes.wintypes.BOOL()
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETSCREENREADER, 0, ctypes.byref(pvParam), 0
        )

        if pvParam.value:
            return False

        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETSCREENREADER, 1, 0, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
        )
        return True

    def restore_from_tray(self, hwnd: int) -> bool:
        """通过原生托盘恢复微信窗口。"""
        return _restore_wechat_from_native_tray()


# ---- 从 tray.py 迁移的原生托盘恢复逻辑 ----

WECHAT_EXE_NAMES = {"wechat.exe", "weixin.exe"}
WECHAT_TEXT_HINTS_TRAY = ("WeChat", "Weixin", "微信")
WM_USER = 0x0400
TB_GETBUTTON = WM_USER + 23
TB_BUTTONCOUNT = WM_USER + 24

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_LIMITED_INFO = 0x1000

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
REMOTE_BUFFER_SIZE = 4096

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = ctypes.c_int
_kernel32.VirtualAllocEx.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32,
]
_kernel32.VirtualAllocEx.restype = ctypes.c_void_p
_kernel32.VirtualFreeEx.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
]
_kernel32.VirtualFreeEx.restype = ctypes.c_int
_kernel32.ReadProcessMemory.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.ReadProcessMemory.restype = ctypes.c_int
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
]
_kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
_user32.SendMessageW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_size_t,
]
_user32.SendMessageW.restype = ctypes.c_size_t

TRAY_RESTORE_EVENTS = (
    (win32con.WM_LBUTTONDOWN,),
    (win32con.WM_LBUTTONUP,),
    (win32con.WM_LBUTTONDBLCLK,),
)


def _open_toolbar_process(pid: int) -> Optional[int]:
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFO | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
        0, pid,
    )
    return int(handle) if handle else None


def _close_handle(handle: int) -> None:
    if handle:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))


def _read_remote(handle: int, address: int, size: int) -> bytes:
    if not address:
        return b""
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(
        ctypes.c_void_p(handle), ctypes.c_void_p(address), buf, size, ctypes.byref(read),
    )
    return buf.raw[:read.value] if ok else b""


def _parse_tbbutton(data: bytes):
    if ctypes.sizeof(ctypes.c_void_p) == 8 and len(data) >= 32:
        id_command = struct.unpack_from("<i", data, 4)[0]
        dw_data = struct.unpack_from("<Q", data, 16)[0]
        return id_command, dw_data
    if len(data) >= 20:
        id_command = struct.unpack_from("<i", data, 4)[0]
        dw_data = struct.unpack_from("<I", data, 12)[0]
        return id_command, dw_data
    return None


def _parse_traydata_candidates(data: bytes) -> List[Tuple[int, int, int]]:
    candidates = []
    if ctypes.sizeof(ctypes.c_void_p) == 8 and len(data) >= 24:
        candidates.append((
            struct.unpack_from("<Q", data, 0)[0],
            struct.unpack_from("<I", data, 8)[0],
            struct.unpack_from("<I", data, 12)[0],
        ))
        if len(data) >= 32:
            candidates.append((
                struct.unpack_from("<Q", data, 8)[0],
                struct.unpack_from("<I", data, 16)[0],
                struct.unpack_from("<I", data, 20)[0],
            ))
    if len(data) >= 12:
        candidates.append((
            struct.unpack_from("<I", data, 0)[0],
            struct.unpack_from("<I", data, 4)[0],
            struct.unpack_from("<I", data, 8)[0],
        ))
    return candidates


def _is_likely_wechat_target(hwnd: int) -> Tuple[bool, str, str, str]:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False, "", "", ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe_path = _get_process_image_name(pid)
        title = win32gui.GetWindowText(hwnd) or ""
        class_name = win32gui.GetClassName(hwnd) or ""
    except Exception:
        return False, "", "", ""
    exe_name = os.path.basename(exe_path).lower()
    text = f"{exe_name} {title} {class_name}"
    matched = exe_name in WECHAT_EXE_NAMES or any(hint in text for hint in WECHAT_TEXT_HINTS_TRAY)
    return matched, exe_path, title, class_name


def _read_toolbar_buttons(toolbar_hwnd: int, toolbar_pid: int) -> List[TrayButtonData]:
    handle = _open_toolbar_process(toolbar_pid)
    if not handle:
        return []

    remote = _kernel32.VirtualAllocEx(
        ctypes.c_void_p(handle), None, REMOTE_BUFFER_SIZE, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE,
    )
    if not remote:
        _close_handle(handle)
        return []

    buttons = []
    seen = set()
    try:
        count = _user32.SendMessageW(ctypes.c_void_p(toolbar_hwnd), TB_BUTTONCOUNT, 0, 0)
        for index in range(int(count)):
            _user32.SendMessageW(ctypes.c_void_p(toolbar_hwnd), TB_GETBUTTON, index, int(remote))
            parsed = _parse_tbbutton(_read_remote(handle, int(remote), 64))
            if not parsed:
                continue
            id_command, dw_data = parsed
            if not dw_data:
                continue
            raw_data = _read_remote(handle, dw_data, 128)
            for target_hwnd, uid, callback_msg in _parse_traydata_candidates(raw_data):
                if not callback_msg:
                    continue
                matched, exe_path, title, class_name = _is_likely_wechat_target(target_hwnd)
                if not matched:
                    continue
                key = (target_hwnd, uid, callback_msg)
                if key in seen:
                    continue
                seen.add(key)
                buttons.append(TrayButtonData(
                    toolbar_hwnd=toolbar_hwnd,
                    index=index,
                    id_command=id_command,
                    dw_data=dw_data,
                    hwnd=target_hwnd,
                    uid=uid,
                    callback_msg=callback_msg,
                    exe_path=exe_path,
                    title=title,
                    class_name=class_name,
                ))
    finally:
        _kernel32.VirtualFreeEx(ctypes.c_void_p(handle), ctypes.c_void_p(int(remote)), 0, MEM_RELEASE)
        _close_handle(handle)
    return buttons


def _enum_native_tray_toolbars() -> List[Tuple[int, int]]:
    roots = []
    shell = win32gui.FindWindow("Shell_TrayWnd", None)
    overflow = win32gui.FindWindow("NotifyIconOverflowWindow", None)
    if shell:
        roots.append(shell)
    if overflow:
        roots.append(overflow)

    toolbars = []
    seen = set()
    for root in roots:
        children = []
        def _cb(hwnd, _):
            children.append(hwnd)
            return True
        win32gui.EnumChildWindows(root, _cb, 0)
        for child in children:
            if child in seen:
                continue
            try:
                class_name = win32gui.GetClassName(child) or ""
            except Exception:
                continue
            if class_name != "ToolbarWindow32":
                continue
            _, pid = win32process.GetWindowThreadProcessId(child)
            seen.add(child)
            toolbars.append((child, pid))
    return toolbars


def _find_wechat_native_tray_buttons() -> List[TrayButtonData]:
    buttons = []
    for toolbar_hwnd, toolbar_pid in _enum_native_tray_toolbars():
        buttons.extend(_read_toolbar_buttons(toolbar_hwnd, toolbar_pid))
    return buttons


def _restore_wechat_from_native_tray(wait_after_event: float = 0.8) -> bool:
    try:
        buttons = _find_wechat_native_tray_buttons()
    except Exception:
        return False

    if not buttons:
        return False

    any_posted = False
    for button in buttons:
        for (event,) in TRAY_RESTORE_EVENTS:
            try:
                win32gui.PostMessage(button.hwnd, button.callback_msg, button.uid, event)
                any_posted = True
                time.sleep(wait_after_event)
                # 检查微信主窗口是否已恢复可见
                if _is_wechat_main_window_visible():
                    return True
            except Exception:
                pass
    return any_posted


def _is_wechat_main_window_visible() -> bool:
    result = [False]

    def _cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe_name = os.path.basename(_get_process_image_name(pid)).lower()
            class_name = win32gui.GetClassName(hwnd) or ""
        except Exception:
            return True
        if exe_name in WECHAT_EXE_NAMES and win32gui.IsWindowVisible(hwnd):
            if "TrayIconMessageWindow" not in class_name:
                result[0] = True
                return False
        return True

    win32gui.EnumWindows(_cb, None)
    return result[0]


# ============================================================================
# Windows 后端总入口
# ============================================================================

class WindowsBackend(PlatformBackend):
    """Windows 平台后端。"""

    def __init__(self):
        self._window_manager = WindowsWindowManager()
        self._automation = WindowsAutomation()
        self._input = WindowsInput()
        self._clipboard = WindowsClipboard()
        self._process = WindowsProcess()
        self._accessibility = WindowsSystemAccessibility()

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
        return "windows"
