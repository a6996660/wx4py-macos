# -*- coding: utf-8 -*-
"""微信群聊监听与自动回复。

该模块按平台使用不同监听策略：
1. Windows 端可以使用独立聊天窗口和 ``chat_message_list`` 轮询。
2. macOS 端只监听左侧会话列表的 ``[有人@我]`` 预览，避免主窗口单聊天区在多群间串读。
3. 自动回复统一进入串行发送队列，发送前再切换目标群。
4. 自动回复时记录本库发送的消息，监听回流时只忽略一次。

注意：
    微信 4.x 的 Qt UIA 对消息方向/发送者暴露不足，无法稳定识别用户手动
    发送的“自己消息”。因此这里默认只忽略“本库发送并记录过”的消息。
"""

from __future__ import annotations

import os
import queue
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..chat import ChatWindow
from ...platform import platform
from ...utils.logger import get_logger, log_error_audit

logger = get_logger(__name__)

WECHAT_EXE_NAMES = {"wechat.exe", "weixin.exe"}
MESSAGE_CLASSES = {
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleItemView",
}
TIME_CLASS = "mmui::ChatItemView"
MACOS_MESSAGE_AUTOMATION_ID = "chat_bubble_item_view"
MACOS_SESSION_ITEM_PREFIX = "session_item_"
DEFAULT_REPLY_DELAY_RANGE = (3.0, 9.0)

_CURRENT_ACCOUNT_NICKNAME: Optional[str] = None
_CURRENT_ACCOUNT_NICKNAME_LOCK = threading.Lock()


@dataclass(frozen=True)
class MessageEvent:
    """监听到的新消息。"""

    group: str
    content: str
    timestamp: float
    group_nickname: Optional[str] = None
    sender_nickname: Optional[str] = None
    is_at_me: bool = False
    raw: object = None


@dataclass(frozen=True)
class _VisibleItem:
    kind: str
    name: str
    class_name: str
    runtime_id: Tuple[int, ...]
    control: object = None

    @property
    def key(self) -> Tuple[Tuple[int, ...], str, str]:
        return self.runtime_id, self.class_name, self.name


@dataclass
class _ListenSession:
    group: str
    hwnd: int
    root: object
    msg_list: object
    seen: Set[Tuple[Tuple[int, ...], str, str]]
    seen_texts: Set[str] = field(default_factory=set)
    at_hint_pending: bool = False
    at_hint_sender: Optional[str] = None
    new_count: int = 0
    scan_count: int = 0
    fail_count: int = 0
    last_message_at: float = field(default_factory=time.time)
    next_scan_at: float = field(default_factory=time.time)
    interval: float = 0.3
    suppress_next_scan: bool = True


@dataclass
class _OutgoingRecord:
    group: str
    content: str
    expires_at: float
    remaining_hits: int


@dataclass(frozen=True)
class _ReplyTask:
    group: str
    content: str


class OutgoingMessageRegistry:
    """记录本库发送的消息，用于监听回流时忽略一次。"""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = ttl_seconds
        self._records: Deque[_OutgoingRecord] = deque()

    def record(self, group: str, content: str, max_hits: int = 8) -> None:
        content = _normalize_message_text(content)
        if not content:
            return
        record = _OutgoingRecord(
            group=group,
            content=content,
            expires_at=time.time() + self.ttl_seconds,
            remaining_hits=max_hits,
        )
        self._records.append(record)

    def should_ignore(self, group: str, content: str) -> bool:
        now = time.time()
        content = _normalize_message_text(content)
        while self._records and self._records[0].expires_at < now:
            self._records.popleft()

        for index, record in enumerate(self._records):
            if record.group != group:
                continue
            if _is_same_outgoing_message(record.content, content):
                record.remaining_hits -= 1
                if record.remaining_hits <= 0:
                    del self._records[index]
                return True
        return False


