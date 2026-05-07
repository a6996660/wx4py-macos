# -*- coding: utf-8 -*-
"""微信图片气泡本地提取器。

微信图片消息通常没有稳定暴露文件名，无法走文件卡片的“文件名 -> 下载目录”
解析链路。这里通过定位最近图片气泡、复制到系统剪贴板，再从 NSPasteboard
读取图片数据并写入临时文件，供 OpenClaw 作为普通本地文件读取。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ...platform import platform
from ...utils.logger import get_logger
from .listener import (
    MACOS_MESSAGE_AUTOMATION_ID,
    MESSAGE_CLASSES,
    _click_session_item,
    _current_chat_title_matches,
    _find_session_item,
    _find_message_list,
    _safe_children,
    _safe_text,
)

logger = get_logger(__name__)

_IMAGE_MARKERS = ("[图片]", "[Image]", "[照片]", "[Photo]", "图片", "Image", "Photo")


class WeChatImageExtractor:
    """从当前微信群最近图片消息中提取图片文件。"""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        hwnd_provider: Optional[Callable[[], Optional[int]]] = None,
    ):
        self.output_dir = Path(output_dir or Path.home() / ".wx4py_files" / "images")
        self.hwnd_provider = hwnd_provider

    @staticmethod
    def _log_action(step: str, ok: bool, started_at: float, **details) -> None:
        """统一记录图片提取的每个 UI 动作，便于从日志还原完整链路。"""
        detail_text = " ".join(f"{key}={value!r}" for key, value in details.items())
        logger.info(
            "图片提取动作: step=%s ok=%s elapsed=%.3fs %s",
            step,
            ok,
            time.time() - started_at,
            detail_text,
        )

    def extract_latest(
        self,
        group: str,
        hwnd: Optional[int] = None,
        *,
        current_content: str = "",
        sender_nickname: str = "",
    ) -> Optional[str]:
        """提取当前消息关联的最近图片，返回本地图片路径。"""
        if not group:
            return None
        started_at = time.time()
        logger.info(
            "图片提取开始: group=%s hwnd=%s current_content=%r sender=%r",
            group,
            hwnd,
            current_content,
            sender_nickname,
        )
        self._log_action(
            "start",
            True,
            started_at,
            group=group,
            hwnd=hwnd,
            current_content=current_content,
            sender=sender_nickname,
            output_dir=str(self.output_dir),
        )
        root, resolved_hwnd = self._get_root(hwnd)
        self._log_action(
            "get_root",
            root is not None,
            started_at,
            requested_hwnd=hwnd,
            resolved_hwnd=resolved_hwnd,
        )
        if root is None:
            logger.warning("图片提取失败: 无法打开群聊 group=%s", group)
            return None

        msg_list_started = time.time()
        msg_list = _find_message_list(root)
        current_match = self._message_list_contains(msg_list, current_content) if msg_list else False
        self._log_action(
            "find_message_list_current",
            msg_list is not None,
            msg_list_started,
            group=group,
            current_match=current_match,
            rect=self._rect_summary(msg_list) if msg_list else None,
        )
        logger.info(
            "图片提取: 当前窗口消息列表检测 group=%s msg_list=%s current_match=%s",
            group,
            bool(msg_list),
            current_match,
        )

        # listener 已经在同一窗口里成功读取到了当前消息的引用内容。这里优先复用
        # 当前聊天区，避免再次点击左侧会话引发 macOS AX 卡顿或切错窗口。
        if msg_list is None:
            open_started = time.time()
            root = self._open_group(group, resolved_hwnd)
            self._log_action(
                "open_group_fallback",
                root is not None,
                open_started,
                group=group,
                hwnd=resolved_hwnd,
            )
            if root is None:
                logger.warning("图片提取失败: 无法打开群聊 group=%s", group)
                return None
            msg_list_started = time.time()
            msg_list = _find_message_list(root)
            self._log_action(
                "find_message_list_after_open",
                msg_list is not None,
                msg_list_started,
                group=group,
                rect=self._rect_summary(msg_list) if msg_list else None,
            )

        if msg_list is None:
            logger.warning("图片提取失败: 未找到消息列表 group=%s", group)
            return None

        scan_started = time.time()
        bubble = self._find_image_bubble(
            msg_list,
            current_content=current_content,
            sender_nickname=sender_nickname,
        )
        self._log_action(
            "find_image_bubble",
            bubble is not None,
            scan_started,
            group=group,
            rect=self._rect_summary(bubble) if bubble else None,
            name=_safe_text(bubble, "Name")[:160] if bubble else None,
        )
        if bubble is None:
            logger.warning(
                "图片提取未命中气泡，尝试截取消息列表底部: group=%s elapsed=%.2fs",
                group,
                time.time() - started_at,
            )
            capture_started = time.time()
            path = self._capture_message_list_tail(group, msg_list)
            self._log_action(
                "capture_message_list_tail",
                bool(path),
                capture_started,
                group=group,
                path=path,
            )
            if path:
                logger.info(
                    "图片已从消息列表底部截图保存: group=%s path=%s elapsed=%.2fs",
                    group,
                    path,
                    time.time() - started_at,
                )
                return path
            logger.warning(
                "图片提取失败: 未找到图片气泡且消息列表截图失败 group=%s elapsed=%.2fs",
                group,
                time.time() - started_at,
            )
            return None

        copy_started = time.time()
        copied = self._copy_bubble_image(bubble)
        self._log_action(
            "copy_bubble_image",
            copied,
            copy_started,
            group=group,
            rect=self._rect_summary(bubble),
            name=_safe_text(bubble, "Name")[:160],
        )
        if not copied:
            logger.warning("图片提取失败: 复制图片气泡失败 group=%s", group)
        else:
            save_started = time.time()
            path = self._save_clipboard_image(group)
            self._log_action(
                "save_clipboard_image",
                bool(path),
                save_started,
                group=group,
                path=path,
            )
            if path:
                logger.info(
                    "图片已从剪贴板保存: group=%s path=%s elapsed=%.2fs",
                    group,
                    path,
                    time.time() - started_at,
                )
                return path

        preview_started = time.time()
        path = self._capture_preview_image(group, bubble)
        self._log_action(
            "capture_preview_image",
            bool(path),
            preview_started,
            group=group,
            path=path,
        )
        if path:
            logger.info(
                "图片已从预览窗口截图保存: group=%s path=%s elapsed=%.2fs",
                group,
                path,
                time.time() - started_at,
            )
            return path

        capture_started = time.time()
        path = self._capture_bubble_image(group, bubble)
        self._log_action(
            "capture_bubble_image",
            bool(path),
            capture_started,
            group=group,
            path=path,
        )
        if path:
            logger.info(
                "图片已从气泡截图保存: group=%s path=%s elapsed=%.2fs",
                group,
                path,
                time.time() - started_at,
            )
        return path

    def _get_root(self, hwnd: Optional[int]):
        try:
            started_at = time.time()
            if hwnd is None:
                hwnd = self._resolve_hwnd()
            logger.info("图片提取: control_from_handle 开始 hwnd=%s", hwnd)
            root = platform.automation.control_from_handle(hwnd)
            logger.info(
                "图片提取: 获取窗口根控件 hwnd=%s root=%s elapsed=%.3fs",
                hwnd,
                bool(root),
                time.time() - started_at,
            )
            return root, hwnd
        except Exception as exc:
            logger.warning("图片提取: 获取窗口根控件异常 hwnd=%s error=%s", hwnd, exc)
            return None, hwnd

    def _open_group(self, group: str, hwnd: Optional[int]):
        opened_at = time.time()
        root, hwnd = self._get_root(hwnd)
        if root is None:
            return None

        try:
            title_started = time.time()
            if _current_chat_title_matches(root, group):
                self._log_action(
                    "check_current_title",
                    True,
                    title_started,
                    group=group,
                )
                logger.info("图片提取: 当前聊天已是目标群 group=%s", group)
                return root
            self._log_action(
                "check_current_title",
                False,
                title_started,
                group=group,
            )
            find_started = time.time()
            item = _find_session_item(root, group)
            self._log_action(
                "find_session_item",
                item is not None,
                find_started,
                group=group,
                item_name=_safe_text(item, "Name")[:160] if item else None,
                item_rect=self._rect_summary(item) if item else None,
            )
            if item is not None:
                logger.info(
                    "图片提取: 当前非目标群，点击左侧会话 group=%s item_name=%r",
                    group,
                    _safe_text(item, "Name")[:120],
                )
                click_started = time.time()
                _click_session_item(item)
                self._log_action(
                    "click_session_item",
                    True,
                    click_started,
                    group=group,
                    item_rect=self._rect_summary(item),
                )
                wait_started = time.time()
                root = self._wait_for_group_message_list(hwnd, group) or platform.automation.control_from_handle(hwnd)
                self._log_action(
                    "wait_after_click_session",
                    root is not None,
                    wait_started,
                    group=group,
                    hwnd=hwnd,
                )
            else:
                logger.warning("图片提取: 未找到左侧会话项 group=%s", group)
        except Exception as exc:
            logger.warning(
                "图片提取: 打开群聊失败 group=%s elapsed=%.3fs error=%s",
                group,
                time.time() - opened_at,
                exc,
            )
        return root

    def _resolve_hwnd(self) -> Optional[int]:
        started_at = time.time()
        if self.hwnd_provider is not None:
            try:
                hwnd = self.hwnd_provider()
                self._log_action(
                    "resolve_hwnd_provider",
                    bool(hwnd),
                    started_at,
                    hwnd=hwnd,
                )
                if hwnd:
                    logger.info("图片提取: 使用注入窗口句柄 hwnd=%s", hwnd)
                    return int(hwnd)
            except Exception as exc:
                logger.warning("图片提取: hwnd_provider 失败: %s", exc)
                self._log_action(
                    "resolve_hwnd_provider",
                    False,
                    started_at,
                    error=str(exc),
                )
        fallback_started = time.time()
        hwnd = platform.window_manager.find_wechat_window()
        self._log_action(
            "resolve_hwnd_fallback",
            bool(hwnd),
            fallback_started,
            hwnd=hwnd,
        )
        logger.info("图片提取: fallback 查找微信窗口 hwnd=%s", hwnd)
        return hwnd

    def _wait_for_group_message_list(self, hwnd: int, group: str, timeout: float = 3.0):
        deadline = time.time() + timeout
        attempt = 0
        last_root = None
        while time.time() < deadline:
            attempt += 1
            try:
                attempt_started = time.time()
                root = platform.automation.control_from_handle(hwnd)
                last_root = root
                msg_list = _find_message_list(root)
                self._log_action(
                    "wait_group_message_list_attempt",
                    msg_list is not None,
                    attempt_started,
                    group=group,
                    hwnd=hwnd,
                    attempt=attempt,
                    msg_list_rect=self._rect_summary(msg_list) if msg_list else None,
                )
                logger.info(
                    "图片提取: 等待切群 attempt=%s msg_list=%s",
                    attempt,
                    bool(msg_list),
                )
                if msg_list:
                    return root
            except Exception as exc:
                logger.info("图片提取: 等待切群异常 attempt=%s error=%s", attempt, exc)
            time.sleep(0.25)
        return last_root

    def _message_list_contains(self, msg_list, current_content: str) -> bool:
        """判断当前消息列表是否包含本次触发消息，作为复用当前聊天区的依据。"""
        if not msg_list or not current_content:
            return False
        normalized_content = " ".join(str(current_content or "").split())
        if not normalized_content:
            return False
        try:
            children = _safe_children(msg_list)
            logger.info(
                "图片提取: 当前消息列表匹配开始 current_content=%r children=%s",
                current_content,
                len(children),
            )
            for child in reversed(children[-12:]):
                name = " ".join(_safe_text(child, "Name").split())
                logger.info(
                    "图片提取: 当前消息列表匹配检查 rect=%s name=%r",
                    self._rect_summary(child),
                    name[:180],
                )
                if normalized_content in name:
                    logger.info(
                        "图片提取: 当前消息列表命中触发消息 name=%r",
                        name[:180],
                    )
                    return True
        except Exception as exc:
            logger.info("图片提取: 当前消息列表匹配异常: %s", exc)
        return False

    def _find_image_bubble(
        self,
        msg_list,
        *,
        current_content: str = "",
        sender_nickname: str = "",
    ) -> Optional[Any]:
        children = _safe_children(msg_list)
        preferred = []
        fallback = []
        normalized_content = " ".join(str(current_content or "").split())
        sender = (sender_nickname or "").strip()
        logger.info(
            "图片气泡扫描: children=%d normalized_content=%r sender=%r",
            len(children),
            normalized_content,
            sender,
        )

        recent_children = list(reversed(children[-24:]))
        for index, child in enumerate(recent_children, 1):
            cls = _safe_text(child, "ClassName")
            auto_id = _safe_text(child, "AutomationId")
            child_rect = self._rect_summary(child)
            if cls not in MESSAGE_CLASSES and auto_id != MACOS_MESSAGE_AUTOMATION_ID:
                if index <= 12:
                    logger.info(
                        "图片气泡候选跳过: idx=%s class=%r auto_id=%r rect=%s name=%r",
                        index,
                        cls,
                        auto_id,
                        child_rect,
                        _safe_text(child, "Name")[:160],
                    )
                continue
            name = _safe_text(child, "Name")
            marker_hit = any(marker in name for marker in _IMAGE_MARKERS)
            normalized_name = " ".join(name.split())
            logger.info(
                "图片气泡候选: idx=%s marker=%s class=%r auto_id=%r rect=%s name=%r",
                index,
                marker_hit,
                cls,
                auto_id,
                child_rect,
                name[:220],
            )

            if marker_hit:
                target = child
            else:
                find_child_started = time.time()
                image_child = self._find_image_child(child)
                self._log_action(
                    "find_image_child",
                    image_child is not None,
                    find_child_started,
                    idx=index,
                    parent_rect=child_rect,
                    child_rect=self._rect_summary(image_child) if image_child else None,
                    child_name=_safe_text(image_child, "Name")[:120] if image_child else None,
                )
                target = image_child
            if not marker_hit and target is None:
                continue

            if normalized_content and normalized_content in normalized_name:
                logger.info("图片气泡匹配当前消息: name=%r", name[:120])
                preferred.append(target)
                continue
            if sender and sender in name:
                logger.info("图片气泡匹配发送者: name=%r", name[:120])
                preferred.append(target)
                continue
            logger.debug("图片气泡候选: name=%r", name[:120])
            fallback.append(target)

        return (preferred or fallback or [None])[0]

    @staticmethod
    def _find_image_child(parent):
        for control_type in ("Image", "Button", "Group"):
            try:
                started_at = time.time()
                controls = platform.automation.find_all_controls(
                    parent, control_type=control_type, search_depth=6
                )
                logger.info(
                    "图片子控件扫描: control_type=%s count=%s elapsed=%.3fs parent_name=%r",
                    control_type,
                    len(controls),
                    time.time() - started_at,
                    _safe_text(parent, "Name")[:120],
                )
            except Exception:
                controls = []
            for ctrl in controls:
                name = _safe_text(ctrl, "Name")
                logger.info(
                    "图片子控件候选: control_type=%s rect=%s name=%r",
                    control_type,
                    WeChatImageExtractor._rect_summary(ctrl),
                    name[:120],
                )
                if any(marker in name for marker in _IMAGE_MARKERS):
                    return ctrl
        return None

    @staticmethod
    def _rect_summary(control) -> str:
        try:
            rect = platform.automation.get_bounding_rectangle(control)
            if not rect:
                return "None"
            return f"{int(rect.left)},{int(rect.top)},{int(rect.right)},{int(rect.bottom)}"
        except Exception as exc:
            return f"error:{exc}"

    def _copy_bubble_image(self, bubble) -> bool:
        try:
            rect = platform.automation.get_bounding_rectangle(bubble)
            if rect:
                logger.info(
                    "图片气泡准备复制: rect=%s name=%r",
                    self._rect_summary(bubble),
                    _safe_text(bubble, "Name")[:160],
                )
                x = (rect.left + rect.right) // 2
                y = (rect.top + rect.bottom) // 2
                logger.info(
                    "图片气泡点击复制: x=%s y=%s rect=%s",
                    x,
                    y,
                    self._rect_summary(bubble),
                )
                platform.input.mouse_click(x, y)
                time.sleep(0.12)
        except Exception as exc:
            logger.warning("图片气泡点击复制异常: %s", exc)

        try:
            logger.info("图片气泡复制快捷键: combo=Command+C")
            platform.input.send_combo(platform.input.VK_CONTROL, platform.input.VK_C, settle_time=0.4)
            self._log_clipboard_types()
            return True
        except Exception as exc:
            logger.debug("图片气泡复制快捷键失败: %s", exc)
        return False

    @staticmethod
    def _log_clipboard_types() -> None:
        try:
            from ...platform import _macos
        except Exception:
            _macos = None
        if not getattr(_macos, "_PYOBJC_AVAILABLE", False):
            logger.info("剪贴板诊断: pyobjc unavailable")
            return
        try:
            pasteboard = _macos.Cocoa.NSPasteboard.generalPasteboard()
            types = [str(t) for t in list(pasteboard.types() or [])]
            logger.info("剪贴板诊断: types=%s", types)
        except Exception as exc:
            logger.info("剪贴板诊断失败: %s", exc)

    def _save_clipboard_image(self, group: str) -> Optional[str]:
        try:
            from ...platform import _macos
        except Exception:
            _macos = None

        if not getattr(_macos, "_PYOBJC_AVAILABLE", False):
            logger.info("剪贴板图片保存跳过: pyobjc unavailable")
            return None

        try:
            pasteboard = _macos.Cocoa.NSPasteboard.generalPasteboard()
            types = [str(t) for t in list(pasteboard.types() or [])]
            logger.info("剪贴板图片保存开始: group=%s types=%s", group, types)
            image = _macos.Cocoa.NSImage.alloc().initWithPasteboard_(pasteboard)
            if image is None:
                logger.info("剪贴板图片保存跳过: NSImage 为空")
                return None
            tiff = image.TIFFRepresentation()
            if tiff is None:
                logger.info("剪贴板图片保存跳过: TIFFRepresentation 为空")
                return None
            bitmap = _macos.Cocoa.NSBitmapImageRep.imageRepWithData_(tiff)
            if bitmap is None:
                logger.info("剪贴板图片保存跳过: NSBitmapImageRep 为空")
                return None
            data = bitmap.representationUsingType_properties_(
                _macos.Cocoa.NSBitmapImageFileTypePNG,
                {},
            )
            if data is None:
                logger.info("剪贴板图片保存跳过: PNG data 为空")
                return None

            self.output_dir.mkdir(parents=True, exist_ok=True)
            safe_group = "".join(ch if ch.isalnum() else "_" for ch in group)[:32] or "group"
            path = self.output_dir / f"{safe_group}_{int(time.time() * 1000)}.png"
            if not data.writeToFile_atomically_(str(path), True):
                logger.info("剪贴板图片保存失败: writeToFile returned false path=%s", path)
                return None
            logger.info(
                "剪贴板图片保存完成: path=%s exists=%s size=%s",
                path,
                os.path.isfile(path),
                os.path.getsize(path) if os.path.isfile(path) else 0,
            )
            return str(path) if os.path.isfile(path) else None
        except Exception as exc:
            logger.warning("保存剪贴板图片失败: %s", exc)
            return None

    def _capture_preview_image(self, group: str, bubble) -> Optional[str]:
        """优先打开微信图片预览窗口并截图，避免只截到引用缩略图。"""
        snapshot_started = time.time()
        initial_windows = self._wechat_window_snapshot()
        self._log_action(
            "preview_snapshot_before",
            True,
            snapshot_started,
            group=group,
            windows=list(initial_windows.keys()),
        )

        if not self._open_image_preview_from_bubble(bubble):
            logger.warning("图片预览打开失败: 无法点击图片缩略图 group=%s", group)
            return None

        window_started = time.time()
        preview = self._wait_for_preview_window(initial_windows, timeout=3.0)
        self._log_action(
            "wait_preview_window",
            preview is not None,
            window_started,
            group=group,
            preview=preview,
        )
        if preview is None:
            return None

        _wid, title, x, y, width, height = preview
        logger.info(
            "图片预览窗口准备截图: group=%s title=%r crop=%s,%s,%s,%s",
            group,
            title,
            x,
            y,
            width,
            height,
        )
        path = self._screencapture(group, x, y, width, height, suffix="preview")
        if path:
            self._close_preview_window(preview)
        return path

    def _open_image_preview_from_bubble(self, bubble) -> bool:
        try:
            rect = platform.automation.get_bounding_rectangle(bubble)
        except Exception as exc:
            logger.warning("图片预览打开失败: 无法获取气泡 rect error=%s", exc)
            return False
        if not rect:
            return False

        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        points = [
            # 引用缩略图通常在气泡底部偏左。
            (
                int(rect.left + min(110, max(50, width * 0.18))),
                int(rect.bottom - min(42, max(24, height * 0.28))),
                "thumb_left",
            ),
            (
                int(rect.left + min(160, max(80, width * 0.28))),
                int(rect.bottom - min(42, max(24, height * 0.28))),
                "thumb_mid",
            ),
            (
                int((rect.left + rect.right) // 2),
                int(rect.bottom - min(42, max(24, height * 0.28))),
                "bottom_center",
            ),
        ]
        for x, y, label in points:
            try:
                logger.info(
                    "图片预览点击: label=%s x=%s y=%s bubble_rect=%s name=%r",
                    label,
                    x,
                    y,
                    self._rect_summary(bubble),
                    _safe_text(bubble, "Name")[:160],
                )
                platform.input.mouse_click(x, y)
                time.sleep(0.08)
                platform.input.mouse_click(x, y)
                time.sleep(0.45)
                return True
            except Exception as exc:
                logger.warning("图片预览点击异常: label=%s error=%s", label, exc)
        return False

    @staticmethod
    def _wechat_window_snapshot() -> dict:
        try:
            from ...platform import _macos
        except Exception:
            return {}
        try:
            app_ref = _macos._get_wechat_app_ref()
            if not app_ref:
                return {}
            windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
            result = {}
            for window in list(windows or []):
                try:
                    wid = _macos._register_window(window)
                    title = _macos._ax_get_title(window)
                    pos = _macos._ax_get_position(window)
                    size = _macos._ax_get_size(window)
                    result[wid] = (title, pos, size, window)
                except Exception:
                    continue
            return result
        except Exception as exc:
            logger.info("微信窗口快照失败: %s", exc)
            return {}

    def _wait_for_preview_window(self, initial_windows: dict, timeout: float):
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            windows = self._wechat_window_snapshot()
            candidates = []
            for wid, (title, pos, size, _window) in windows.items():
                if not pos or not size:
                    continue
                width = int(size[0])
                height = int(size[1])
                x = int(pos[0])
                y = int(pos[1])
                is_new = wid not in initial_windows
                area = width * height
                logger.info(
                    "图片预览窗口扫描: attempt=%s wid=%s is_new=%s title=%r pos=%r size=%r area=%s",
                    attempt,
                    wid,
                    is_new,
                    title,
                    pos,
                    size,
                    area,
                )
                if width < 180 or height < 160:
                    continue
                title_text = str(title or "")
                title_hit = any(
                    token in title_text
                    for token in ("图片", "照片", "Image", "Photo", "预览")
                )
                if is_new or title_hit:
                    candidates.append((area, wid, title_text, x, y, width, height))
            if candidates:
                candidates.sort(reverse=True)
                _area, wid, title, x, y, width, height = candidates[0]
                # 去掉窗口标题栏边缘，减少把黑框和工具栏传给 OpenClaw。
                crop_x = max(0, x)
                crop_y = max(0, y)
                crop_w = max(1, width)
                crop_h = max(1, height)
                return wid, title, crop_x, crop_y, crop_w, crop_h
            time.sleep(0.25)
        return None

    @staticmethod
    def _close_preview_window(preview) -> None:
        started_at = time.time()

        def log_close(method: str, ok: bool, **details) -> None:
            detail_text = " ".join(f"{key}={value!r}" for key, value in details.items())
            logger.info(
                "图片预览关闭动作: method=%s ok=%s elapsed=%.3fs %s",
                method,
                ok,
                time.time() - started_at,
                detail_text,
            )

        try:
            from ...platform import _macos
        except Exception as exc:
            log_close("import_macos", False, error=str(exc))
            return

        try:
            wid = preview[0]
            expected_title = str(preview[1] or "") if len(preview) > 1 else ""
            windows = WeChatImageExtractor._wechat_window_snapshot()
            item = windows.get(wid)
            if item is None:
                # AX window ids can change after a screenshot/focus transition. Fall back
                # to the image-preview looking window so the close path is still observable.
                for candidate_wid, candidate in windows.items():
                    title, _pos, size, _window = candidate
                    title_text = str(title or "")
                    title_hit = expected_title and title_text == expected_title
                    preview_hit = any(
                        token in title_text
                        for token in ("图片", "照片", "Image", "Photo", "预览")
                    )
                    if title_hit or preview_hit:
                        wid = candidate_wid
                        item = candidate
                        break
            if not item:
                log_close(
                    "find_window",
                    False,
                    expected_wid=preview[0],
                    expected_title=expected_title,
                    windows=list(windows.keys()),
                )
                return
            title, pos, size, window = item
            log_close("find_window", True, wid=wid, title=title, pos=pos, size=size)

            raised = _macos._ax_perform_action(window, "AXRaise")
            log_close("AXRaise", raised, wid=wid, title=title)
            time.sleep(0.08)

            ax_closed = _macos._ax_perform_action(window, "AXClose")
            log_close("AXClose", ax_closed, wid=wid, title=title)
            if ax_closed:
                return

            close_button = WeChatImageExtractor._find_ax_close_button(window)
            log_close("find_AXCloseButton", close_button is not None, wid=wid, title=title)
            if close_button is not None:
                pressed = _macos._ax_perform_action(close_button, "AXPress")
                log_close("AXCloseButton", pressed, wid=wid, title=title)
                if pressed:
                    return

            if pos:
                # macOS traffic-light close button is near the top-left corner of the
                # window. Use a coordinate fallback because some WeChat preview windows
                # do not expose AXClose or a pressable close button.
                for offset_x, offset_y in ((18, 18), (22, 22), (14, 20)):
                    try:
                        click_x = int(pos[0] + offset_x)
                        click_y = int(pos[1] + offset_y)
                        platform.input.mouse_click(click_x, click_y)
                        time.sleep(0.25)
                        still_open = WeChatImageExtractor._preview_window_exists(wid, title)
                        log_close(
                            "mouse_close",
                            not still_open,
                            wid=wid,
                            title=title,
                            x=click_x,
                            y=click_y,
                        )
                        if not still_open:
                            return
                    except Exception as exc:
                        log_close(
                            "mouse_close",
                            False,
                            wid=wid,
                            title=title,
                            offset=(offset_x, offset_y),
                            error=str(exc),
                        )

            log_close("all_methods", False, wid=wid, title=title)
        except Exception as exc:
            log_close("exception", False, error=str(exc))

    @staticmethod
    def _find_ax_close_button(window):
        try:
            from ...platform import _macos
        except Exception:
            return None
        try:
            stack = list(_macos._ax_get_children(window))
            while stack:
                node = stack.pop()
                role = _macos._ax_get_role(node)
                subrole = str(_macos._ax_get_attr(node, "AXSubrole") or "")
                title = _macos._ax_get_title(node)
                description = _macos._ax_get_description(node)
                logger.info(
                    "图片预览关闭按钮候选: role=%r subrole=%r title=%r desc=%r",
                    role,
                    subrole,
                    title,
                    description,
                )
                if subrole == "AXCloseButton" or title in {"关闭", "Close"}:
                    return node
                stack.extend(_macos._ax_get_children(node))
        except Exception as exc:
            logger.info("图片预览关闭按钮扫描失败: %s", exc)
        return None

    @staticmethod
    def _preview_window_exists(wid: int, title: str) -> bool:
        windows = WeChatImageExtractor._wechat_window_snapshot()
        if wid in windows:
            return True
        title_text = str(title or "")
        if not title_text:
            return False
        for _candidate_wid, (candidate_title, _pos, _size, _window) in windows.items():
            if str(candidate_title or "") == title_text:
                return True
        return False

    def _capture_message_list_tail(self, group: str, msg_list) -> Optional[str]:
        """兜底：截取消息列表靠下区域为 PNG，便于确认 UIA 候选和 OpenClaw 输入。"""
        try:
            rect = platform.automation.get_bounding_rectangle(msg_list)
        except Exception:
            rect = None
        if not rect:
            logger.warning("消息列表截图失败: 无 rect group=%s", group)
            return None

        width = max(1, int(rect.right - rect.left))
        full_height = max(1, int(rect.bottom - rect.top))
        height = min(360, full_height)
        x = max(0, int(rect.left))
        y = max(0, int(rect.bottom - height))
        logger.info(
            "消息列表底部截图准备: group=%s rect=%s crop=%s,%s,%s,%s",
            group,
            self._rect_summary(msg_list),
            x,
            y,
            width,
            height,
        )
        return self._screencapture(group, x, y, width, height, suffix="msglist")

    def _capture_bubble_image(self, group: str, bubble) -> Optional[str]:
        """兜底：截取可见图片/引用气泡区域为 PNG。"""
        try:
            rect = platform.automation.get_bounding_rectangle(bubble)
        except Exception:
            rect = None
        if not rect:
            return None

        width = max(1, int(rect.right - rect.left))
        height = max(1, int(rect.bottom - rect.top))
        x = max(0, int(rect.left))
        y = max(0, int(rect.top))

        # 引用图片气泡通常上半部分是文字，下半部分是引用缩略图。
        # 如果气泡较高，优先裁底部区域，减少把用户指令文字混进图片。
        if height >= 90:
            y = int(rect.top + height * 0.38)
            height = max(40, int(rect.bottom - y))

        logger.info(
            "图片气泡截图准备: group=%s source_rect=%s crop=%s,%s,%s,%s name=%r",
            group,
            self._rect_summary(bubble),
            x,
            y,
            width,
            height,
            _safe_text(bubble, "Name")[:160],
        )
        return self._screencapture(group, x, y, width, height, suffix="capture")

    def _screencapture(
        self,
        group: str,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        suffix: str,
    ) -> Optional[str]:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            safe_group = "".join(ch if ch.isalnum() else "_" for ch in group)[:32] or "group"
            path = self.output_dir / f"{safe_group}_{int(time.time() * 1000)}_{suffix}.png"
            result = subprocess.run(
                [
                    "screencapture",
                    "-x",
                    "-R",
                    f"{x},{y},{width},{height}",
                    str(path),
                ],
                capture_output=True,
                timeout=5,
            )
            logger.info(
                "screencapture 结果: suffix=%s returncode=%s path=%s exists=%s size=%s stderr=%r",
                suffix,
                result.returncode,
                path,
                os.path.isfile(path),
                os.path.getsize(path) if os.path.isfile(path) else 0,
                result.stderr.decode(errors="replace")[:200],
            )
            if result.returncode == 0 and os.path.isfile(path):
                return str(path)
            logger.debug(
                "气泡截图失败: returncode=%s stderr=%s",
                result.returncode,
                result.stderr.decode(errors="replace")[:200],
            )
        except Exception as exc:
            logger.warning("气泡截图保存失败: %s", exc)
        return None
