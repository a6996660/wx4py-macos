# -*- coding: utf-8 -*-
"""macOS 微信群聊监听与自动回复。

监听策略固定为读取左侧会话列表的 ``[有人@我]`` 预览，不搜索群名，
不把当前聊天区作为多群消息来源。自动回复统一进入串行发送队列，
发送前再切换目标群，并在发送后确认回复出现在消息列表中。

注意：
    微信 4.x 的 Qt UIA 对消息方向/发送者暴露不足，无法稳定识别用户手动
    发送的“自己消息”。因此这里默认只忽略“本库发送并记录过”的消息。
"""

from __future__ import annotations

import queue
import random
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..chat import ChatWindow
from ...platform import platform
from ...utils.logger import get_logger, log_error_audit, log_send_audit

logger = get_logger(__name__)

MESSAGE_CLASSES = {
    "mmui::ChatTextItemView",
    "mmui::ChatBubbleItemView",
}
TIME_CLASS = "mmui::ChatItemView"
MACOS_MESSAGE_AUTOMATION_ID = "chat_bubble_item_view"
MACOS_SESSION_ITEM_PREFIX = "session_item_"
DEFAULT_REPLY_DELAY_RANGE = (3.0, 9.0)
SEND_ATTEMPT_LIMIT = 3
GROUP_SWITCH_ATTEMPT_LIMIT = 3
GROUP_SWITCH_WAIT_SECONDS = 0.8
SEND_VERIFY_TIMEOUT = 1.6
RECOVERY_PAUSE_SECONDS = 0.25
GROUP_SWITCH_FALLBACK_ACCEPT_SECONDS = 0.5
DETACHED_WINDOW_CHECK_INTERVAL = 0.25
QUICK_EXISTS_TIMEOUT = 0.08
_SESSION_ITEM_CACHE_TTL = 60.0
PENDING_GROUP_RETRY_SECONDS = 30.0
SEND_INPUT_SETTLE_SECONDS = 0.18
SEND_PASTE_SETTLE_SECONDS = 0.18
SEND_FOCUS_SETTLE_SECONDS = 0.12
SEND_FAST_VERIFY_SECONDS = 0.45

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
    quoted_content: Optional[str] = None
    quoted_sender: Optional[str] = None
    attachment_name: Optional[str] = None
    attachment_type: Optional[str] = None


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


class _UNSET:
    pass


@dataclass
class _ListenSession:
    group: str
    hwnd: int
    root: object
    msg_list: object
    seen: Set[Tuple[Tuple[int, ...], str, str]]
    seen_texts: Dict[str, float] = field(default_factory=dict)
    at_hint_pending: bool = False
    at_hint_sender: Optional[str] = None
    new_count: int = 0
    scan_count: int = 0
    fail_count: int = 0
    last_message_at: float = field(default_factory=time.time)
    next_scan_at: float = field(default_factory=time.time)
    interval: float = 0.3
    suppress_next_scan: bool = True
    cached_session_item: Optional[object] = None
    cached_session_item_at: float = 0.0


TEXT_DEDUPE_TTL = 10.0


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
    send_id: str
    enqueued_at: float


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

    # 微信 UIA 在部分版本上会对长文本、多行文本做轻微裁剪。只能接受
    # “实际气泡是期望回复的前缀”这种情况，不能接受任意包含关系；
    # 否则长回复可能被输入区残留、历史气泡或相邻文本误判为已发送。
    if len(actual) < 24:
        return False
    return expected.startswith(actual) and len(actual) >= min(80, len(expected))


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
        windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
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


def _find_message_list(root):
    """查找聊天消息列表。"""
    try:
        msg_list = root.ListControl(AutomationId="chat_message_list")
        if msg_list.Exists(maxSearchSeconds=QUICK_EXISTS_TIMEOUT):
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
        if session_list.Exists(maxSearchSeconds=QUICK_EXISTS_TIMEOUT):
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
        logger.debug("会话查找: group=%s session_list=False", group_name)
        return None

    direct_candidates = []
    visible_items = _safe_children(session_list)
    visible_names = []
    for control in visible_items:
        name = _safe_text(control, "Name")
        auto_id = _safe_text(control, "AutomationId")
        if name or auto_id:
            visible_names.append(f"{auto_id}:{name[:40]}")
        score = 0
        if group_name in name:
            score += 100
        if auto_id == f"{MACOS_SESSION_ITEM_PREFIX}{group_name}":
            score += 120
        if score:
            direct_candidates.append((score, control))
    if direct_candidates:
        direct_candidates.sort(key=lambda item: item[0], reverse=True)
        selected = direct_candidates[0][1]
        logger.debug(
            "会话查找: group=%s source=direct candidates=%s selected_auto_id=%r selected_name=%r",
            group_name,
            len(direct_candidates),
            _safe_text(selected, "AutomationId"),
            _safe_text(selected, "Name")[:120],
        )
        return selected

    logger.debug(
        "会话查找: group=%s source=visible candidates=0 visible_count=%s visible=%s",
        group_name,
        len(visible_items),
        visible_names[:12],
    )
    return None


def _normalize_chat_title(text: str) -> str:
    """归一化微信聊天标题，去掉群人数后缀等展示噪声。"""
    text = _normalize_message_text(text)
    text = re.sub(r"\s*[（(]\d+[)）]\s*$", "", text)
    return text.strip()


def _current_chat_title_matches(root, group_name: str) -> bool:
    """快速判断当前聊天标题是否已经是目标群，避免重复点击左侧会话。

    只扫描窗口顶部标题区域。不要在发送热路径里全量遍历控件树，否则
    macOS 微信 AX 偶尔会把一次切群确认拖到几十秒。
    """
    expected = _normalize_chat_title(group_name)
    if not expected:
        return False

    try:
        root_rect = root.BoundingRectangle
    except Exception:
        root_rect = None

    if not root_rect:
        return False

    try:
        for control, _depth in platform.automation.walk_control(root, include_top=True, max_depth=3):
            name = _normalize_chat_title(_safe_text(control, "Name"))
            if name != expected:
                continue
            try:
                rect = control.BoundingRectangle
            except Exception:
                continue

            in_header = rect.top <= root_rect.top + 120
            in_main_title_area = rect.left >= root_rect.left + 220
            in_detached_title_area = rect.left >= root_rect.left + 12
            if in_header and (in_main_title_area or in_detached_title_area):
                return True
    except Exception:
        return False
    return False


def _get_wechat_ax_windows():
    try:
        from ...platform import _macos
    except Exception:
        return None, []

    try:
        app_ref = _macos._get_wechat_app_ref()
        if not app_ref:
            return _macos, []
        windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
        return _macos, list(windows or [])
    except Exception:
        return _macos, []


def _close_ax_window(macos_module, window) -> bool:
    if macos_module._ax_perform_action(window, "AXClose"):
        return True

    try:
        buttons = macos_module._ax_find_descendants(window, role="AXButton", max_depth=4)
        for button in buttons:
            subrole = str(macos_module._ax_get_attr(button, "AXSubrole") or "")
            title = macos_module._ax_get_title(button)
            desc = macos_module._ax_get_description(button)
            if subrole == "AXCloseButton" or "关闭" in f"{title} {desc}":
                if macos_module._ax_perform_action(button, "AXPress"):
                    return True
    except Exception:
        pass

    try:
        pos = macos_module._ax_get_position(window)
        if pos:
            platform.input.mouse_click(int(pos[0] + 14), int(pos[1] + 14))
            return True
    except Exception:
        pass
    return False


def _close_detached_group_windows(group_name: str, expected_message: str = "") -> Tuple[bool, bool]:
    """关闭目标群独立聊天窗口。

    Returns:
        (是否关闭过窗口, 是否确认过回复可见；当前不做深度确认，恒为 False)
    """
    macos_module, windows = _get_wechat_ax_windows()
    if not macos_module:
        return False, False

    closed = False
    message_visible = False
    expected_title = _normalize_chat_title(group_name)
    for window in windows:
        try:
            title = _normalize_chat_title(macos_module._ax_get_title(window))
            if title != expected_title:
                continue
            if _close_ax_window(macos_module, window):
                closed = True
                logger.warning("已自动关闭目标群独立聊天窗口: %s", group_name)
        except Exception as exc:
            logger.debug("关闭独立聊天窗口失败: %s: %s", group_name, exc)
    if closed:
        time.sleep(0.4)
    return closed, message_visible


def _get_detached_group_root(group_name: str):
    """获取目标群独立聊天窗口的控件根节点；没有则返回 None。"""
    macos_module, windows = _get_wechat_ax_windows()
    if not macos_module:
        return None

    expected_title = _normalize_chat_title(group_name)
    for window in windows:
        try:
            title = _normalize_chat_title(macos_module._ax_get_title(window))
            if title != expected_title:
                continue
            macos_module._ax_perform_action(window, "AXRaise")
            wid = macos_module._register_window(window)
            root = platform.automation.control_from_handle(wid)
            if root:
                return root
        except Exception as exc:
            logger.debug("获取独立聊天窗口失败: %s: %s", group_name, exc)
    return None


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