def _normalize_message_text(content: str) -> str:
    """归一化消息文本，提升本库发送回流识别的稳定性。"""
    text = str(content or "")
    text = text.replace("\u2005", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_same_outgoing_message(expected: str, actual: str) -> bool:
    """判断回流消息是否可视为本库刚发送的同一条消息。"""
    if not expected or not actual:
        return False
    if expected == actual:
        return True

    # 微信 UIA 在部分版本上会对长文本、多行文本做轻微归一化或裁剪，
    # 这里允许“包含关系”命中，避免机器人自己的回复再次触发监听链路。
    shorter, longer = sorted((expected, actual), key=len)
    if len(shorter) < 12:
        return False
    return shorter in longer


def _safe_text(control, attr: str) -> str:
    try:
        return str(getattr(control, attr, "") or "")
    except Exception:
        return ""


def _safe_children(control) -> list:
    try:
        return list(control.GetChildren())
    except Exception:
        return []


def _safe_runtime_id(control) -> Tuple[int, ...]:
    try:
        return tuple(control.GetRuntimeId() or ())
    except Exception:
        return ()


def _get_current_account_nickname(root=None) -> Optional[str]:
    """读取并缓存当前登录账号昵称。

    macOS 微信没有稳定暴露“我”的昵称属性，这里在监听启动时点击一次
    左侧头像，从资料卡读取 display_name_text。读取结果作为进程级静态缓存。
    """
    global _CURRENT_ACCOUNT_NICKNAME
    if _CURRENT_ACCOUNT_NICKNAME:
        return _CURRENT_ACCOUNT_NICKNAME

    with _CURRENT_ACCOUNT_NICKNAME_LOCK:
        if _CURRENT_ACCOUNT_NICKNAME:
            return _CURRENT_ACCOUNT_NICKNAME
        if platform.platform_name != "darwin":
            return None

        nickname = _read_current_account_nickname_macos(root)
        if nickname:
            _CURRENT_ACCOUNT_NICKNAME = nickname
            logger.info(f"当前登录微信昵称: {nickname}")
        return _CURRENT_ACCOUNT_NICKNAME


def _read_current_account_nickname_macos(root=None) -> Optional[str]:
    try:
        from ...platform import _macos
    except Exception:
        return None

    try:
        root = root or platform.automation.control_from_handle(platform.window_manager.find_wechat_window())
        rect = root.BoundingRectangle
        if not rect:
            return None

        # 左侧栏头像位于主窗口左上角头像区。点击后弹出当前账号资料卡。
        platform.input.mouse_click(rect.left + 30, rect.top + 65)
        time.sleep(0.6)

        app_ref = _macos._get_wechat_app_ref()
        windows, _ = _macos._ax_copy_attr(app_ref, "AXWindows")
        for window in list(windows or []):
            title = _macos._ax_get_title(window)
            pos = _macos._ax_get_position(window)
            size = _macos._ax_get_size(window)
            if title or not pos or not size:
                continue
            if not (200 < size[0] < 500 and 200 < size[1] < 500):
                continue

            nickname = _extract_profile_nickname_from_ax(window, _macos)
            if nickname:
                return nickname
    except Exception as exc:
        logger.debug(f"读取当前微信昵称失败: {exc}")
    finally:
        try:
            if root:
                root.SendKeys("{Esc}")
        except Exception:
            pass
        try:
            platform.input.key_down(platform.input.VK_ESCAPE)
            time.sleep(0.05)
            platform.input.key_up(platform.input.VK_ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass
    return None


def _extract_profile_nickname_from_ax(element, macos_module) -> Optional[str]:
    stack = [element]
    fallback: Optional[str] = None
    ignored = {"微信号：", "朋友圈", "发消息"}
    while stack:
        node = stack.pop()
        try:
            role = macos_module._ax_get_role(node)
            title = (
                macos_module._ax_get_title(node)
                or macos_module._ax_get_value(node)
                or macos_module._ax_get_description(node)
                or ""
            ).strip()
            auto_id = str(macos_module._ax_get_attr(node, "AXIdentifier") or "")
            if auto_id == "display_name_text" and title:
                return title
            if role == "AXStaticText" and title and title not in ignored:
                if not title.startswith("微信号") and not fallback:
                    fallback = title
            children = macos_module._ax_get_children(node)
            for child in reversed(children):
                stack.append(child)
        except Exception:
            continue
    return fallback


def _get_process_image_name(pid: int) -> str:
    """通过 pid 获取进程路径。"""
    return platform.process.get_process_name(pid)


def _find_wechat_windows() -> List[Tuple[int, str, str]]:
    windows: List[Tuple[int, str, str]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            pid = platform.process.get_window_pid(hwnd)
            exe_name = os.path.basename(_get_process_image_name(pid)).lower()
            title = platform.window_manager.get_title(hwnd) or ""
            class_name = platform.window_manager.get_class(hwnd) or ""
        except Exception:
            return True

        if exe_name in WECHAT_EXE_NAMES and platform.window_manager.is_visible(hwnd):
            windows.append((hwnd, title, class_name))
        return True

    platform.window_manager.enum_windows(callback, 0)
    return windows


def _find_window_by_title(title_keyword: str, exclude_hwnd: Optional[int] = None) -> Optional[int]:
    for hwnd, title, _class_name in _find_wechat_windows():
        if hwnd == exclude_hwnd:
            continue
        if title_keyword in title:
            return hwnd
    return None


def _find_message_list(root):
    """查找聊天消息列表。"""
    try:
        msg_list = root.ListControl(AutomationId="chat_message_list")
        if msg_list.Exists(maxSearchSeconds=1):
            return msg_list
    except Exception:
        pass

    candidates = []
    try:
        for control, depth in platform.automation.walk_control(root, include_top=True, max_depth=8):
            if _safe_text(control, "ControlTypeName") != "ListControl":
                continue
            score = 0
            for child in _safe_children(control)[-12:]:
                cls = _safe_text(child, "ClassName")
                auto_id = _safe_text(child, "AutomationId")
                if cls in MESSAGE_CLASSES or auto_id == MACOS_MESSAGE_AUTOMATION_ID:
                    score += 10
                elif cls == TIME_CLASS:
                    score += 2
            if score:
                candidates.append((score, depth, control))
    except Exception:
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def _read_visible_items(msg_list) -> List[_VisibleItem]:
    items: List[_VisibleItem] = []
    time_re = re.compile(r'^(今天|昨天|星期[一二三四五六日]|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}|\d{4}年|\d{1,2}:\d{2})')
    for child in _safe_children(msg_list):
        cls = _safe_text(child, "ClassName")
        name = _safe_text(child, "Name").strip()
        auto_id = _safe_text(child, "AutomationId")
        if not name:
            continue
        if cls == TIME_CLASS or (not auto_id and time_re.match(name)):
            kind = "time/system"
        elif cls in MESSAGE_CLASSES or auto_id == MACOS_MESSAGE_AUTOMATION_ID:
            kind = "message"
        else:
            continue
        items.append(
            _VisibleItem(
                kind=kind,
                name=name,
                class_name=cls,
                runtime_id=_safe_runtime_id(child),
                control=child,
            )
        )
    return items


def _find_session_list(root):
    """查找微信左侧会话列表。"""
    try:
        session_list = root.ListControl(AutomationId="session_list")
        if session_list.Exists(maxSearchSeconds=1):
            return session_list
    except Exception:
        pass

    try:
        for control, _depth in platform.automation.walk_control(root, include_top=True, max_depth=6):
            if _safe_text(control, "ControlTypeName") != "ListControl":
                continue
            if _safe_text(control, "AutomationId") == "session_list" or _safe_text(control, "Name") == "会话":
                return control
    except Exception:
        return None
    return None


def _find_session_item(root, group_name: str):
    session_list = _find_session_list(root)
    if not session_list:
        return None

    direct_candidates = []
    for control in _safe_children(session_list):
        name = _safe_text(control, "Name")
        auto_id = _safe_text(control, "AutomationId")
        score = 0
        if group_name in name:
            score += 100
        if auto_id == f"{MACOS_SESSION_ITEM_PREFIX}{group_name}":
            score += 120
        if auto_id.startswith(MACOS_SESSION_ITEM_PREFIX):
            score += 30
        if score:
            direct_candidates.append((score, control))
    if direct_candidates:
        direct_candidates.sort(key=lambda item: item[0], reverse=True)
        return direct_candidates[0][1]

    candidates = []
    try:
        for control, depth in platform.automation.walk_control(session_list, include_top=False, max_depth=3):
            control_type = _safe_text(control, "ControlTypeName")
            auto_id = _safe_text(control, "AutomationId")
            if control_type != "ListItemControl" and not auto_id.startswith(MACOS_SESSION_ITEM_PREFIX):
                continue
            name = _safe_text(control, "Name")
            cls = _safe_text(control, "ClassName")
            score = 0
            if group_name in name:
                score += 100
            if auto_id == f"{MACOS_SESSION_ITEM_PREFIX}{group_name}":
                score += 120
            if "Session" in cls or "Conversation" in cls or "Cell" in cls:
                score += 30
            if auto_id.startswith(MACOS_SESSION_ITEM_PREFIX):
                score += 30
            try:
                if control.IsSelected:
                    score += 80
            except Exception:
                pass
            if score:
                candidates.append((score, depth, control))
    except Exception:
        return None

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def _session_item_has_at_me(control) -> bool:
    name = _safe_text(control, "Name")
    return "[有人@我]" in name or "有人@我" in name


def _session_item_matches_group(control, group_name: str) -> bool:
    auto_id = _safe_text(control, "AutomationId")
    if auto_id == f"{MACOS_SESSION_ITEM_PREFIX}{group_name}":
        return True
    name = _safe_text(control, "Name")
    first_line = next((line.strip() for line in name.splitlines() if line.strip()), "")
    return first_line == group_name


def _extract_sender_from_session_item(control, group_name: str) -> Optional[str]:
    """从左侧会话预览中解析发送者，如: 群名\n[有人@我] 张三: @我..."""
    name = _safe_text(control, "Name")
    if not name:
        return None
    lines = [line.strip() for line in name.splitlines() if line.strip()]
    for line in lines:
        if line == group_name:
            continue
        if line in {"消息免打扰"} or re.match(r"^\d{1,2}:\d{2}$", line):
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if "@" not in line:
            continue
        if ":" in line:
            sender = line.split(":", 1)[0].strip()
        elif "：" in line:
            sender = line.split("：", 1)[0].strip()
        else:
            continue
        sender = re.sub(r"^\[[^\]]+\]\s*", "", sender).strip()
        if not sender or sender in {"我", "你"}:
            continue
        if sender.startswith("@") or "@" in sender or "\n" in sender:
            continue
        if len(sender) > 24:
            continue
        if re.match(r"^\d{1,2}:\d{2}$", sender):
            continue
        if sender:
            return sender
    return None


def _extract_message_from_session_item(control, group_name: str) -> Optional[str]:
    """从左侧会话预览中解析最新 @ 消息正文。"""
    name = _safe_text(control, "Name")
    if not name:
        return None
    lines = [line.strip() for line in name.splitlines() if line.strip()]
    for line in lines:
        if line == group_name:
            continue
        if line in {"消息免打扰"} or re.match(r"^\d{1,2}:\d{2}$", line):
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if "@" not in line:
            continue
        if ":" in line:
            _sender, message = line.split(":", 1)
        elif "：" in line:
            _sender, message = line.split("：", 1)
        else:
            message = line
        message = message.strip()
        if message:
            return message
    return None


def _click_control(control) -> bool:
    try:
        control.Click()
        return True
    except Exception:
        pass

    try:
        control.Click(simulateMove=False)
        return True
    except Exception:
        pass

    try:
        rect = control.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        platform.input.mouse_click(x, y)
        return True
    except Exception:
        return False


def _click_session_item(control) -> bool:
    """macOS 会话列表使用坐标单击，避免 AX Invoke 被微信解释为打开独立窗口。"""
    if platform.platform_name != "darwin":
        return _click_control(control)
    try:
        rect = control.BoundingRectangle
        x = rect.left + min(90, max(30, (rect.right - rect.left) // 3))
        y = (rect.top + rect.bottom) // 2
        platform.input.mouse_click(x, y)
        return True
    except Exception:
        return _click_control(control)


def _dismiss_send_failure_dialog(root=None) -> bool:
    """关闭微信“发送失败”一类阻塞弹窗。"""
    roots = []
    if root is not None:
        roots.append(root)
    try:
        current_root = platform.automation.control_from_handle(platform.window_manager.find_wechat_window())
        if current_root is not root:
            roots.append(current_root)
    except Exception:
        pass

    for search_root in roots:
        try:
            has_failure_text = False
            buttons = []
            for control, _depth in platform.automation.walk_control(search_root, include_top=True, max_depth=8):
                name = _safe_text(control, "Name")
                if "发送失败" in name:
                    has_failure_text = True
                if name in {"我知道了", "确定", "好"}:
                    buttons.append(control)
            if has_failure_text and buttons:
                for button in buttons:
                    if _click_control(button):
                        time.sleep(0.3)
                        return True
        except Exception:
            continue
    return False


def _double_click_control(control) -> bool:
    try:
        control.DoubleClick(simulateMove=False)
        return True
    except Exception:
        pass

    try:
        rect = control.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        platform.input.mouse_dblclick(x, y)
        return True
    except Exception:
        return False
class WeChatGroupListener:
    """微信群聊监听器。"""

    def __init__(
        self,
        client,
        groups: Iterable[str],
        on_message: Callable[[MessageEvent], Optional[str]],
        *,
        auto_reply: bool = True,
        ignore_client_sent: bool = True,
        reply_on_at: bool = False,
        group_nicknames: Optional[Dict[str, str]] = None,
        outgoing_ttl: float = 60.0,
        tick: float = 0.1,
        batch_size: int = 8,
        tail_size: int = 8,
        reply_delay_range: Tuple[float, float] = DEFAULT_REPLY_DELAY_RANGE,
    ):
        self.client = client
        self.groups = list(dict.fromkeys(groups))
        self.on_message = on_message
        self.auto_reply = auto_reply
        self.ignore_client_sent = ignore_client_sent
        self.reply_on_at = reply_on_at
        self.group_nicknames = dict(group_nicknames or {})
        self.tick = tick
        self.batch_size = batch_size
        self.tail_size = tail_size
        self.reply_delay_range = self._normalize_reply_delay_range(reply_delay_range)
        shared_registry = getattr(self.client, "outgoing_registry", None)
        self.outgoing_registry = shared_registry or OutgoingMessageRegistry(outgoing_ttl)
        self.sessions: Dict[str, _ListenSession] = {}
        self._reply_queue: "queue.Queue[_ReplyTask]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._current_send_group: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, block: bool = False) -> "WeChatGroupListener":
        """启动监听。"""
        self._open_sessions()
        self._stop_event.clear()
        self._start_sender()
        if block:
            try:
                self._run_loop()
            finally:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        """停止监听。"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._sender_thread and self._sender_thread.is_alive():
            self._sender_thread.join(timeout=5)

    def run_forever(self) -> None:
        """阻塞当前线程持续监听，直到 Ctrl+C。"""
        try:
            if not self.is_running:
                self.start(block=True)
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def _open_sessions(self) -> None:
        for group in self.groups:
            if group in self.sessions:
                continue

            chat_already_open = False
            if (
                self.reply_on_at
                and platform.platform_name != "darwin"
                and not self.group_nicknames.get(group)
            ):
                chat_already_open = self._read_group_nickname(group)
            elif self.reply_on_at and platform.platform_name == "darwin" and not self.group_nicknames.get(group):
                nickname = _get_current_account_nickname(self.client.window.uia.root)
                if nickname:
                    self.group_nicknames[group] = nickname

            if platform.platform_name == "darwin":
                root = platform.automation.control_from_handle(self.client.window.hwnd)
                session_item = _find_session_item(root, group)
                if not session_item:
                    raise RuntimeError(f"左侧会话列表未找到群聊，macOS 监听不会回退搜索: {group}")
                has_startup_at_hint = _session_item_has_at_me(session_item)
                self.sessions[group] = _ListenSession(
                    group=group,
                    hwnd=self.client.window.hwnd,
                    root=root,
                    msg_list=None,
                    seen=set(),
                    seen_texts=set(),
                    at_hint_pending=has_startup_at_hint,
                    at_hint_sender=_extract_sender_from_session_item(session_item, group),
                    suppress_next_scan=has_startup_at_hint,
                )
                continue

            hwnd = self._ensure_subwindow(group, chat_already_open=chat_already_open)
            root, msg_list = self._wait_for_message_list(hwnd, group)
            if not msg_list:
                raise RuntimeError(f"未找到群聊消息列表: {group}")
            baseline = _read_visible_items(msg_list)
            session_item = _find_session_item(self.client.window.uia.root, group)
            self.sessions[group] = _ListenSession(
                group=group,
                hwnd=hwnd,
                root=root,
                msg_list=msg_list,
                seen={item.key for item in baseline},
                seen_texts={
                    _normalize_message_text(item.name)
                    for item in baseline
                    if item.kind == "message" and _normalize_message_text(item.name)
                },
                at_hint_pending=_session_item_has_at_me(session_item) if session_item else False,
                at_hint_sender=_extract_sender_from_session_item(session_item, group) if session_item else None,
            )

    def _wait_for_message_list(self, hwnd: int, group: str, timeout: float = 15.0):
        """等待目标群聊的消息列表出现。

        macOS 上点击头像读取昵称后，资料卡关闭和会话切换都有短暂异步刷新；
        因此这里需要轮询，并在必要时重新点击左侧群会话项。
        """
        deadline = time.time() + timeout
        root = None
        msg_list = None
        clicked_at = 0.0
        while time.time() < deadline:
            root = platform.automation.control_from_handle(hwnd)
            if platform.platform_name == "darwin":
                session_item = _find_session_item(root, group)
                if session_item and not clicked_at:
                    _click_session_item(session_item)
                    clicked_at = time.time()
                    time.sleep(0.2)
                    root = platform.automation.control_from_handle(hwnd)

            msg_list = _find_message_list(root)
            if msg_list:
                return root, msg_list
            time.sleep(0.3)
        if platform.platform_name == "darwin":
            try:
                session_item = _find_session_item(root, group) if root else None
                logger.warning(
                    "等待 macOS 群聊消息列表超时: group=%s, session_item=%s, session_name=%s",
                    group,
                    bool(session_item),
                    _safe_text(session_item, "Name")[:120] if session_item else "",
                )
            except Exception:
                pass
        return root, msg_list

    def _read_group_nickname(self, group: str) -> bool:
        """读取群昵称。

        ``GroupManager.get_group_nickname`` 本身会打开目标群聊并进入详情面板。
        返回 True 表示当前主窗口大概率已经停留在该群聊，可直接双击左侧会话项
        打开独立窗口，避免再次搜索同一个群。
        """
        try:
            nickname = self.client.group_manager.get_group_nickname(group)
        except Exception as exc:
            logger.warning(f"读取群昵称失败: {group}: {exc}")
            return False

        if nickname:
            self.group_nicknames[group] = nickname
        else:
            logger.warning(f"未读取到群昵称，无法精确判断是否 @ 我: {group}")
        return True

    def _ensure_subwindow(self, group: str, chat_already_open: bool = False) -> int:
        main_hwnd = self.client.window.hwnd
        if platform.platform_name == "darwin":
            root = platform.automation.control_from_handle(main_hwnd)
            item = _find_session_item(root, group)
            if item and _click_session_item(item):
                time.sleep(0.8)
                return main_hwnd
            raise RuntimeError(f"左侧会话列表未找到群聊，macOS 监听不会回退搜索: {group}")

        hwnd = _find_window_by_title(group, exclude_hwnd=main_hwnd)
        if hwnd:
            return hwnd

        item = _find_session_item(self.client.window.uia.root, group)
        if item and _click_session_item(item):
            time.sleep(0.5)
        elif item:
            logger.debug(f"左侧会话项点击失败，回退搜索打开群聊: {group}")

        if not chat_already_open:
            current_msg_list = _find_message_list(self.client.window.uia.root)
            if not current_msg_list and not self.client.chat_window.open_chat(group, target_type="group"):
                raise RuntimeError(f"打开群聊失败: {group}")
            time.sleep(0.8)

        item = _find_session_item(self.client.window.uia.root, group)
        if not item and chat_already_open:
            logger.debug(f"当前会话项未找到，重新搜索打开群聊: {group}")
            if not self.client.chat_window.open_chat(group, target_type="group"):
                raise RuntimeError(f"打开群聊失败: {group}")
            time.sleep(0.8)
            item = _find_session_item(self.client.window.uia.root, group)

        if not item or not _double_click_control(item):
            logger.warning(f"打开独立聊天窗口失败，复用当前主窗口监听: {group}")
            return main_hwnd

        deadline = time.time() + 5
        while time.time() < deadline:
            hwnd = _find_window_by_title(group, exclude_hwnd=main_hwnd)
            if hwnd:
                return hwnd
            time.sleep(0.2)
        logger.warning(f"等待独立聊天窗口超时，复用当前主窗口监听: {group}")
        return main_hwnd

    def _run_loop(self) -> None:
        logger.info(f"开始监听群聊: {', '.join(self.groups)}")
        while not self._stop_event.is_set():
            now = time.time()
            for session in self._due_sessions(now):
                self._poll_session(session)
            time.sleep(self.tick)
        logger.info("群聊监听已停止")

    def _due_sessions(self, now: float) -> List[_ListenSession]:
        sessions = [
            session for session in self.sessions.values()
            if session.next_scan_at <= now
        ]
        sessions.sort(key=lambda session: session.next_scan_at)
        return sessions[:self.batch_size]

    def _poll_session(self, session: _ListenSession) -> None:
        session.scan_count += 1
        at_hint = session.at_hint_pending
        sender_nickname = session.at_hint_sender
        try:
            session_item = _find_session_item(self.client.window.uia.root, session.group)
            if platform.platform_name == "darwin":
                if session_item and not _session_item_matches_group(session_item, session.group):
                    logger.warning(
                        "忽略疑似错配会话项: expected=%s, automation_id=%s, name=%s",
                        session.group,
                        _safe_text(session_item, "AutomationId"),
                        _safe_text(session_item, "Name")[:120],
                    )
                    session_item = None
                if not session_item or not _session_item_has_at_me(session_item):
                    session.at_hint_pending = False
                    session.at_hint_sender = None
                    self._update_next_scan(session, 0)
                    return
                sender_nickname = _extract_sender_from_session_item(session_item, session.group)
                content = _extract_message_from_session_item(session_item, session.group)
                if not content:
                    self._update_next_scan(session, 0)
                    return
                normalized_text = _normalize_message_text(content)
                if normalized_text in session.seen_texts:
                    self._update_next_scan(session, 0)
                    return
                if normalized_text:
                    session.seen_texts.add(normalized_text)
                self._handle_text_message(
                    session,
                    content,
                    sender_nickname=sender_nickname,
                    at_hint=True,
                )
                session.at_hint_pending = False
                session.at_hint_sender = None
                self._update_next_scan(session, 1)
                return

            if session_item:
                sender_nickname = _extract_sender_from_session_item(session_item, session.group) or sender_nickname
            has_at_hint = bool(session_item and _session_item_has_at_me(session_item))
            if platform.platform_name == "darwin" and not at_hint and not has_at_hint:
                self._update_next_scan(session, 0)
                return
            if has_at_hint:
                at_hint = True
                sender_nickname = _extract_sender_from_session_item(session_item, session.group)
                _click_session_item(session_item)
                time.sleep(0.6)
                session.root = platform.automation.control_from_handle(self.client.window.hwnd)
                refreshed_msg_list = _find_message_list(session.root)
                if refreshed_msg_list:
                    session.msg_list = refreshed_msg_list
            if not session.msg_list:
                self._update_next_scan(session, 0)
                return
            items = _read_visible_items(session.msg_list)
            if self.tail_size > 0:
                items = items[-self.tail_size:]
        except Exception as exc:
            session.fail_count += 1
            logger.debug(f"读取群聊消息失败: {session.group}: {exc}")
            return

        pending_messages: List[_VisibleItem] = []
        for item in items:
            if item.key in session.seen:
                continue
            session.seen.add(item.key)
            if item.kind != "message":
                continue
            normalized_text = _normalize_message_text(item.name)
            if normalized_text in session.seen_texts:
                continue
            if normalized_text:
                session.seen_texts.add(normalized_text)
            if self.ignore_client_sent and self.outgoing_registry.should_ignore(session.group, item.name):
                continue
            pending_messages.append(item)

        if session.suppress_next_scan:
            session.suppress_next_scan = False
            if pending_messages:
                logger.info(
                    "macOS 群聊启动基线已刷新，忽略历史消息: %s, count=%s",
                    session.group,
                    len(pending_messages),
                )
            if at_hint:
                session.at_hint_pending = False
                session.at_hint_sender = None
            self._update_next_scan(session, 0)
            return

        added = len(pending_messages)
        for index, item in enumerate(pending_messages):
            session.new_count += 1
            event_sender = (
                sender_nickname
                if index == len(pending_messages) - 1 and (at_hint or self._is_at_me(session.group, item.name))
                else None
            )
            self._handle_message(
                session,
                item,
                at_hint=at_hint,
                sender_nickname=event_sender,
            )

        if at_hint and pending_messages:
            session.at_hint_pending = False
            session.at_hint_sender = None

        self._update_next_scan(session, added)

    def _handle_text_message(
        self,
        session: _ListenSession,
        content: str,
        at_hint: bool = False,
        sender_nickname: Optional[str] = None,
    ) -> None:
        event = MessageEvent(
            group=session.group,
            content=content,
            timestamp=time.time(),
            group_nickname=self.group_nicknames.get(session.group),
            sender_nickname=sender_nickname,
            is_at_me=self._is_at_me(session.group, content) or (
                at_hint and not self.group_nicknames.get(session.group)
            ),
            raw=None,
        )
        try:
            reply = self.on_message(event)
        except Exception as exc:
            logger.exception(f"消息回调执行失败: {session.group}: {exc}")
            return

        if self.auto_reply and reply and self._should_send_reply(event):
            self.enqueue_reply(session.group, str(reply))

    def _handle_message(
        self,
        session: _ListenSession,
        item: _VisibleItem,
        at_hint: bool = False,
        sender_nickname: Optional[str] = None,
    ) -> None:
        event = MessageEvent(
            group=session.group,
            content=item.name,
            timestamp=time.time(),
            group_nickname=self.group_nicknames.get(session.group),
            sender_nickname=sender_nickname,
            is_at_me=self._is_at_me(session.group, item.name) or (
                at_hint and not self.group_nicknames.get(session.group)
            ),
            raw=item.control,
        )
        try:
            reply = self.on_message(event)
        except Exception as exc:
            logger.exception(f"消息回调执行失败: {session.group}: {exc}")
            return

        if self.auto_reply and reply and self._should_send_reply(event):
            self.enqueue_reply(session.group, str(reply))

    def _is_at_me(self, group: str, content: str) -> bool:
        nickname = self.group_nicknames.get(group)
        if not nickname:
            return False
        return f"@{nickname}" in content or f"@{nickname}\u2005" in content

    def _should_send_reply(self, event: MessageEvent) -> bool:
        if not self.reply_on_at:
            return True
        return event.is_at_me

    def _update_next_scan(self, session: _ListenSession, added: int) -> None:
        now = time.time()
        if added:
            session.last_message_at = now
            session.interval = 0.3
        else:
            idle_for = now - session.last_message_at
            if idle_for >= 120:
                session.interval = 3.0
            elif idle_for >= 30:
                session.interval = 1.0
            else:
                session.interval = 0.3
        session.next_scan_at = now + session.interval

    def reply(self, group: str, content: str) -> bool:
        """立即使用对应独立窗口回复群聊。

        注意：该方法会直接操作窗口、剪贴板和焦点。自动回复默认不直接调用它，
        而是进入发送队列，由单个 sender 线程串行发送，避免多个群同时回复时
        抢占窗口。
        """
        session = self.sessions.get(group)
        if not session:
            raise ValueError(f"未监听群聊: {group}")

        self._sleep_before_reply(group, content)

        if self.ignore_client_sent:
            # 先登记，再发送，避免微信回流速度快于登记速度导致漏判。
            self.outgoing_registry.record(group, content)

        sent = self._send_in_subwindow(session, content)
        return sent

    @staticmethod
    def _normalize_reply_delay_range(delay_range) -> Tuple[float, float]:
        try:
            min_delay, max_delay = delay_range
            min_delay = max(0.0, float(min_delay))
            max_delay = max(0.0, float(max_delay))
        except Exception:
            return DEFAULT_REPLY_DELAY_RANGE
        if max_delay < min_delay:
            min_delay, max_delay = max_delay, min_delay
        return min_delay, max_delay

    def _sleep_before_reply(self, group: str, content: str) -> float:
        min_delay, max_delay = self.reply_delay_range
        if max_delay <= 0:
            return 0.0
        delay = random.uniform(min_delay, max_delay)
        logger.info(
            f"群聊回复随机等待 {delay:.2f} 秒: {group} -> {(content or '')[:30]}"
        )
        time.sleep(delay)
        return delay

    def enqueue_reply(self, group: str, content: str) -> None:
        """将回复加入串行发送队列。"""
        content = (content or "").strip()
        if not content:
            return
        self._reply_queue.put(_ReplyTask(group=group, content=content))

    def _start_sender(self) -> None:
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._sender_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._sender_thread.start()

    def _send_loop(self) -> None:
        """串行发送回复，避免多个窗口同时争抢焦点/剪贴板。"""
        while not self._stop_event.is_set() or not self._reply_queue.empty():
            try:
                task = self._reply_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self.reply(task.group, task.content)
            except Exception as exc:
                logger.exception(f"发送队列回复失败: {task.group}: {exc}")
                log_error_audit(
                    "reply_queue_failed",
                    {"group": task.group, "reply": task.content},
                    exc,
                )
            finally:
                self._reply_queue.task_done()

    def _send_in_subwindow(self, session: _ListenSession, content: str) -> bool:
        if platform.platform_name == "darwin":
            if self._current_send_group == session.group:
                session.root = platform.automation.control_from_handle(self.client.window.hwnd)
                if not self._find_chat_input(session.root):
                    self._current_send_group = None
            if self._current_send_group != session.group:
                if not self._activate_group_for_send(session):
                    return False

        root = session.root
        edit = self._find_chat_input(root)
        if not edit:
            logger.error(f"未找到聊天输入框: {session.group}")
            log_error_audit(
                "chat_input_missing",
                {"group": session.group, "reply": content},
            )
            return False

        sent = ChatWindow.send_text_via_input(
            edit,
            content,
            clipboard_error="写入回复到剪贴板失败",
            send_error=f"发送群聊回复失败: {session.group}",
            logger_override=logger,
        )
        if platform.platform_name == "darwin" and _dismiss_send_failure_dialog(session.root):
            logger.error(f"微信提示发送失败: {session.group}")
            self._current_send_group = None
            log_error_audit(
                "wechat_send_failure_dialog",
                {"group": session.group, "reply": content},
            )
            return False
        if sent:
            self._current_send_group = session.group
        return sent

    def _activate_group_for_send(self, session: _ListenSession) -> bool:
        handles = []
        for hwnd in (self.client.window.hwnd, platform.window_manager.find_wechat_window()):
            if hwnd and hwnd not in handles:
                handles.append(hwnd)

        last_reason = "no_window"
        for hwnd in handles:
            try:
                root = platform.automation.control_from_handle(hwnd)
            except Exception as exc:
                last_reason = f"root_failed:{exc}"
                continue

            _dismiss_send_failure_dialog(root)
            clicked_at = 0.0
            for attempt in range(2):
                try:
                    root = platform.automation.control_from_handle(hwnd)
                    item = _find_session_item(root, session.group)
                    if not item:
                        last_reason = "session_item_missing"
                        break
                    if clicked_at:
                        time.sleep(max(0.0, 1.2 - (time.time() - clicked_at)))
                    if not _click_session_item(item):
                        last_reason = "session_item_click_failed"
                        continue
                    clicked_at = time.time()
                    wait_deadline = time.time() + 3.5
                    while time.time() < wait_deadline:
                        time.sleep(0.25)
                        session.root = platform.automation.control_from_handle(hwnd)
                        msg_list = _find_message_list(session.root)
                        edit = self._find_chat_input(session.root)
                        if msg_list:
                            session.msg_list = msg_list
                        if msg_list and edit:
                            self._current_send_group = session.group
                            return True
                        last_reason = f"msg_list={bool(msg_list)}, edit={bool(edit)}"
                except Exception as exc:
                    last_reason = f"attempt_failed:{exc}"
                    continue
        logger.error(f"发送前切换目标群失败: {session.group}, reason={last_reason}")
        log_error_audit(
            "activate_group_failed",
            {"group": session.group, "reason": last_reason},
        )
        return False

    @staticmethod
    def _find_chat_input(root):
        possible_ids = ["chat_input_field", "input_field", "msg_input", "edit_input"]
        for auto_id in possible_ids:
            try:
                edit = root.EditControl(AutomationId=auto_id)
                if edit.Exists(maxSearchSeconds=0.3):
                    return edit
            except Exception:
                continue

        candidates = []
        try:
            root_rect = root.BoundingRectangle
            for control, _depth in platform.automation.walk_control(root, include_top=True, max_depth=8):
                if _safe_text(control, "ControlTypeName") != "EditControl":
                    continue
                rect = control.BoundingRectangle
                if rect.top < root_rect.top + root_rect.height * 0.55:
                    continue
                width = rect.right - rect.left
                if width <= 100:
                    continue
                candidates.append((width, control))
        except Exception:
            return None

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