def _parse_quote_from_text(text: str) -> Tuple[Optional[str], Optional[str], str]:
    """尝试从消息文本中解析引用内容。

    支持多种可能的引用格式：
      - [引用 发送者: 内容] 回复
      - [引用 发送者：内容] 回复
      - 发送者: 内容 | 回复
      - 发送者: 内容\n回复

    Returns:
        (quoted_sender, quoted_content, remaining_text)
    """
    if not text:
        return None, None, text

    # 模式 0: 微信气泡/预览中的引用格式
    # 例如: "@豆角他说什么引用 爸爸 的消息 : 车没这么便宜，估计要 500"
    pattern0 = re.compile(
        r'引用\s+(.+?)\s+的消息\s*[:：]\s*(.*?)(?:\n|$)',
        re.DOTALL
    )
    m = pattern0.search(text)
    if m:
        sender = m.group(1).strip()
        content = m.group(2).strip()
        if sender and content:
            return sender, content, ""

    # 模式 1: [引用 发送者: 内容] 回复  /  [引用 发送者：内容] 回复
    pattern1 = re.compile(
        r'^\[引用\s+([^:\]]+?)[:：]\s*(.*?)\]\s*(.*)$',
        re.DOTALL
    )
    m = pattern1.match(text)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()

    # 模式 2: 微信预览中直接拼接的引用，尝试用 "回复" 关键词分割
    # 例如: "爸爸: 哪里有北京有趣 回复: 他说的什么"
    pattern2 = re.compile(
        r'^(.*?)(?:\n|\r| 回复[:：]?\s*|\s+回复[:：]?\s+)(.*)$',
        re.DOTALL
    )
    m = pattern2.match(text)
    if m:
        possible_quote = m.group(1).strip()
        reply_text = m.group(2).strip()
        # 如果引用部分包含 "说:" 或 ":"，尝试提取发送者
        if ':' in possible_quote or '：' in possible_quote:
            sep = ':' if ':' in possible_quote else '：'
            parts = possible_quote.split(sep, 1)
            sender = parts[0].strip()
            content = parts[1].strip()
            if sender and content and len(sender) < 24:
                return sender, content, reply_text
        # 如果没有明确的发送者，但内容看起来像引用（较长且以标点结尾）
        if len(possible_quote) > 3 and possible_quote[-1] in '。！？.!?':
            return None, possible_quote, reply_text

    return None, None, text


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


# 常见文件扩展名白名单（用于微信预览省略 [文件] 标记时的 fallback 识别）
_FILE_EXT_PATTERN = re.compile(
    r"\.([a-zA-Z0-9]{2,6})(?:\s|$|[,;)\]}])"
)
_FILE_EXT_WHITELIST = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "zip", "rar", "7z", "tar", "gz", "bz2",
    "txt", "csv", "md", "json", "xml", "yaml", "yml",
    "png", "jpg", "jpeg", "gif", "bmp", "webp", "svg",
    "mp4", "mov", "avi", "mkv", "flv",
    "mp3", "wav", "aac", "flac", "ogg",
    "exe", "dmg", "pkg", "deb", "rpm",
}


def _is_image_attachment_marker(text: str) -> bool:
    """判断文本是否是微信图片/照片附件标记。"""
    value = str(text or "").strip()
    return bool(
        re.match(
            r"^(?:\[(?:图片|Image|照片|Photo)\](?:\s*.*)?|图片|Image|照片|Photo)$",
            value,
        )
    )


def _parse_attachment_from_text(
    text: str, group_name: str, current_content: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """从会话预览文本中解析附件信息。

    注意：微信会话列表项的 Name 属性可能包含历史消息（如旧文件预览 + 新文本消息）。
    为避免历史消息污染，优先只检查与 current_content 相邻的行。

    Returns:
        (attachment_name, attachment_type)
        attachment_type 取值: "file" | "image" | None
    """
    if not text:
        return None, None
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # 过滤掉元数据行，保留候选消息行
    candidate_lines = [
        line for line in lines
        if line != group_name
        and line not in {"消息免打扰"}
        and not re.match(r"^\d{1,2}:\d{2}$", line)
        and line not in {"已发送", "已接收", "Sent", "Received", "撤回了一条消息"}
    ]

    if not candidate_lines:
        return None, None

    # 确定需要检查的行索引
    check_indices: List[int] = []
    if current_content:
        # 在 candidate_lines 中找到 current_content 所在的位置
        content_idx = -1
        for i, line in enumerate(candidate_lines):
            if current_content in line or line in current_content:
                content_idx = i
                break
        if content_idx >= 0:
            # 检查 current_content 所在行及相邻行（附件预览可能在消息前后）
            if content_idx > 0:
                check_indices.append(content_idx - 1)
            check_indices.append(content_idx)
            if content_idx + 1 < len(candidate_lines):
                check_indices.append(content_idx + 1)
        else:
            # 没找到 current_content，回退到只检查最后一条候选行
            check_indices.append(len(candidate_lines) - 1)
    else:
        # 没有 current_content，回退到只检查最后一条候选行
        check_indices.append(len(candidate_lines) - 1)

    logger.info(
        "诊断-附件解析候选: group=%s, current_content=%r, candidate_lines=%r, check_indices=%r",
        group_name,
        current_content,
        candidate_lines,
        check_indices,
    )

    for idx in check_indices:
        check_line = candidate_lines[idx]
        logger.info(
            "诊断-附件检查行: group=%s, idx=%s, line=%r",
            group_name,
            idx,
            check_line,
        )
        # 跳过包含 @ 的消息正文行
        if "@" in check_line:
            logger.info(
                "诊断-附件检查跳过@行: group=%s, idx=%s, line=%r",
                group_name,
                idx,
                check_line,
            )
            continue

        # 去掉可能的发送者前缀，如 "成员昵称: [文件] xxx"
        content = check_line
        if ":" in content:
            content = content.split(":", 1)[1].strip()
        elif "：" in content:
            content = content.split("：", 1)[1].strip()
        if _is_image_attachment_marker(content):
            logger.info(
                "诊断-附件命中图片: group=%s, idx=%s, raw_line=%r",
                group_name,
                idx,
                check_line,
            )
            return None, "image"
        content = re.sub(r"^\[[^\]]+\]\s*", "", content).strip()

        # 策略 1: 匹配 [文件] filename 或 [File] filename
        m = re.match(r"^\[(?:文件|File)\]\s*(.+)$", content)
        if m:
            logger.info(
                "诊断-附件命中显式文件: group=%s, idx=%s, filename=%r, raw_line=%r",
                group_name,
                idx,
                m.group(1).strip(),
                check_line,
            )
            return m.group(1).strip(), "file"
        if _is_image_attachment_marker(content):
            logger.info(
                "诊断-附件命中图片: group=%s, idx=%s, raw_line=%r",
                group_name,
                idx,
                check_line,
            )
            return None, "image"

        # 策略 2: fallback — 微信预览可能省略 [文件] 标记，直接显示文件名
        m = _FILE_EXT_PATTERN.search(content)
        if m and m.group(1).lower() in _FILE_EXT_WHITELIST:
            logger.info(
                "诊断-附件命中文件名fallback: group=%s, idx=%s, filename=%r, raw_line=%r",
                group_name,
                idx,
                content,
                check_line,
            )
            return content, "file"

    logger.info(
        "诊断-附件解析无命中: group=%s, current_content=%r",
        group_name,
        current_content,
    )
    return None, None


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
    try:
        rect = control.BoundingRectangle
        x = rect.left + min(42, max(28, (rect.right - rect.left) // 7))
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
        self.pending_groups: Set[str] = set()
        self._next_pending_group_retry_at = 0.0
        self._pending_group_log_at: Dict[str, float] = {}
        self._reply_queue: "queue.Queue[_ReplyTask]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._current_send_group: Optional[str] = None
        self._current_send_group_at = 0.0
        self._current_send_surface_detached = False
        self._last_verify_closed_detached = False
        self._send_in_progress = False
        self._send_state_lock = threading.Lock()

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
            try:
                self._thread.join(timeout=5)
            except KeyboardInterrupt:
                pass
        if self._sender_thread and self._sender_thread.is_alive():
            try:
                self._sender_thread.join(timeout=5)
            except KeyboardInterrupt:
                pass

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
            self._open_session(group, startup=True)

        if self.pending_groups:
            logger.warning(
                "部分群聊当前不在左侧可见会话列表，先跳过并后台重试: %s",
                ", ".join(sorted(self.pending_groups)),
            )
            self._next_pending_group_retry_at = time.time() + PENDING_GROUP_RETRY_SECONDS

    def _open_session(self, group: str, *, startup: bool = False) -> bool:
        if group in self.sessions:
            self.pending_groups.discard(group)
            return True

        if self.reply_on_at and not self.group_nicknames.get(group):
            # 不在启动期点击左上角头像读取当前账号昵称。macOS 微信 4.x
            # 点击头像/关闭资料卡会扰动左侧虚拟会话列表，导致当前置顶群被
            # 挪出可见区域。没有昵称时仍可靠左侧 [有人@我] 预览作为 at_hint。
            logger.debug("跳过启动期昵称读取: group=%s", group)

        try:
            root = platform.automation.control_from_handle(self.client.window.hwnd)
            session_item = _find_session_item(root, group)
        except Exception as exc:
            logger.warning("群聊注册失败: group=%s error=%s", group, exc)
            self.pending_groups.add(group)
            return False

        if not session_item:
            self._log_pending_group_missing(group, startup=startup)
            self.pending_groups.add(group)
            return False

        has_startup_at_hint = _session_item_has_at_me(session_item)
        self.sessions[group] = _ListenSession(
            group=group,
            hwnd=self.client.window.hwnd,
            root=root,
            msg_list=None,
            seen=set(),
            seen_texts={},
            at_hint_pending=has_startup_at_hint,
            at_hint_sender=_extract_sender_from_session_item(session_item, group),
            suppress_next_scan=has_startup_at_hint,
        )
        self.pending_groups.discard(group)
        logger.info(
            "群聊注册成功: group=%s startup=%s at_hint=%s",
            group,
            startup,
            has_startup_at_hint,
        )
        return True

    def _run_loop(self) -> None:
        logger.info(f"开始监听群聊: {', '.join(self.groups)}")
        while not self._stop_event.is_set():
            now = time.time()
            self._retry_pending_groups(now)
            for session in self._due_sessions(now):
                self._poll_session(session)
            time.sleep(self.tick)
        logger.info("群聊监听已停止")

    def _retry_pending_groups(self, now: float) -> None:
        if not self.pending_groups or now < self._next_pending_group_retry_at:
            return
        self._next_pending_group_retry_at = now + PENDING_GROUP_RETRY_SECONDS
        groups = list(self.pending_groups)
        logger.info("重试注册不可见群聊: groups=%s", groups)
        for group in groups:
            self._open_session(group, startup=False)

    def _log_pending_group_missing(self, group: str, *, startup: bool) -> None:
        now = time.time()
        last_at = self._pending_group_log_at.get(group, 0.0)
        if startup or now - last_at >= 60.0:
            logger.warning(
                "群聊当前不可见，暂不注册监听: group=%s startup=%s",
                group,
                startup,
            )
            self._pending_group_log_at[group] = now
        else:
            logger.debug(
                "群聊当前不可见，暂不注册监听: group=%s startup=%s",
                group,
                startup,
            )

    def _due_sessions(self, now: float) -> List[_ListenSession]:
        sessions = [
            session for session in self.sessions.values()
            if session.next_scan_at <= now
        ]
        sessions.sort(key=lambda session: session.next_scan_at)
        return sessions[:self.batch_size]

    def _poll_session(self, session: _ListenSession) -> None:
        session.scan_count += 1
        try:
            session_item = _find_session_item(self.client.window.uia.root, session.group)
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

            # 记录原始 UIA Name，帮助验证引用消息实际格式
            raw_name = _safe_text(session_item, "Name")
            logger.info(
                "诊断-左侧会话原始Name: group=%s, raw_name=%r",
                session.group,
                raw_name,
            )
            attachment_name, attachment_type = _parse_attachment_from_text(
                raw_name, session.group, current_content=content
            )
            if attachment_name or attachment_type:
                logger.info(
                    "附件识别: group=%s, name=%r, type=%r",
                    session.group, attachment_name, attachment_type,
                )
            logger.debug(
                "引用调试: group=%s, raw_name=%r, extracted_content=%r",
                session.group, raw_name, content,
            )

            quoted_sender, quoted_content, clean_content = _parse_quote_from_text(content)
            if quoted_content:
                logger.info(
                    "引用解析成功: group=%s, quoted_sender=%r, quoted_content=%r, clean=%r",
                    session.group, quoted_sender, quoted_content, clean_content,
                )
            logger.info(
                "诊断-当前解析字段: group=%s, sender=%r, content=%r, clean=%r, quoted_sender=%r, quoted_content=%r, attachment_name=%r, attachment_type=%r",
                session.group,
                sender_nickname,
                content,
                clean_content,
                quoted_sender,
                quoted_content,
                attachment_name,
                attachment_type,
            )

            # 左侧预览未包含引用内容时，尝试从聊天窗口消息气泡读取
            if not quoted_content:
                bubble_sender, bubble_content = self._fetch_quote_from_chat(
                    session,
                    sender_nickname=sender_nickname,
                    clean_content=clean_content,
                )
                if bubble_content:
                    quoted_sender = bubble_sender
                    quoted_content = bubble_content
                    logger.info(
                        "从聊天气泡获取引用: group=%s, quoted_sender=%r, quoted_content=%r",
                        session.group, quoted_sender, quoted_content,
                    )
                    logger.info(
                        "诊断-气泡引用后字段: group=%s, clean=%r, quoted_sender=%r, quoted_content=%r, attachment_name=%r, attachment_type=%r",
                        session.group,
                        clean_content,
                        quoted_sender,
                        quoted_content,
                        attachment_name,
                        attachment_type,
                    )

            # 如果引用内容中包含文件/图片，提取附件信息（REQ-002）
            if quoted_content and not attachment_name:
                q_lines = [
                    line.strip()
                    for line in quoted_content.splitlines()
                    if line.strip()
                ]
                for q_line in q_lines:
                    # 策略 A: 匹配 [文件] filename / [File] filename
                    m = re.match(r"^\[(?:文件|File)\]\s*(.+)$", q_line)
                    if m:
                        attachment_name = m.group(1).strip()
                        attachment_type = "file"
                        logger.info(
                            "引用内容识别为文件: group=%s, name=%r",
                            session.group, attachment_name,
                        )
                        break
                    # 策略 B: 匹配 [图片] / 图片 / [Image] 等图片引用标记
                    if _is_image_attachment_marker(q_line):
                        attachment_type = "image"
                        logger.info(
                            "引用内容识别为图片: group=%s", session.group
                        )
                        break
                    # 策略 C: fallback — 引用内容直接是文件名（微信气泡省略 [文件] 标记）
                    m_ext = _FILE_EXT_PATTERN.search(q_line)
                    if m_ext and m_ext.group(1).lower() in _FILE_EXT_WHITELIST:
                        attachment_name = q_line
                        attachment_type = "file"
                        logger.info(
                            "引用内容识别为文件(fallback): group=%s, name=%r",
                            session.group, attachment_name,
                        )
                        break

            # 兜底校验：如果从 raw_name 解析出了附件，但当前消息不是引用消息
            # 且消息正文中没有明确提到文件/附件，则认为是历史消息污染，清空附件
            if attachment_name and not quoted_content:
                has_file_hint = bool(
                    re.search(r"\[文件\]|\[File\]|附件|文件", clean_content, re.IGNORECASE)
                )
                logger.info(
                    "诊断-附件清理判断: group=%s, attachment=%r, quoted_content=%r, clean=%r, has_file_hint=%s",
                    session.group,
                    attachment_name,
                    quoted_content,
                    clean_content,
                    has_file_hint,
                )
                if not has_file_hint:
                    logger.info(
                        "忽略疑似历史消息附件: group=%s, attachment=%r, clean=%r",
                        session.group, attachment_name, clean_content,
                    )
                    attachment_name = None
                    attachment_type = None
            elif attachment_name and quoted_content:
                logger.info(
                    "诊断-附件未清理原因: group=%s, attachment=%r, quoted_content_present=True, quoted_content=%r",
                    session.group,
                    attachment_name,
                    quoted_content,
                )
            elif not attachment_name:
                logger.info(
                    "诊断-附件清理跳过: group=%s, reason=no_attachment, clean=%r",
                    session.group,
                    clean_content,
                )

            logger.info(
                "诊断-最终事件字段: group=%s, sender=%r, clean=%r, quoted_sender=%r, quoted_content=%r, attachment_name=%r, attachment_type=%r",
                session.group,
                sender_nickname,
                clean_content,
                quoted_sender,
                quoted_content,
                attachment_name,
                attachment_type,
            )

            normalized_text = _normalize_message_text(clean_content)
            if normalized_text:
                # 同一群里不同人可能短时间发送相同 @ 内容，去重必须包含发送人。
                # 只按内容去重会把第二个人的消息当作重复事件吞掉。
                dedupe_sender = _normalize_message_text(sender_nickname or "")
                normalized_key = f"{dedupe_sender}\n{normalized_text}"
                now = time.time()
                expired_texts = [
                    text
                    for text, seen_at in session.seen_texts.items()
                    if now - seen_at > TEXT_DEDUPE_TTL
                ]
                for text in expired_texts:
                    session.seen_texts.pop(text, None)
                if normalized_key in session.seen_texts:
                    logger.info(
                        "跳过重复左侧 @ 摘要: group=%s sender=%r content=%r",
                        session.group,
                        sender_nickname,
                        clean_content,
                    )
                    self._update_next_scan(session, 0)
                    return
                session.seen_texts[normalized_key] = now
            if self.ignore_client_sent and self.outgoing_registry.should_ignore(session.group, clean_content):
                self._update_next_scan(session, 0)
                return
            self._handle_text_message(
                session,
                clean_content,
                sender_nickname=sender_nickname,
                at_hint=True,
                quoted_sender=quoted_sender,
                quoted_content=quoted_content,
                attachment_name=attachment_name,
                attachment_type=attachment_type,
            )
            session.at_hint_pending = False
            session.at_hint_sender = None
            self._update_next_scan(session, 1)
            return
        except Exception as exc:
            session.fail_count += 1
            logger.warning("读取群聊消息失败: %s: %s", session.group, exc)
            return

    def _fetch_quote_from_chat(
        self,
        session: _ListenSession,
        sender_nickname: Optional[str] = None,
        clean_content: str = "",
    ) -> Tuple[Optional[str], Optional[str]]:
        """点击打开群聊，读取最新消息气泡，尝试提取引用内容。

        注意：这会临时切换当前聊天窗口到目标群聊。

        这段是自动回复发送链路的前置步骤：收到 @ 后，如果当前右侧
        聊天窗口不在目标群，这里会先点击左侧目标群以读取引用/图片。
        后续模型生成回复后会进入 _activate_group_for_send。由于微信 4.x
        在“当前会话已打开”时再次点击左侧同一个会话，会把右侧聊天区
        切成空白态，所以这里点击成功后必须写入发送状态，供发送阶段
        复用，避免第二次点击同一个群。
        """
        try:
            # 如果当前有发送正在进行，跳过点击切群，避免覆盖发送状态
            _, _, _, in_progress = self._get_send_state()
            if in_progress:
                logger.debug("获取引用: 发送进行中，跳过点击切群 group=%s", session.group)
                return None, None

            # 获取左侧会话项并点击打开群聊
            root = platform.automation.control_from_handle(self.client.window.hwnd)
            session_item = _find_session_item(root, session.group)
            if not session_item:
                logger.debug("获取引用: 未找到会话项 group=%s", session.group)
                return None, None

            # 如果当前聊天窗口已经在目标群，跳过点击
            if _current_chat_title_matches(root, session.group):
                logger.debug("获取引用: 当前已在目标群，跳过点击 group=%s", session.group)
            else:
                logger.debug("获取引用: 点击打开群聊 group=%s", session.group)
                _click_session_item(session_item)
                time.sleep(0.3)
                # 记录“目标群刚刚被读取阶段打开”。发送阶段会优先检查这个
                # 状态并跳过左侧点击；不要删掉，否则模型回复后可能二次点击
                # 当前群，导致微信右侧聊天区变成空白。
                self._set_send_state(group=session.group, group_at=time.time())
                try:
                    session.root = platform.automation.control_from_handle(self.client.window.hwnd)
                except Exception:
                    pass

            # 重新获取 root，查找消息列表（带重试）
            msg_list = None
            for attempt in range(3):
                root = platform.automation.control_from_handle(self.client.window.hwnd)
                msg_list = _find_message_list(root)
                if msg_list:
                    break
                time.sleep(0.3)

            if not msg_list:
                logger.debug("获取引用: 未找到消息列表 group=%s", session.group)
                return None, None

            children = list(msg_list.GetChildren())
            if not children:
                logger.debug("获取引用: 消息列表为空 group=%s", session.group)
                return None, None

            logger.debug("获取引用: 消息列表共 %d 条 group=%s", len(children), session.group)
            diag_items = []
            for idx, child in enumerate(reversed(children[-10:]), 1):
                cls = _safe_text(child, "ClassName")
                auto_id = _safe_text(child, "AutomationId")
                name = _safe_text(child, "Name")
                diag_items.append(
                    f"#{idx} class={cls!r} auto_id={auto_id!r} name={name!r}"
                )
            logger.info(
                "诊断-右侧最近气泡原始Name: group=%s, items=[%s]",
                session.group,
                "; ".join(diag_items),
            )

            # 辅助函数：从单个子控件解析引用
            def _try_parse_child(child) -> Tuple[Optional[str], Optional[str]]:
                cls = _safe_text(child, "ClassName")
                auto_id = _safe_text(child, "AutomationId")
                name = _safe_text(child, "Name")
                logger.debug(
                    "获取引用: 检查子控件 class=%r auto_id=%r name=%.80r group=%s",
                    cls, auto_id, name, session.group,
                )
                if cls not in MESSAGE_CLASSES and auto_id != MACOS_MESSAGE_AUTOMATION_ID:
                    return None, None
                if not name:
                    logger.debug("获取引用: 子控件 name 为空，跳过 group=%s", session.group)
                    return None, None
                logger.debug("气泡原始文本: group=%s, name=%r", session.group, name)
                qs, qc, _ = _parse_quote_from_text(name)
                if qc:
                    logger.info("气泡引用解析成功: sender=%r, content=%r", qs, qc)
                    return qs, qc
                result = self._extract_quote_from_bubble(name)
                if result[1]:
                    logger.info("气泡引用解析成功(换行): sender=%r, content=%r", result[0], result[1])
                    return result
                return None, None

            # 第一轮：优先匹配当前消息内容对应的气泡。
            # macOS 微信右侧消息气泡 Name 经常不包含群成员昵称，只包含 "@机器人 ... 引用 ..."。
            # sender_nickname 只能作为增强信息，不能作为硬条件。
            if clean_content:
                normalized_clean = _normalize_message_text(clean_content)
                for child in reversed(children[-10:]):
                    name = _safe_text(child, "Name")
                    normalized_name = _normalize_message_text(name)
                    if normalized_clean in normalized_name:
                        logger.debug(
                            "获取引用: 匹配到目标气泡 sender=%r content=%.40r group=%s",
                            sender_nickname, clean_content, session.group,
                        )
                        result = _try_parse_child(child)
                        if result[1]:
                            logger.info(
                                "诊断-引用气泡命中: group=%s, source=matched_current_message, sender=%r, sender_in_bubble=%s, clean=%r, bubble_name=%r, quoted=%r",
                                session.group,
                                sender_nickname,
                                bool(sender_nickname and sender_nickname in name),
                                clean_content,
                                name,
                                result,
                            )
                            return result

            # 第二轮：只诊断最近引用气泡，不再返回。
            # 直接返回最近任意引用会把上一条文件引用污染到“很好/不错”等纯文本消息。
            for child in reversed(children[-10:]):
                name = _safe_text(child, "Name")
                result = _try_parse_child(child)
                if result[1]:
                    logger.info(
                        "诊断-引用气泡忽略: group=%s, source=fallback_recent_any, sender=%r, clean=%r, bubble_name=%r, quoted=%r",
                        session.group,
                        sender_nickname,
                        clean_content,
                        name,
                        result,
                    )
                    break

            logger.debug("获取引用: 遍历 %d 条消息未找到引用内容 group=%s", min(len(children), 10), session.group)
            return None, None
        except Exception as exc:
            logger.warning("读取聊天引用内容失败: %s: %s", session.group, exc)
            return None, None

    @staticmethod
    def _extract_quote_from_bubble(name: str) -> Tuple[Optional[str], Optional[str]]:
        """从消息气泡 Name 属性中提取引用内容，支持多种微信格式。"""
        if not name or '\n' not in name:
            return None, None

        lines = [l.strip() for l in name.split('\n') if l.strip()]
        if len(lines) < 2:
            return None, None

        # 格式推测 1: 微信引用消息常见格式
        # 引用
        # 发送者
        # 被引用内容
        # 回复内容
        if lines[0] in {"引用", "Reply", "Re", "⤷"}:
            reply_idx = None
            for i, line in enumerate(lines):
                if '@' in line and i > 0:
                    reply_idx = i
                    break
            if reply_idx is None:
                reply_idx = len(lines) - 1
            if reply_idx > 1:
                quote_lines = lines[1:reply_idx]
                sender = None
                if quote_lines:
                    first = quote_lines[0]
                    if len(first) <= 20 and not any(p in first for p in '。！？.!?；;：:,，'):
                        sender = first
                        quote_lines = quote_lines[1:]
                quote_content = '\n'.join(quote_lines)
                if quote_content:
                    return sender, quote_content

        # 格式推测 2: 被引用内容和回复内容直接换行分隔
        # 发送者: 被引用内容
        # 回复内容（含 @）
        for i in range(len(lines) - 1, 0, -1):
            line = lines[i]
            if '@' in line:
                quote_lines = lines[:i]
                sender = None
                content_parts = []
                for ql in quote_lines:
                    if (':' in ql or '：' in ql) and not sender:
                        sep = ':' if ':' in ql else '：'
                        parts = ql.split(sep, 1)
                        ps = parts[0].strip()
                        pc = parts[1].strip()
                        if ps and pc and len(ps) < 24:
                            sender = ps
                            content_parts.append(pc)
                        else:
                            content_parts.append(ql)
                    else:
                        content_parts.append(ql)
                quote_content = '\n'.join(content_parts)
                if quote_content:
                    return sender, quote_content

        # 格式推测 3: 只有两行，第一行是被引用内容，第二行是回复
        if len(lines) == 2:
            first = lines[0]
            if ':' in first or '：' in first:
                sep = ':' if ':' in first else '：'
                parts = first.split(sep, 1)
                if len(parts) == 2:
                    sender = parts[0].strip()
                    content = parts[1].strip()
                    if sender and content and len(sender) < 24:
                        return sender, content
            return None, first

        return None, None

    def _handle_text_message(
        self,
        session: _ListenSession,
        content: str,
        at_hint: bool = False,
        sender_nickname: Optional[str] = None,
        quoted_sender: Optional[str] = None,
        quoted_content: Optional[str] = None,
        attachment_name: Optional[str] = None,
        attachment_type: Optional[str] = None,
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
            quoted_sender=quoted_sender,
            quoted_content=quoted_content,
            attachment_name=attachment_name,
            attachment_type=attachment_type,
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
        quoted_sender, quoted_content, clean_content = _parse_quote_from_text(item.name)
        attachment_name, attachment_type = _parse_attachment_from_text(
            item.name, session.group, current_content=clean_content
        )
        event = MessageEvent(
            group=session.group,
            content=clean_content,
            timestamp=time.time(),
            group_nickname=self.group_nicknames.get(session.group),
            sender_nickname=sender_nickname,
            is_at_me=self._is_at_me(session.group, clean_content) or (
                at_hint and not self.group_nicknames.get(session.group)
            ),
            raw=item.control,
            quoted_sender=quoted_sender,
            quoted_content=quoted_content,
            attachment_name=attachment_name,
            attachment_type=attachment_type,
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

    def reply(self, group: str, content: str, *, send_id: str = "") -> bool:
        """立即在微信主窗口回复群聊。

        注意：该方法会直接操作窗口、剪贴板和焦点。自动回复默认不直接调用它，
        而是进入发送队列，由单个 sender 线程串行发送，避免多个群同时回复时
        抢占窗口。
        """
        session = self.sessions.get(group)
        if not session:
            raise ValueError(f"未监听群聊: {group}")

        started_at = time.time()
        self._log_send_stage(
            "reply_start",
            group=group,
            send_id=send_id,
            reply=content,
        )
        delay = self._sleep_before_reply(group, content)
        self._log_send_stage(
            "reply_delay_done",
            group=group,
            send_id=send_id,
            reply=content,
            elapsed_ms=int(delay * 1000),
        )
        if self._stop_event.is_set():
            return False

        if self.ignore_client_sent:
            # 先登记，再发送，避免微信回流速度快于登记速度导致漏判。
            self.outgoing_registry.record(group, content)

        sent = self._send_in_subwindow(session, content, send_id=send_id)
        self._log_send_stage(
            "reply_done",
            group=group,
            send_id=send_id,
            success=sent,
            reply=content,
            elapsed_ms=int((time.time() - started_at) * 1000),
        )
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
        self._stop_event.wait(delay)
        return delay

    def enqueue_reply(self, group: str, content: str) -> None:
        """将回复加入串行发送队列。"""
        content = (content or "").strip()
        if not content:
            return
        send_id = uuid.uuid4().hex[:12]
        task = _ReplyTask(
            group=group,
            content=content,
            send_id=send_id,
            enqueued_at=time.time(),
        )
        self._reply_queue.put(task)
        self._log_send_stage(
            "queued",
            group=group,
            send_id=send_id,
            reply=content,
            queue_size=self._reply_queue.qsize(),
        )

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
                self._log_send_stage(
                    "dequeued",
                    group=task.group,
                    send_id=task.send_id,
                    reply=task.content,
                    queued_ms=int((time.time() - task.enqueued_at) * 1000),
                    queue_size=self._reply_queue.qsize(),
                )
                self.reply(task.group, task.content, send_id=task.send_id)
            except Exception as exc:
                logger.exception(f"发送队列回复失败: {task.group}: {exc}")
                log_error_audit(
                    "reply_queue_failed",
                    {"group": task.group, "reply": task.content},
                    exc,
                )
            finally:
                self._reply_queue.task_done()

    @staticmethod
    def _log_send_stage(
        stage: str,
        *,
        group: str,
        send_id: str = "",
        attempt: int = 0,
        success: Optional[bool] = None,
        reason: str = "",
        elapsed_ms: Optional[int] = None,
        reply: str = "",
        **extra,
    ) -> None:
        payload = {
            "kind": "group_reply_send",
            "stage": stage,
            "send_id": send_id,
            "group": group,
            "attempt": attempt,
            "success": success,
            "reason": reason,
            "elapsed_ms": elapsed_ms,
            "reply_len": len(reply or ""),
            "reply_preview": (reply or "")[:120],
            **extra,
        }
        log_send_audit(payload)

    def _get_send_state(self):
        """原子读取当前发送状态快照。"""
        with self._send_state_lock:
            return (
                self._current_send_group,
                self._current_send_group_at,
                self._current_send_surface_detached,
                self._send_in_progress,
            )

    def _set_send_state(
        self,
        group=_UNSET,
        group_at=_UNSET,
        detached=_UNSET,
        in_progress=_UNSET,
    ):
        """原子更新发送状态。"""
        with self._send_state_lock:
            if group is not _UNSET:
                self._current_send_group = group
            if group_at is not _UNSET:
                self._current_send_group_at = group_at
            if detached is not _UNSET:
                self._current_send_surface_detached = detached
            if in_progress is not _UNSET:
                self._send_in_progress = in_progress

    @staticmethod
    def _validate_control_cache(control) -> bool:
        """检查缓存的控件引用是否仍然有效。"""
        if control is None:
            return False
        try:
            return bool(control.Exists(maxSearchSeconds=0.02))
        except Exception:
            return False

    def _get_main_root(self):
        """激活并获取微信主窗口控件根节点。"""
        try:
            self.client.window.activate()
        except Exception as exc:
            logger.debug(f"激活微信主窗口失败: {exc}")

        try:
            return platform.automation.control_from_handle(self.client.window.hwnd)
        except Exception as exc:
            logger.debug(f"使用现有微信窗口句柄获取控件树失败: {exc}")

        try:
            hwnd = platform.window_manager.find_wechat_window()
            if hwnd:
                self.client.window._hwnd = hwnd
                return platform.automation.control_from_handle(hwnd)
        except Exception as exc:
            logger.debug(f"获取微信主窗口控件树失败: {exc}")
            return None

    def _recover_send_surface(
        self,
        session: _ListenSession,
        *,
        reason: str,
        content: str = "",
        attempt: int = 0,
        send_id: str = "",
    ) -> bool:
        """恢复发送表面：关闭弹窗/独立聊天窗口，重新激活主窗口。

        Returns:
            如果回复已经出现在独立窗口里，返回 True，避免重复发送。
        """
        logger.warning(
            "发送恢复流程: group=%s, attempt=%s/%s, reason=%s",
            session.group,
            attempt or "-",
            SEND_ATTEMPT_LIMIT,
            reason,
        )
        log_error_audit(
            "send_recovery",
            {
                "group": session.group,
                "reason": reason,
                "attempt": attempt,
            },
        )
        self._log_send_stage(
            "recover_start",
            group=session.group,
            send_id=send_id,
            attempt=attempt,
            reason=reason,
            reply=content,
        )

        started_at = time.time()
        detached_root = _get_detached_group_root(session.group) if content else None
        if detached_root:
            logger.warning("恢复时保留目标群独立窗口，下一轮直接在独立窗口发送: %s", session.group)
            session.root = detached_root
            self._set_send_state(group=None, detached=True)
            self._stop_event.wait(RECOVERY_PAUSE_SECONDS)
            self._log_send_stage(
                "recover_done",
                group=session.group,
                send_id=send_id,
                attempt=attempt,
                success=True,
                reason="detached_root_kept",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False

        closed, _ = _close_detached_group_windows(session.group)
        if closed:
            self._set_send_state(group=None)

        for root in (session.root, self._get_main_root()):
            if not root:
                continue
            _dismiss_send_failure_dialog(root)
            try:
                root.SendKeys("{Esc}")
            except Exception:
                pass
            session.root = root

        self._set_send_state(group=None)
        self._stop_event.wait(RECOVERY_PAUSE_SECONDS)
        self._log_send_stage(
            "recover_done",
            group=session.group,
            send_id=send_id,
            attempt=attempt,
            success=True,
            reason="main_root_restored",
            elapsed_ms=int((time.time() - started_at) * 1000),
            reply=content,
            closed_detached=closed,
        )
        return False

    def _send_in_subwindow(self, session: _ListenSession, content: str, *, send_id: str = "") -> bool:
        last_reason = ""
        overall_started_at = time.time()
        for attempt in range(1, SEND_ATTEMPT_LIMIT + 1):
            attempt_started_at = time.time()
            self._log_send_stage(
                "attempt_start",
                group=session.group,
                send_id=send_id,
                attempt=attempt,
                reply=content,
            )
            if self._stop_event.is_set():
                return False
            if attempt > 1:
                recovered_visible = self._recover_send_surface(
                    session,
                    reason=last_reason or "retry",
                    content=content,
                    attempt=attempt,
                    send_id=send_id,
                )
                if recovered_visible:
                    self._set_send_state(group=session.group, group_at=time.time())
                    self._log_send_stage(
                        "attempt_done",
                        group=session.group,
                        send_id=send_id,
                        attempt=attempt,
                        success=True,
                        reason="recovered_visible",
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        reply=content,
                    )
                    return True

            current_group, _, current_detached, _ = self._get_send_state()
            if current_group != session.group:
                self._set_send_state(group=None)
            self._set_send_state(detached=False)
            # 标记发送进行中，防止监视线程的 _fetch_quote_from_chat 覆盖发送状态
            self._set_send_state(in_progress=True)
            if not self._activate_group_for_send(session, send_id=send_id, send_attempt=attempt):
                self._set_send_state(in_progress=False)
                last_reason = "activate_group_failed"
                logger.warning(
                    "发送尝试 %s/%s 切换目标群失败: %s",
                    attempt,
                    SEND_ATTEMPT_LIMIT,
                    session.group,
                )
                self._log_send_stage(
                    "attempt_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason=last_reason,
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    reply=content,
                )
                continue

            root = session.root
            sent = self._send_text_via_chat_input(root, session.group, content, attempt, send_id=send_id)
            failure_check_started_at = time.time()
            failure_dialog = _dismiss_send_failure_dialog(session.root)
            self._log_send_stage(
                "failure_dialog_checked",
                group=session.group,
                send_id=send_id,
                attempt=attempt,
                success=not failure_dialog,
                reason="found" if failure_dialog else "not_found",
                elapsed_ms=int((time.time() - failure_check_started_at) * 1000),
                reply=content,
            )
            if failure_dialog:
                logger.error(f"微信提示发送失败: {session.group}")
                self._set_send_state(group=None, detached=False, in_progress=False)
                log_error_audit(
                    "wechat_send_failure_dialog",
                    {"group": session.group, "reply": content, "attempt": attempt},
                )
                last_reason = "wechat_send_failure_dialog"
                self._log_send_stage(
                    "attempt_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason=last_reason,
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    reply=content,
                )
                continue
            if sent and self._verify_reply_visible(
                session,
                content,
                timeout=SEND_VERIFY_TIMEOUT,
                send_id=send_id,
                attempt=attempt,
            ):
                _, _, verify_detached, _ = self._get_send_state()
                if verify_detached:
                    _close_detached_group_windows(session.group, expected_message=content)
                    self._set_send_state(detached=False)
                    session.root = None
                    self._stop_event.wait(0.3)
                self._set_send_state(group=session.group, group_at=time.time(), in_progress=False)
                self._log_send_stage(
                    "attempt_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="verified_visible",
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    reply=content,
                )
                return True
            if sent and self._last_verify_closed_detached and attempt < SEND_ATTEMPT_LIMIT:
                logger.warning("检测到回复流程进入独立聊天窗口，下一轮改用独立窗口发送: %s", session.group)
                log_error_audit(
                    "detached_window_detected_retry",
                    {"group": session.group, "reply": content, "attempt": attempt},
                )
                self._set_send_state(group=None, detached=False, in_progress=False)
                last_reason = "detached_window_detected_retry"
                self._stop_event.wait(0.8)
                self._log_send_stage(
                    "attempt_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason=last_reason,
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    reply=content,
                )
                continue
            if sent:
                last_reason = "reply_not_visible_after_send"
                logger.warning(
                    "发送尝试 %s/%s 后未确认到回复: group=%s reply_len=%s preview=%s",
                    attempt,
                    SEND_ATTEMPT_LIMIT,
                    session.group,
                    len(content),
                    content[:80],
                )
                log_error_audit(
                    "reply_not_visible_after_send",
                    {"group": session.group, "reply": content, "attempt": attempt},
                )
                self._log_send_stage(
                    "attempt_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason=last_reason,
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    reply=content,
                )
                continue
            last_reason = "send_text_failed"
            logger.warning(
                "发送尝试 %s/%s 写入或发送失败: %s",
                attempt,
                SEND_ATTEMPT_LIMIT,
                session.group,
            )
            self._log_send_stage(
                "attempt_done",
                group=session.group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason=last_reason,
                elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                reply=content,
            )

        logger.error(
            "群聊回复发送最终失败: %s, reason=%s, reply=%s",
            session.group,
            last_reason,
            content[:120],
        )
        self._set_send_state(group=None, detached=False, in_progress=False)
        log_error_audit(
            "reply_failed_after_retries",
            {"group": session.group, "reply": content, "reason": last_reason},
        )
        self._log_send_stage(
            "send_failed",
            group=session.group,
            send_id=send_id,
            success=False,
            reason=last_reason,
            elapsed_ms=int((time.time() - overall_started_at) * 1000),
            reply=content,
        )
        return False

    @staticmethod
    def _send_text_via_chat_input(
        root,
        group: str,
        content: str,
        attempt: int,
        *,
        send_id: str = "",
    ) -> bool:
        """通过明确的聊天输入框发送，避免坐标误点导致草稿残留。"""
        started_at = time.time()
        WeChatGroupListener._log_send_stage(
            "input_start",
            group=group,
            send_id=send_id,
            attempt=attempt,
            reply=content,
            root=bool(root),
        )
        if not root or not content:
            logger.warning(
                "发送输入阶段跳过: group=%s attempt=%s root=%s content_len=%s",
                group,
                attempt,
                bool(root),
                len(content or ""),
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="missing_root_or_content",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False

        WeChatGroupListener._log_send_surface(root, group, attempt, "before_find_input")
        find_started_at = time.time()
        edit = WeChatGroupListener._find_chat_input(root)
        WeChatGroupListener._log_send_stage(
            "input_found",
            group=group,
            send_id=send_id,
            attempt=attempt,
            success=bool(edit),
            elapsed_ms=int((time.time() - find_started_at) * 1000),
            reply=content,
        )
        if not edit:
            logger.warning(
                "发送输入阶段失败: group=%s attempt=%s reason=chat_input_missing",
                group,
                attempt,
            )
            log_error_audit(
                "send_input_missing",
                {"group": group, "attempt": attempt, "reply": content},
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="chat_input_missing",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False

        WeChatGroupListener._log_control_rect("发送输入框命中", edit, group, attempt)
        if not WeChatGroupListener._focus_chat_input(root, edit, group, attempt, send_id=send_id, reply=content):
            logger.warning(
                "发送输入阶段失败: group=%s attempt=%s reason=focus_input_failed",
                group,
                attempt,
            )
            log_error_audit(
                "send_input_focus_failed",
                {"group": group, "attempt": attempt, "reply": content},
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="focus_input_failed",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False

        if not WeChatGroupListener._clear_focused_input(edit, group, attempt, send_id=send_id, reply=content):
            logger.warning(
                "发送输入阶段失败: group=%s attempt=%s reason=clear_input_failed",
                group,
                attempt,
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="clear_input_failed",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False

        time.sleep(SEND_INPUT_SETTLE_SECONDS)
        paste_started_at = time.time()
        if not ChatWindow.paste_text_into_focused_input(
            content,
            log_error="写入回复到剪贴板失败",
            logger_override=logger,
        ):
            logger.warning(
                "发送输入阶段失败: group=%s attempt=%s reason=paste_failed",
                group,
                attempt,
            )
            WeChatGroupListener._log_send_stage(
                "paste_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="paste_failed",
                elapsed_ms=int((time.time() - paste_started_at) * 1000),
                reply=content,
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="paste_failed",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return False
        WeChatGroupListener._log_send_stage(
            "paste_done",
            group=group,
            send_id=send_id,
            attempt=attempt,
            success=True,
            elapsed_ms=int((time.time() - paste_started_at) * 1000),
            reply=content,
        )

        time.sleep(SEND_PASTE_SETTLE_SECONDS)
        WeChatGroupListener._log_send_surface(root, group, attempt, "after_paste")

        button = WeChatGroupListener._find_send_button(root)
        if button:
            WeChatGroupListener._log_control_rect("发送按钮命中", button, group, attempt)
            if _click_control(button):
                logger.info(
                    "发送动作完成: group=%s attempt=%s method=send_button",
                    group,
                    attempt,
                )
                time.sleep(0.3)
                WeChatGroupListener._log_send_stage(
                    "send_key_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="send_button",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return True
            logger.warning(
                "发送按钮点击失败: group=%s attempt=%s",
                group,
                attempt,
            )
        else:
            logger.info("发送按钮未命中，改用键盘发送: group=%s attempt=%s", group, attempt)

        enter_ok = False
        try:
            edit.SendKeys("{Enter}")
            enter_ok = True
            logger.info("发送键盘动作完成: group=%s attempt=%s method=input_enter", group, attempt)
            WeChatGroupListener._log_send_stage(
                "send_key_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=True,
                reason="input_enter",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            if WeChatGroupListener._reply_visible_in_root(root, _normalize_message_text(content)):
                logger.info("发送后快速确认成功: group=%s attempt=%s method=input_enter", group, attempt)
                WeChatGroupListener._log_send_stage(
                    "input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="input_enter_fast_verified",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return True
            time.sleep(SEND_FAST_VERIFY_SECONDS)
            if WeChatGroupListener._reply_visible_in_root(root, _normalize_message_text(content)):
                logger.info("发送后快速确认成功: group=%s attempt=%s method=input_enter_wait", group, attempt)
                WeChatGroupListener._log_send_stage(
                    "input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="input_enter_wait_verified",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return True
        except Exception as exc:
            logger.warning(
                "输入框 Enter 发送失败: group=%s attempt=%s error=%s",
                group,
                attempt,
                exc,
            )

        try:
            edit.SendKeys("{Ctrl}{Enter}")
            logger.info("发送键盘动作完成: group=%s attempt=%s method=input_cmd_enter", group, attempt)
            time.sleep(0.3)
            WeChatGroupListener._log_send_stage(
                "send_key_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=True,
                reason="input_cmd_enter",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=True,
                reason="input_cmd_enter_sent",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
            )
            return True
        except Exception as exc:
            if enter_ok:
                logger.warning(
                    "Cmd/Ctrl+Enter 发送失败，但 Enter 已执行: group=%s attempt=%s error=%s",
                    group,
                    attempt,
                    exc,
                )
                time.sleep(0.2)
                WeChatGroupListener._log_send_stage(
                    "input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="enter_executed_cmd_enter_failed",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return True
            logger.error(
                "发送动作最终失败: group=%s attempt=%s error=%s",
                group,
                attempt,
                exc,
            )
            log_error_audit(
                "send_action_failed",
                {"group": group, "attempt": attempt, "reply": content, "error": str(exc)},
            )
            WeChatGroupListener._log_send_stage(
                "input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="send_action_failed",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=content,
                error=str(exc),
            )
            return False

    @staticmethod
    def _clear_focused_input(
        edit,
        group: str,
        attempt: int,
        *,
        send_id: str = "",
        reply: str = "",
    ) -> bool:
        started_at = time.time()
        try:
            edit.SendKeys("{Ctrl}a")
            time.sleep(0.05)
            edit.SendKeys("{Delete}")
            time.sleep(0.05)
            logger.info("输入框清空完成: group=%s attempt=%s", group, attempt)
            WeChatGroupListener._log_send_stage(
                "clear_input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=True,
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=reply,
            )
            return True
        except Exception as exc:
            logger.warning("输入框清空失败: group=%s attempt=%s error=%s", group, attempt, exc)
            WeChatGroupListener._log_send_stage(
                "clear_input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="exception",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=reply,
                error=str(exc),
            )
            return False

    @staticmethod
    def _send_text_direct(root, content: str) -> bool:
        """不依赖输入框 AX 控件，直接点击窗口底部输入区并发送。"""
        if not root or not content:
            return False
        try:
            rect = root.BoundingRectangle
            if not rect:
                return False
            x = int(rect.left + rect.width * 0.58)
            y = int(rect.bottom - 58)
            platform.input.mouse_click(x, y)
            time.sleep(0.08)
            platform.input.send_combo(platform.input.VK_CONTROL, platform.input.VK_A, settle_time=0.04)
            platform.input.key_down(platform.input.VK_DELETE)
            time.sleep(0.02)
            platform.input.key_up(platform.input.VK_DELETE)
            if not platform.clipboard.set_text(content):
                logger.error("写入回复到剪贴板失败")
                return False
            platform.input.send_combo(platform.input.VK_CONTROL, platform.input.VK_V, settle_time=0.08)
            platform.input.key_down(platform.input.VK_RETURN)
            time.sleep(0.02)
            platform.input.key_up(platform.input.VK_RETURN)
            time.sleep(0.15)
            return True
        except Exception as exc:
            logger.debug("直接坐标发送失败: %s", exc)
            return False

    @staticmethod
    def _log_control_rect(prefix: str, control, group: str, attempt: int) -> None:
        try:
            rect = control.BoundingRectangle
            logger.info(
                "%s: group=%s attempt=%s name=%r auto_id=%r class=%r type=%r rect=(%s,%s,%s,%s)",
                prefix,
                group,
                attempt,
                _safe_text(control, "Name")[:120],
                _safe_text(control, "AutomationId"),
                _safe_text(control, "ClassName"),
                _safe_text(control, "ControlTypeName"),
                getattr(rect, "left", None),
                getattr(rect, "top", None),
                getattr(rect, "right", None),
                getattr(rect, "bottom", None),
            )
        except Exception as exc:
            logger.debug("%s日志失败: group=%s attempt=%s error=%s", prefix, group, attempt, exc)

    @staticmethod
    def _log_send_surface(root, group: str, attempt: int, phase: str) -> None:
        try:
            root_rect = root.BoundingRectangle
            msg_list = _find_message_list(root)
            edit = WeChatGroupListener._find_chat_input(root)
            button = WeChatGroupListener._find_send_button(root)
            logger.info(
                "发送表面检查: group=%s attempt=%s phase=%s root_rect=(%s,%s,%s,%s) "
                "msg_list=%s input=%s send_button=%s root_name=%r",
                group,
                attempt,
                phase,
                getattr(root_rect, "left", None),
                getattr(root_rect, "top", None),
                getattr(root_rect, "right", None),
                getattr(root_rect, "bottom", None),
                bool(msg_list),
                bool(edit),
                bool(button),
                _safe_text(root, "Name")[:120],
            )
        except Exception as exc:
            logger.debug(
                "发送表面检查失败: group=%s attempt=%s phase=%s error=%s",
                group,
                attempt,
                phase,
                exc,
            )

    @staticmethod
    def _focus_chat_input(
        root,
        edit,
        group: str,
        attempt: int,
        *,
        send_id: str = "",
        reply: str = "",
    ) -> bool:
        """把焦点明确放到当前群的聊天输入框，失败则禁止粘贴。"""
        started_at = time.time()
        WeChatGroupListener._log_send_stage(
            "focus_input_start",
            group=group,
            send_id=send_id,
            attempt=attempt,
            reply=reply,
        )
        try:
            try:
                hwnd = platform.window_manager.find_wechat_window()
                if hwnd:
                    platform.window_manager.bring_to_front(hwnd)
            except Exception as exc:
                logger.debug("发送前置前微信失败: group=%s attempt=%s error=%s", group, attempt, exc)

            edit_rect = edit.BoundingRectangle
            root_rect = root.BoundingRectangle
            if not edit_rect or not root_rect:
                logger.warning("输入框聚焦失败: group=%s attempt=%s reason=rect_missing", group, attempt)
                WeChatGroupListener._log_send_stage(
                    "focus_input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason="rect_missing",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=reply,
                )
                return False
            if edit_rect.top < root_rect.top + root_rect.height * 0.55:
                logger.warning(
                    "输入框聚焦失败: group=%s attempt=%s reason=input_not_in_bottom rect=(%s,%s,%s,%s)",
                    group,
                    attempt,
                    edit_rect.left,
                    edit_rect.top,
                    edit_rect.right,
                    edit_rect.bottom,
                )
                WeChatGroupListener._log_send_stage(
                    "focus_input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason="input_not_in_bottom",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=reply,
                )
                return False

            x = int((edit_rect.left + edit_rect.right) / 2)
            y = int((edit_rect.top + edit_rect.bottom) / 2)
            logger.info(
                "点击聊天输入框: group=%s attempt=%s x=%s y=%s",
                group,
                attempt,
                x,
                y,
            )
            platform.input.mouse_click(x, y)
            time.sleep(SEND_FOCUS_SETTLE_SECONDS)

            try:
                edit.SetFocus()
                time.sleep(SEND_FOCUS_SETTLE_SECONDS)
            except Exception as exc:
                logger.debug("输入框 SetFocus 失败: group=%s attempt=%s error=%s", group, attempt, exc)

            focused = None
            try:
                focused = platform.automation.get_focused_control()
            except Exception as exc:
                logger.debug("读取当前焦点失败: group=%s attempt=%s error=%s", group, attempt, exc)

            if not focused:
                logger.info("输入框焦点无法读取，已完成坐标点击: group=%s attempt=%s", group, attempt)
                WeChatGroupListener._log_send_stage(
                    "focus_input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="focused_unreadable_after_click",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=reply,
                )
                return True

            WeChatGroupListener._log_control_rect("发送前当前焦点", focused, group, attempt)
            focused_rect = focused.BoundingRectangle
            if WeChatGroupListener._rects_overlap(edit_rect, focused_rect, min_ratio=0.2):
                WeChatGroupListener._log_send_stage(
                    "focus_input_done",
                    group=group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="focused_input",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=reply,
                )
                return True

            logger.warning(
                "输入框聚焦失败: group=%s attempt=%s reason=focused_elsewhere "
                "focused_name=%r focused_auto_id=%r",
                group,
                attempt,
                _safe_text(focused, "Name")[:120],
                _safe_text(focused, "AutomationId"),
            )
            WeChatGroupListener._log_send_stage(
                "focus_input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="focused_elsewhere",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=reply,
                focused_name=_safe_text(focused, "Name")[:120],
                focused_auto_id=_safe_text(focused, "AutomationId"),
            )
            return False
        except Exception as exc:
            logger.warning("输入框聚焦异常: group=%s attempt=%s error=%s", group, attempt, exc)
            WeChatGroupListener._log_send_stage(
                "focus_input_done",
                group=group,
                send_id=send_id,
                attempt=attempt,
                success=False,
                reason="exception",
                elapsed_ms=int((time.time() - started_at) * 1000),
                reply=reply,
                error=str(exc),
            )
            return False

    @staticmethod
    def _rects_overlap(a, b, *, min_ratio: float = 0.1) -> bool:
        if not a or not b:
            return False
        left = max(a.left, b.left)
        top = max(a.top, b.top)
        right = min(a.right, b.right)
        bottom = min(a.bottom, b.bottom)
        if right <= left or bottom <= top:
            return False
        intersection = (right - left) * (bottom - top)
        area = max(1, (b.right - b.left) * (b.bottom - b.top))
        return intersection / area >= min_ratio

    def _verify_reply_visible(
        self,
        session: _ListenSession,
        content: str,
        timeout: float = 5.0,
        send_id: str = "",
        attempt: int = 0,
    ) -> bool:
        """确认回复已出现在当前聊天气泡中。"""
        expected = _normalize_message_text(content)
        if not expected:
            return False

        started_at = time.time()
        self._log_send_stage(
            "verify_start",
            group=session.group,
            send_id=send_id,
            attempt=attempt,
            reply=content,
            timeout=timeout,
        )
        self._last_verify_closed_detached = False
        deadline = time.time() + min(timeout, SEND_VERIFY_TIMEOUT)
        while time.time() < deadline:
            failure_check_started_at = time.time()
            failure_dialog = _dismiss_send_failure_dialog(session.root)
            self._log_send_stage(
                "failure_dialog_checked",
                group=session.group,
                send_id=send_id,
                attempt=attempt,
                success=not failure_dialog,
                reason="found" if failure_dialog else "not_found",
                elapsed_ms=int((time.time() - failure_check_started_at) * 1000),
                reply=content,
                phase="verify",
            )
            if failure_dialog:
                log_error_audit(
                    "wechat_send_failure_dialog",
                    {"group": session.group, "reply": content},
                )
                self._log_send_stage(
                    "verify_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason="wechat_send_failure_dialog",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return False
            _, _, current_detached, _ = self._get_send_state()
            if not current_detached:
                detached_root = _get_detached_group_root(session.group)
                if detached_root:
                    session.root = detached_root
                    self._last_verify_closed_detached = True
                    logger.warning("发送后快速检测到目标群独立窗口，改用独立窗口重发: %s", session.group)
                    self._log_send_stage(
                        "verify_done",
                        group=session.group,
                        send_id=send_id,
                        attempt=attempt,
                        success=False,
                        reason="detached_window_detected",
                        elapsed_ms=int((time.time() - started_at) * 1000),
                        reply=content,
                    )
                    return False
            if self._reply_visible_in_root(session.root, expected):
                self._log_send_stage(
                    "verify_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=True,
                    reason="reply_visible",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return True
            if self._stop_event.wait(0.15):
                self._log_send_stage(
                    "verify_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=attempt,
                    success=False,
                    reason="stopped",
                    elapsed_ms=int((time.time() - started_at) * 1000),
                    reply=content,
                )
                return False

        self._log_send_stage(
            "verify_done",
            group=session.group,
            send_id=send_id,
            attempt=attempt,
            success=False,
            reason="timeout",
            elapsed_ms=int((time.time() - started_at) * 1000),
            reply=content,
        )
        return False

    @staticmethod
    def _reply_visible_in_root(root, expected: str) -> bool:
        """检查最近可见消息气泡是否包含预期回复。"""
        if not root or not expected:
            return False
        msg_list = _find_message_list(root)
        if not msg_list:
            return False
        for item in reversed(_read_visible_items(msg_list)[-12:]):
            if item.kind != "message":
                continue
            actual = _normalize_message_text(item.name)
            if _is_same_outgoing_message(expected, actual):
                logger.info(
                    "回复可见校验命中: expected_len=%s actual_len=%s actual_preview=%r",
                    len(expected),
                    len(actual),
                    actual[:120],
                )
                return True
        return False

    def _activate_group_for_send(
        self,
        session: _ListenSession,
        *,
        send_id: str = "",
        send_attempt: int = 0,
    ) -> bool:
        """确保发送前目标群处于前台。

        发送链路中有三个容易误判的地方：
        1. _fetch_quote_from_chat 可能已经为了读取引用/图片点击过目标群。
           如果这里再点击左侧当前会话，微信 4.x 会把右侧聊天区域置为空白。
           因此先检查 _send_state 中的 recent_target_selected，命中则直接复用。
        2. 不读取左侧 ListItem.IsSelected。该 AX 属性在 macOS 微信上会偶发
           阻塞或返回不稳定结果，不能作为发送热路径的判断依据。
        3. 点击目标群后不在本阶段扫描消息列表/输入框。聊天区 AX 查询也可能
           卡住；本阶段只负责切群，输入框定位交给 _send_text_via_chat_input
           并通过 input_* 日志观察。
        """
        last_reason = "no_window"
        for attempt in range(1, GROUP_SWITCH_ATTEMPT_LIMIT + 1):
            attempt_started_at = time.time()
            self._log_send_stage(
                "activate_start",
                group=session.group,
                send_id=send_id,
                attempt=send_attempt,
                switch_attempt=attempt,
            )
            if self._stop_event.is_set():
                return False
            if attempt > 1:
                self._recover_send_surface(
                    session,
                    reason=last_reason,
                    attempt=attempt,
                )

            logger.info(
                "发送前切群开始: group=%s attempt=%s/%s",
                session.group,
                attempt,
                GROUP_SWITCH_ATTEMPT_LIMIT,
            )
            detached_root = _get_detached_group_root(session.group)
            detached_elapsed_ms = int((time.time() - attempt_started_at) * 1000)
            logger.info(
                "发送前独立窗口检查: group=%s attempt=%s found=%s elapsed=%.3fs",
                session.group,
                attempt,
                bool(detached_root),
                time.time() - attempt_started_at,
            )
            self._log_send_stage(
                "activate_detached_checked",
                group=session.group,
                send_id=send_id,
                attempt=send_attempt,
                success=bool(detached_root),
                elapsed_ms=detached_elapsed_ms,
                switch_attempt=attempt,
            )
            if detached_root:
                detached_msg_list = _find_message_list(detached_root)
                detached_edit = self._find_chat_input(detached_root)
                if detached_msg_list and detached_edit:
                    session.root = detached_root
                    session.msg_list = detached_msg_list
                    self._set_send_state(
                        group=session.group, group_at=time.time(), detached=True
                    )
                    logger.warning("使用目标群独立聊天窗口发送: %s", session.group)
                    log_error_audit(
                        "detached_window_used_for_send",
                        {"group": session.group, "attempt": attempt},
                    )
                    self._log_send_stage(
                        "activate_done",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=True,
                        reason="detached_window_ready",
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                    )
                    return True

            root_started_at = time.time()
            root = self._get_main_root()
            root_elapsed_ms = int((time.time() - root_started_at) * 1000)
            logger.info(
                "发送前主窗口检查: group=%s attempt=%s root=%s elapsed=%.3fs total=%.3fs",
                session.group,
                attempt,
                bool(root),
                time.time() - root_started_at,
                time.time() - attempt_started_at,
            )
            self._log_send_stage(
                "activate_main_root_checked",
                group=session.group,
                send_id=send_id,
                attempt=send_attempt,
                success=bool(root),
                reason="" if root else "main_root_missing",
                elapsed_ms=root_elapsed_ms,
                total_ms=int((time.time() - attempt_started_at) * 1000),
                switch_attempt=attempt,
            )
            if not root:
                last_reason = "main_root_missing"
                continue

            session.root = root
            try:
                root.SendKeys("{Esc}")
            except Exception:
                pass

            try:
                current_group, current_group_at, current_detached, _ = self._get_send_state()
                # 优先复用“读取消息阶段刚刚打开过目标群”的状态。这是防止
                # 二次点击当前会话导致右侧空白的核心保护。
                recent_target_selected = (
                    current_group == session.group
                    and not current_detached
                    and time.time() - current_group_at <= 90.0
                )
                self._log_send_stage(
                    "activate_state_checked",
                    group=session.group,
                    send_id=send_id,
                    attempt=send_attempt,
                    success=recent_target_selected,
                    reason="recent_target_selected" if recent_target_selected else "not_recent_target",
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    switch_attempt=attempt,
                    current_group=current_group or "",
                    state_age_ms=int((time.time() - current_group_at) * 1000) if current_group_at else None,
                )
                if recent_target_selected:
                    # 引用/图片读取阶段可能刚刚点击过目标群。macOS 微信 4.x
                    # 对当前左侧会话再次点击会让右侧聊天区进入空白态，所以这里
                    # 直接复用最近一次明确切到目标群的状态。
                    session.root = root
                    logger.info(
                        "发送前复用最近已打开目标群，跳过左侧点击: group=%s attempt=%s age=%.3fs",
                        session.group,
                        attempt,
                        time.time() - current_group_at,
                    )
                    self._log_send_stage(
                        "activate_done",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=True,
                        reason="recent_target_selected",
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                    )
                    return True

                item_lookup_started_at = time.time()
                current_selected_item = _find_session_item(root, session.group)
                self._log_send_stage(
                    "activate_session_item_checked",
                    group=session.group,
                    send_id=send_id,
                    attempt=send_attempt,
                    success=bool(current_selected_item),
                    elapsed_ms=int((time.time() - item_lookup_started_at) * 1000),
                    switch_attempt=attempt,
                )
                title_matches = _current_chat_title_matches(root, session.group)
                self._log_send_stage(
                    "activate_title_checked",
                    group=session.group,
                    send_id=send_id,
                    attempt=send_attempt,
                    success=title_matches,
                    elapsed_ms=int((time.time() - item_lookup_started_at) * 1000),
                    switch_attempt=attempt,
                )

                # 只有 recent_target_selected 和右侧标题都没命中时，才准备点击
                # 左侧会话。优先使用本轮可见列表中找到的控件，缓存只作为找不到
                # 可见项时的回退；缓存不能证明当前会话已选中。
                item = current_selected_item
                item_from_cache = False
                if not item and (
                    session.cached_session_item
                    and time.time() - session.cached_session_item_at <= _SESSION_ITEM_CACHE_TTL
                ):
                    if self._validate_control_cache(session.cached_session_item):
                        item = session.cached_session_item
                        item_from_cache = True
                if not item:
                    item = _find_session_item(root, session.group)
                    if item:
                        session.cached_session_item = item
                        session.cached_session_item_at = time.time()
                if not item:
                    last_reason = "session_item_missing"
                    logger.warning(
                        "切群尝试 %s/%s 未找到左侧会话: %s",
                        attempt,
                        GROUP_SWITCH_ATTEMPT_LIMIT,
                        session.group,
                    )
                    self._log_send_stage(
                        "activate_session_item_missing",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=False,
                        reason=last_reason,
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                    )
                    continue

                # 发送热路径不读取 ListItem.IsSelected。macOS 微信 AX 在该属性上
                # 偶发卡住；右侧标题才是是否已打开目标群的强判断。标题命中时
                # 必须跳过左侧点击，否则会触发“当前会话二次点击 -> 聊天区空白”。
                selected_before = title_matches

                if selected_before:
                    logger.info(
                        "发送前目标群已打开，跳过左侧点击: group=%s attempt=%s title_match=%s",
                        session.group,
                        attempt,
                        title_matches,
                    )
                    self._log_send_stage(
                        "activate_click_skipped",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=True,
                        reason="title_matches",
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                    )
                else:
                    self._log_send_stage(
                        "activate_click_start",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=None,
                        reason="title_not_matched",
                        elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                        item_from_cache=item_from_cache,
                    )
                    click_started_at = time.time()
                    if not _click_session_item(item):
                        last_reason = "session_item_click_failed"
                        logger.warning(
                            "切群尝试 %s/%s 点击会话失败: %s",
                            attempt,
                            GROUP_SWITCH_ATTEMPT_LIMIT,
                            session.group,
                        )
                        self._log_send_stage(
                            "activate_click_done",
                            group=session.group,
                            send_id=send_id,
                            attempt=send_attempt,
                            success=False,
                            reason=last_reason,
                            elapsed_ms=int((time.time() - click_started_at) * 1000),
                            total_ms=int((time.time() - attempt_started_at) * 1000),
                            switch_attempt=attempt,
                            item_from_cache=item_from_cache,
                        )
                        continue
                    self._log_send_stage(
                        "activate_click_done",
                        group=session.group,
                        send_id=send_id,
                        attempt=send_attempt,
                        success=True,
                        reason="clicked",
                        elapsed_ms=int((time.time() - click_started_at) * 1000),
                        total_ms=int((time.time() - attempt_started_at) * 1000),
                        switch_attempt=attempt,
                        item_from_cache=item_from_cache,
                    )
                    logger.info("发送前已点击目标群: group=%s attempt=%s", session.group, attempt)
                    self._set_send_state(group=session.group, group_at=time.time(), detached=False)
                    session.cached_session_item = item
                    session.cached_session_item_at = time.time()

                # 发送前切群阶段只负责把目标会话带到前台，不再扫描消息列表/输入框。
                # macOS 微信 AX 在聊天区控件树查询上会偶发卡住；输入框定位交给
                # _send_text_via_chat_input，并由 input_* 阶段日志记录成败。这里返回
                # True 表示“已完成切群动作或已确认无需切群”，不表示消息已经发送。
                settle_seconds = 0.12 if selected_before else GROUP_SWITCH_FALLBACK_ACCEPT_SECONDS
                if self._stop_event.wait(settle_seconds):
                    return False
                try:
                    session.root = self._get_main_root() or root
                except Exception:
                    session.root = root
                self._set_send_state(group=session.group, group_at=time.time(), detached=False)
                logger.info(
                    "发送前目标群切换完成，进入输入阶段: group=%s attempt=%s selected_before=%s",
                    session.group,
                    attempt,
                    selected_before,
                )
                self._log_send_stage(
                    "activate_done",
                    group=session.group,
                    send_id=send_id,
                    attempt=send_attempt,
                    success=True,
                    reason="target_selected_assumed_ready",
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    switch_attempt=attempt,
                    selected_before=selected_before,
                )
                return True
            except Exception as exc:
                last_reason = f"attempt_failed:{exc}"
                logger.warning(
                    "切群尝试 %s/%s 异常: %s: %s",
                    attempt,
                    GROUP_SWITCH_ATTEMPT_LIMIT,
                    session.group,
                    exc,
                )
                self._log_send_stage(
                    "activate_attempt_failed",
                    group=session.group,
                    send_id=send_id,
                    attempt=send_attempt,
                    success=False,
                    reason=last_reason,
                    elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                    switch_attempt=attempt,
                    error=str(exc),
                )
                continue

            logger.warning(
                "发送前切群尝试未就绪: group=%s attempt=%s reason=%s elapsed=%.3fs",
                session.group,
                attempt,
                last_reason,
                time.time() - attempt_started_at,
            )
            self._log_send_stage(
                "activate_attempt_failed",
                group=session.group,
                send_id=send_id,
                attempt=send_attempt,
                success=False,
                reason=last_reason,
                elapsed_ms=int((time.time() - attempt_started_at) * 1000),
                switch_attempt=attempt,
            )

        logger.error(f"发送前切换目标群最终失败: {session.group}, reason={last_reason}")
        log_error_audit(
            "activate_group_failed",
            {"group": session.group, "reason": last_reason},
        )
        self._log_send_stage(
            "activate_done",
            group=session.group,
            send_id=send_id,
            attempt=send_attempt,
            success=False,
            reason=last_reason,
        )
        return False

    @staticmethod
    def _find_chat_input(root):
        possible_ids = ["chat_input_field", "input_field", "msg_input", "edit_input"]
        for auto_id in possible_ids:
            try:
                edit = root.EditControl(AutomationId=auto_id)
                if edit.Exists(maxSearchSeconds=QUICK_EXISTS_TIMEOUT):
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

    @staticmethod
    def _find_send_button(root):
        if not root:
            return None
        candidates = []
        try:
            root_rect = root.BoundingRectangle
            for control, _depth in platform.automation.walk_control(root, include_top=True, max_depth=8):
                control_type = _safe_text(control, "ControlTypeName")
                name = _safe_text(control, "Name").strip()
                auto_id = _safe_text(control, "AutomationId").strip()
                if control_type != "ButtonControl" and "Button" not in control_type:
                    continue
                if name not in {"发送", "Send"} and auto_id not in {"send_button", "btn_send"}:
                    continue
                rect = control.BoundingRectangle
                if rect.top < root_rect.top + root_rect.height * 0.55:
                    continue
                if rect.left < root_rect.left + root_rect.width * 0.45:
                    continue
                area = max(1, (rect.right - rect.left) * (rect.bottom - rect.top))
                candidates.append((rect.top, rect.left, area, control))
        except Exception:
            return None

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return candidates[0][3]
