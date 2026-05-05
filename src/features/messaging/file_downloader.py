# -*- coding: utf-8 -*-
"""微信文件引用主动下载触发器。

当消息引用了未下载的文件时，通过 UIA 自动化点击引用气泡、
弹出下载对话框并点击"接收文件"，将文件下载到本地 xwechat_files 目录，
再通过 file_monitor 获取实际路径。

交互链（macOS 微信 4.x）：
    1. 在聊天窗口中找到包含文件名的引用气泡
    2. 点击引用气泡 → 弹出独立窗口
    3. 在弹出窗口中点击"接收文件"按钮
    4. 文件下载到 ~/Library/Containers/.../xwechat_files/.../msg/file/YYYY-MM
    5. 轮询 file_monitor 确认文件出现并返回路径
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Tuple

from ...platform import platform
from ...utils.logger import get_logger

logger = get_logger(__name__)

# 弹出窗口轮询配置
_POPUP_SEARCH_INTERVAL = 0.3
_POPUP_SEARCH_TIMEOUT = 5.0
_RECEIVE_BUTTON_TIMEOUT = 3.0
_FILE_POLL_INTERVAL = 0.5
_FILE_POLL_TIMEOUT = 30.0
_BUBBLE_SEARCH_DEPTH = 12

# 接收按钮的多语言匹配
_RECEIVE_BUTTON_NAMES = {"接收文件", "Receive File", "接受", "Accept"}


def _safe_name(control) -> str:
    """安全获取控件 Name（AXTitle / AXDescription）。"""
    try:
        return str(control.Name or "")
    except Exception:
        return ""


def _safe_children(control) -> List[Any]:
    """安全获取控件子元素列表。"""
    try:
        return list(control.GetChildren())
    except Exception:
        return []


class WeChatFileDownloader:
    """微信文件引用主动下载触发器。

    Args:
        file_monitor: 可选的 WeChatDownloadMonitor 实例，用于轮询确认文件下载完成。
    """

    def __init__(self, file_monitor=None):
        self.file_monitor = file_monitor
        # 记录 _wait_for_popup 过程中检测到的弹窗标题，用于后续关闭窗口匹配
        self._popup_window_titles: List[str] = []

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def download(self, filename: str, hwnd: Optional[int] = None) -> Optional[str]:
        """主动触发文件下载并返回本地路径。

        流程：
            1. 若 file_monitor 已索引该文件，直接返回路径（短路优化）。
            2. 在聊天窗口中查找包含该文件名的引用气泡并点击。
            3. 等待弹出下载对话框。
            4. 点击"接收文件"按钮。
            5. 轮询 file_monitor 直到文件出现。

        Args:
            filename: 要下载的文件名（不含路径）。
            hwnd: 微信主窗口句柄，用于定位聊天窗口控件树。
                  若为 None，自动通过 platform.window_manager 获取当前微信窗口。

        Returns:
            下载完成后的本地绝对路径；若任何步骤失败返回 None。
        """
        if not filename:
            return None

        # 1. 短路：已下载则直接返回
        if self.file_monitor is not None:
            resolved = self.file_monitor.resolve(filename)
            if resolved:
                logger.debug("文件已存在，跳过主动下载: %s", filename)
                # 微信可能已自动打开预览窗口（即使文件早已存在），同样要关闭
                self._close_file_windows(os.path.basename(resolved))
                return resolved

        # 2. 获取聊天窗口根控件
        if hwnd is None:
            try:
                hwnd = platform.window_manager.find_wechat_window()
            except Exception:
                pass
        if hwnd is None:
            logger.warning("下载文件失败: 无法获取微信窗口句柄 filename=%s", filename)
            return None

        root = platform.automation.control_from_handle(hwnd)
        if root is None:
            logger.warning("下载文件失败: 无法获取窗口根控件 hwnd=%s", hwnd)
            return None

        # 3. 查找并点击引用气泡
        bubble = self._find_quote_bubble(root, filename)
        if bubble is None:
            logger.warning("下载文件失败: 未找到引用气泡 filename=%s", filename)
            return None

        logger.info("找到引用气泡，准备点击: %s", filename)
        if not self._click_bubble(bubble, filename=filename):
            logger.warning("下载文件失败: 点击引用气泡失败 filename=%s", filename)
            return None

        # 4. 等待弹出下载对话框
        time.sleep(0.5)  # 给微信渲染弹出窗口的时间
        dialog = self._wait_for_popup(filename, timeout=_POPUP_SEARCH_TIMEOUT)
        if dialog is None:
            logger.warning("下载文件失败: 未弹出下载对话框 filename=%s", filename)
            return None

        # 5. 查找并点击"接收文件"按钮
        button = self._find_receive_button(dialog)
        if button is None:
            logger.warning("下载文件失败: 未找到接收按钮 filename=%s", filename)
            return None

        logger.info("找到接收按钮，准备点击: %s", filename)
        if not self._click_receive(button, dialog=dialog):
            logger.warning("下载文件失败: 点击接收按钮失败 filename=%s", filename)
            return None

        # 6. 轮询等待文件出现在监控目录
        logger.info("已触发下载，等待文件落地: %s", filename)
        path = None
        try:
            path = self._wait_for_file(filename, timeout=_FILE_POLL_TIMEOUT)
            if path:
                logger.info("文件下载成功: %s -> %s", filename, path)
            else:
                logger.warning("文件下载超时: %s", filename)
        finally:
            # 无论成功/超时，都尝试关闭微信自动打开的文件预览窗口
            # 优先使用实际下载的文件名（微信可能加了序号如(3)），其次用原始文件名
            close_name = os.path.basename(path) if path else filename
            self._close_file_windows(close_name)
        return path

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _find_quote_bubble(self, root, filename: str) -> Optional[Any]:
        """在聊天窗口中查找包含文件名的引用气泡。

        策略：
            1. 先尝试在消息列表（ListControl）的子控件中查找 name 包含 filename 的项。
               命中后进一步在其子控件中定位精确的引用区域（避免点击整个消息行）。
            2. 若消息列表策略失败，回退到全局深度搜索 Button/Group/Image。
        """
        # 辅助：尝试在父控件中查找最匹配的子控件
        def _find_best_subcontrol(parent, target_name: str):
            """在 parent 内部查找引用文件相关的最精确子控件。"""
            # 先尝试精确匹配文件名 —— 只对 Image/Button/Group 生效
            # StaticText 通常是整段消息文本块，点击中心会点到错误位置，跳过
            for sub_type in ("Image", "Button", "Group"):
                try:
                    sub = platform.automation.find_control(
                        parent,
                        control_type=sub_type,
                        name=target_name,
                        search_depth=6,
                        timeout=0.4,
                    )
                    if sub is not None:
                        return sub, sub_type, "exact"
                except Exception:
                    pass
            # 再尝试扩展名或文件类型图标匹配（微信可能截断文件名）
            ext = (target_name.split(".")[-1] if "." in target_name else "").lower()
            fallback_patterns = []
            if ext:
                fallback_patterns.append(f".{ext}")
            fallback_patterns.extend(["PDF", "pdf", "文件", "File"])
            for pattern in fallback_patterns:
                for sub_type in ("Image", "Button", "Group"):
                    try:
                        sub = platform.automation.find_control(
                            parent,
                            control_type=sub_type,
                            name=pattern,
                            search_depth=6,
                            timeout=0.3,
                        )
                        if sub is not None:
                            return sub, sub_type, f"fallback({pattern})"
                    except Exception:
                        pass
            return None, None, None

        # 策略 1：遍历消息列表子控件
        try:
            msg_list = self._find_message_list(root)
            if msg_list is not None:
                children = _safe_children(msg_list)
                # 从后往前找（最新消息优先）
                for child in reversed(children[-20:]):
                    name = _safe_name(child)
                    if filename in name:
                        logger.debug(
                            "引用气泡匹配(消息列表): %s in %.80r", filename, name
                        )
                        sub, sub_type, match_kind = _find_best_subcontrol(
                            child, filename
                        )
                        if sub is not None:
                            logger.debug(
                                "引用气泡精确定位: type=%s kind=%s name=%.80r",
                                sub_type,
                                match_kind,
                                _safe_name(sub),
                            )
                            return sub
                        # 精确定位失败：打印子控件结构帮助调试
                        self._log_children_debug(child, filename)
                        return child
        except Exception as exc:
            logger.debug("消息列表查找引用气泡失败: %s", exc)

        # 策略 2：全局深度搜索 Button / Group / Image
        for control_type in ("Image", "Button", "Group"):
            try:
                ctrl = platform.automation.find_control(
                    root,
                    control_type=control_type,
                    name=filename,
                    search_depth=_BUBBLE_SEARCH_DEPTH,
                    timeout=2,
                )
                if ctrl is not None:
                    logger.debug(
                        "引用气泡匹配(全局搜索): type=%s filename=%s",
                        control_type,
                        filename,
                    )
                    return ctrl
            except Exception as exc:
                logger.debug(
                    "全局搜索引用气泡失败(type=%s): %s", control_type, exc
                )

        logger.warning("未找到引用气泡: filename=%s", filename)
        return None

    @staticmethod
    def _log_children_debug(parent, filename: str) -> None:
        """打印 parent 下前 20 个子控件的调试信息，帮助定位引用区域结构。"""
        try:
            lines = []
            for sub, depth in platform.automation.walk_control(
                parent, include_top=False, max_depth=4
            ):
                if len(lines) >= 20:
                    break
                sub_name = _safe_name(sub)
                sub_type = ""
                try:
                    sub_type = str(sub.ControlTypeName or "")
                except Exception:
                    pass
                cls = ""
                try:
                    cls = str(sub.ClassName or "")
                except Exception:
                    pass
                rect = ""
                try:
                    r = platform.automation.get_bounding_rectangle(sub)
                    if r:
                        rect = f"({r.left},{r.top}-{r.right},{r.bottom})"
                except Exception:
                    pass
                marker = " <<<" if filename in sub_name else ""
                lines.append(
                    f"  [{depth}] {sub_type:12s} {cls:20s} {rect:20s} {sub_name[:60]!r}{marker}"
                )
            if lines:
                logger.debug(
                    "引用气泡子控件结构 (filename=%s):\n%s",
                    filename,
                    "\n".join(lines),
                )
        except Exception as exc:
            logger.debug("打印子控件调试信息失败: %s", exc)

    def _click_bubble(self, bubble, filename: str = "") -> bool:
        """点击引用气泡。尝试多种策略确保命中可点击区域。

        策略（按可靠性排序）：
            1. 点击气泡内文件图标(Image)或文件名子控件
            2. 使用 bounding rect 模拟真实鼠标点击（大区域时偏下，命中引用区）
            3. AXPress action
            4. 点击气泡内第一个 Button 子控件
        """
        # 策略 1: 优先点击气泡内最精确的子控件（文件图标/Image 或 Button）
        # 注意：StaticText 通常是整段消息文本块，点击中心会点到错误位置，需要额外校验
        try:
            for sub_type in ("Image", "Button"):
                sub = platform.automation.find_control(
                    bubble,
                    control_type=sub_type,
                    name=filename,
                    search_depth=5,
                    timeout=0.5,
                )
                if sub is not None:
                    rect = platform.automation.get_bounding_rectangle(sub)
                    if rect:
                        x = (rect.left + rect.right) // 2
                        y = (rect.top + rect.bottom) // 2
                        logger.info(
                            "点击引用气泡内 %s 子控件: (%d, %d) name=%.80r",
                            sub_type, x, y, _safe_name(sub),
                        )
                        platform.input.mouse_click(x, y)
                        return True
            # StaticText 兜底：只有当 name 接近 filename 长度时才点击（避免点到整段消息）
            if filename:
                sub = platform.automation.find_control(
                    bubble,
                    control_type="StaticText",
                    name=filename,
                    search_depth=5,
                    timeout=0.5,
                )
                if sub is not None:
                    sub_name = _safe_name(sub)
                    # 如果 StaticText 包含大量其他文本（如 @消息、引用标记），跳过
                    if len(sub_name) <= len(filename) + 10:
                        rect = platform.automation.get_bounding_rectangle(sub)
                        if rect:
                            x = (rect.left + rect.right) // 2
                            y = (rect.top + rect.bottom) // 2
                            logger.info(
                                "点击引用气泡内 StaticText 子控件: (%d, %d) name=%.80r",
                                x, y, sub_name,
                            )
                            platform.input.mouse_click(x, y)
                            return True
                    else:
                        logger.debug(
                            "跳过过大 StaticText 子控件 (len=%d): %.80r",
                            len(sub_name), sub_name,
                        )
        except Exception as exc:
            logger.debug("点击引用气泡内精确子控件失败: %s", exc)

        # 策略 2: bounding rect 鼠标点击
        # 微信消息气泡通常包含发送者、正文、引用区；引用区在最底部。
        # 引用区（文件名/文件图标）通常在气泡左侧，不要点击水平中心（会偏右），
        # 而是点击左侧偏内一点的位置（左边界 + 15% 宽度），同时保持底部偏上。
        try:
            rect = platform.automation.get_bounding_rectangle(bubble)
            if rect:
                width = rect.right - rect.left
                # x 偏左：引用文件区在气泡左侧，中心会点到正文下方
                x = rect.left + int(width * 0.15) + 20
                height = rect.bottom - rect.top
                if height > 60:
                    # 大区域：点击底部（引用区通常在消息气泡最下方）
                    y = int(rect.bottom - 12)
                    logger.info(
                        "点击引用气泡(底部偏左): (%d, %d) rect=%s",
                        x, y, rect,
                    )
                elif height > 40:
                    # 中等区域：点击偏下
                    y = int(rect.top + height * 0.7)
                    logger.info(
                        "点击引用气泡(偏下偏左): (%d, %d) rect=%s",
                        x, y, rect,
                    )
                else:
                    # 小区域：点击中心
                    y = (rect.top + rect.bottom) // 2
                    logger.info(
                        "点击引用气泡(中心偏左): (%d, %d) rect=%s",
                        x, y, rect,
                    )
                platform.input.mouse_click(x, y)
                return True
        except Exception as exc:
            logger.debug("鼠标点击引用气泡失败: %s", exc)

        # 策略 3: AXPress action
        try:
            if platform.automation.click(bubble, simulateMove=False):
                logger.info("点击引用气泡(AXPress): %s", _safe_name(bubble)[:80])
                return True
        except Exception as exc:
            logger.debug("AXPress 点击引用气泡失败: %s", exc)

        # 策略 4: 递归查找并点击气泡内第一个 Button 子控件
        try:
            btn = platform.automation.find_control(
                bubble,
                control_type="Button",
                search_depth=4,
                timeout=1,
            )
            if btn is not None:
                logger.info(
                    "点击引用气泡内 Button 子控件: %s", _safe_name(btn)[:80]
                )
                rect = platform.automation.get_bounding_rectangle(btn)
                if rect:
                    x = (rect.left + rect.right) // 2
                    y = (rect.top + rect.bottom) // 2
                    platform.input.mouse_click(x, y)
                else:
                    platform.automation.click(btn, simulateMove=False)
                return True
        except Exception as exc:
            logger.debug("点击引用气泡内 Button 失败: %s", exc)

        logger.warning("所有点击策略均失败: %s", _safe_name(bubble)[:80])
        return False

    def _wait_for_popup(self, filename: str, timeout: float) -> Optional[Any]:
        """等待并返回包含文件下载对话框的弹出窗口控件。

        识别策略（按优先级）：
            1. 标题包含文件名的窗口（所有窗口，不论新旧）
            2. 标题包含扩展名/关键词的窗口
            3. 新窗口且标题为文件相关
            4. 窗口内部包含"接收文件"按钮（最强兜底）
        """
        deadline = time.time() + timeout
        # 记录初始窗口 ID 集合，用于识别新窗口
        initial_wids = set()
        try:
            from ...platform import _macos
            app_ref = _macos._get_wechat_app_ref()
            if app_ref:
                windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
                if windows:
                    for w in windows:
                        try:
                            initial_wids.add(_macos._register_window(w))
                        except Exception:
                            pass
        except Exception:
            pass

        # 预计算文件名特征，用于扩展名匹配
        _, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        base_name = os.path.splitext(filename)[0]

        while time.time() < deadline:
            try:
                from ...platform import _macos
                app_ref = _macos._get_wechat_app_ref()
                if not app_ref:
                    time.sleep(_POPUP_SEARCH_INTERVAL)
                    continue

                windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
                if not windows:
                    time.sleep(_POPUP_SEARCH_INTERVAL)
                    continue

                # 先打印所有窗口信息（info 级别，方便排查）
                for w in windows:
                    try:
                        wtitle = _macos._ax_get_title(w)
                        wid = _macos._register_window(w)
                        is_new = wid not in initial_wids
                        logger.info(
                            "窗口扫描: title=%r wid=%s is_new=%s",
                            wtitle, wid, is_new,
                        )
                    except Exception:
                        continue

                # 策略 1: 标题包含完整文件名（仅限新窗口，避免匹配已打开的文件预览窗口）
                for w in windows:
                    try:
                        wtitle = _macos._ax_get_title(w)
                        if filename in wtitle:
                            wid = _macos._register_window(w)
                            is_new = wid not in initial_wids
                            if is_new:
                                logger.info(
                                    "弹出窗口匹配(文件名): title=%r wid=%s",
                                    wtitle, wid,
                                )
                                self._popup_window_titles.append(wtitle)
                                return platform.automation.control_from_handle(wid)
                            else:
                                logger.debug(
                                    "跳过已存在窗口(文件名): title=%r wid=%s",
                                    wtitle, wid,
                                )
                    except Exception:
                        continue

                # 策略 2: 标题包含扩展名/基础名/文件关键词（仅限新窗口）
                for w in windows:
                    try:
                        wtitle = _macos._ax_get_title(w)
                        wid = _macos._register_window(w)
                        is_new = wid not in initial_wids
                        if not is_new:
                            continue
                        wtitle_lower = wtitle.lower()
                        matched = False
                        if ext_lower and ext_lower in wtitle_lower:
                            matched = True
                        elif base_name and base_name in wtitle:
                            matched = True
                        elif any(kw in wtitle for kw in ("PDF", "pdf", "文件", "File", "下载", "Download")):
                            matched = True
                        if matched:
                            logger.info(
                                "弹出窗口匹配(关键词): title=%r wid=%s",
                                wtitle, wid,
                            )
                            self._popup_window_titles.append(wtitle)
                            return platform.automation.control_from_handle(wid)
                    except Exception:
                        continue

                # 策略 3: 新窗口且标题为文件相关
                for w in windows:
                    try:
                        wtitle = _macos._ax_get_title(w)
                        wid = _macos._register_window(w)
                        if wid not in initial_wids and (
                            wtitle in {"文件", "File", "", " ", "下载", "Download"}
                            or "文件" in wtitle
                            or "File" in wtitle
                        ):
                            logger.info(
                                "弹出窗口匹配(新窗口): title=%r wid=%s",
                                wtitle, wid,
                            )
                            self._popup_window_titles.append(wtitle)
                            return platform.automation.control_from_handle(wid)
                    except Exception:
                        continue

                # 策略 4: 检查每个窗口内部是否有"接收文件"按钮（最强兜底）
                # 微信可能复用已有窗口对象，导致 wid 已在 initial_wids 中
                for w in windows:
                    try:
                        wid = _macos._register_window(w)
                        wtitle = _macos._ax_get_title(w)
                        ctrl = platform.automation.control_from_handle(wid)
                        if ctrl is None:
                            continue
                        for btn_name in _RECEIVE_BUTTON_NAMES:
                            btn = platform.automation.find_control(
                                ctrl,
                                control_type="Button",
                                name=btn_name,
                                search_depth=6,
                                timeout=0.3,
                            )
                            if btn is not None:
                                logger.info(
                                    "弹出窗口匹配(内部按钮): title=%r wid=%s btn=%r",
                                    wtitle, wid, btn_name,
                                )
                                return ctrl
                    except Exception:
                        continue

                # 策略 5: 弹窗可能是主窗口内的面板而非独立窗口
                try:
                    root = platform.automation.control_from_handle(
                        platform.window_manager.find_wechat_window()
                    )
                    if root is not None:
                        for btn_name in _RECEIVE_BUTTON_NAMES:
                            btn = platform.automation.find_control(
                                root,
                                control_type="Button",
                                name=btn_name,
                                search_depth=8,
                                timeout=0.3,
                            )
                            if btn is not None:
                                logger.info(
                                    "弹出窗口匹配(主窗口面板): btn=%r", btn_name
                                )
                                return root
                except Exception:
                    pass

            except Exception as exc:
                logger.debug("弹窗扫描异常: %s", exc)

            time.sleep(_POPUP_SEARCH_INTERVAL)

        logger.warning(
            "弹出窗口等待超时: filename=%s, initial_wids=%s",
            filename, initial_wids,
        )
        return None

    def _find_receive_button(self, dialog) -> Optional[Any]:
        """在弹出对话框中查找"接收文件"按钮。

        部分 macOS 微信弹窗的 Accessibility 树为空，此时尝试全局搜索或坐标硬编码。
        """
        # 策略 1: 弹窗内精确 name 匹配
        for control_type in ("Button", "Group", "StaticText"):
            for btn_name in _RECEIVE_BUTTON_NAMES:
                try:
                    btn = platform.automation.find_control(
                        dialog,
                        control_type=control_type,
                        name=btn_name,
                        search_depth=6,
                        timeout=1.0,
                    )
                    if btn is not None:
                        rect = platform.automation.get_bounding_rectangle(btn)
                        logger.info(
                            "找到接收按钮(%s): name=%r rect=%s",
                            control_type, btn_name, rect,
                        )
                        return btn
                except Exception:
                    pass

        # 策略 2: 弹窗内子串匹配
        for control_type in ("Button", "Group", "StaticText", "Image"):
            try:
                controls = platform.automation.find_all_controls(
                    dialog, control_type=control_type, search_depth=10
                )
                for ctrl in controls:
                    name = _safe_name(ctrl)
                    if any(kw in name for kw in ("接收", "Receive", "接受", "Accept", "Download", "下载")):
                        rect = platform.automation.get_bounding_rectangle(ctrl)
                        logger.info(
                            "找到接收按钮(子串): type=%s name=%r rect=%s",
                            control_type, name, rect,
                        )
                        return ctrl
            except Exception:
                pass

        # 策略 3: 全局搜索 — 弹窗 Accessibility 树可能为空，从微信应用根搜索
        logger.warning("弹窗内未找到接收按钮，尝试全局搜索...")
        try:
            from ...platform import _macos
            app_ref = _macos._get_wechat_app_ref()
            if app_ref:
                app_ctrl = platform.automation.control_from_handle(
                    _macos._register_window(app_ref)
                )
                if app_ctrl is not None:
                    for control_type in ("Button", "Group", "StaticText"):
                        for btn_name in _RECEIVE_BUTTON_NAMES:
                            try:
                                btn = platform.automation.find_control(
                                    app_ctrl,
                                    control_type=control_type,
                                    name=btn_name,
                                    search_depth=12,
                                    timeout=1.0,
                                )
                                if btn is not None:
                                    rect = platform.automation.get_bounding_rectangle(btn)
                                    logger.info(
                                        "找到接收按钮(全局): type=%s name=%r rect=%s",
                                        control_type, btn_name, rect,
                                    )
                                    return btn
                            except Exception:
                                pass
        except Exception as exc:
            logger.debug("全局搜索接收按钮失败: %s", exc)

        # 策略 4: 基于弹窗坐标硬编码点击位置
        # 从截图观察，绿色按钮在弹窗底部中央，高度约 30px
        logger.warning("全局搜索也未找到，尝试基于弹窗坐标硬编码点击")
        try:
            rect = platform.automation.get_bounding_rectangle(dialog)
            if rect:
                # 按钮在弹窗底部中央，y = bottom - 40 左右
                x = (rect.left + rect.right) // 2
                y = int(rect.bottom - 40)
                logger.info("硬编码点击接收按钮位置: (%d, %d) 基于弹窗=%s", x, y, rect)
                platform.input.mouse_click(x, y)
                # 返回一个假按钮对象，让调用方以为找到了
                return dialog
        except Exception as exc:
            logger.debug("硬编码点击失败: %s", exc)

        # 策略 5: AppleScript — 绕过空的 Accessibility 树，直接让 System Events 点击
        logger.warning("坐标硬编码也失败，尝试 AppleScript 点击")
        try:
            import subprocess as _sp
            script = '''
            tell application "System Events"
                tell process "WeChat"
                    set frontWin to first window whose value of attribute "AXMain" is true
                    try
                        click (first button of frontWin whose name contains "接收" or name contains "Receive" or name contains "接受" or name contains "Accept")
                        return "clicked"
                    on error
                        -- 如果找不到具体名称，尝试点击第一个按钮（通常是默认/接收按钮）
                        click button 1 of frontWin
                        return "clicked_fallback"
                    end try
                end tell
            end tell
            '''
            result = _sp.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info("AppleScript 点击成功: %s", result.stdout.strip())
                return dialog
            else:
                logger.debug("AppleScript 失败: %s", result.stderr)
        except Exception as exc:
            logger.debug("AppleScript 执行失败: %s", exc)

        logger.warning("所有找按钮策略均失败")
        return None

    def _click_receive(self, button, dialog=None) -> bool:
        """点击"接收文件"按钮。

        macOS 微信的绿色按钮 AXPress 经常不生效，必须强制跟一次真实鼠标点击。
        如果提供了 dialog，会在点击后验证按钮是否消失，以确认点击生效。
        """
        # 策略 1: AXPress action（可能不生效，但作为前置）
        ax_ok = False
        try:
            ax_ok = bool(platform.automation.click(button, simulateMove=False))
            logger.info("点击接收文件(AXPress): success=%s", ax_ok)
        except Exception as exc:
            logger.debug("AXPress 点击接收按钮失败: %s", exc)

        # 策略 2: 真实鼠标点击（强制执行，双保险）
        mouse_ok = False
        try:
            rect = platform.automation.get_bounding_rectangle(button)
            if rect:
                x = (rect.left + rect.right) // 2
                y = (rect.top + rect.bottom) // 2
                logger.info("点击接收文件(鼠标): (%d, %d)", x, y)
                platform.input.mouse_click(x, y)
                mouse_ok = True
        except Exception as exc:
            logger.debug("鼠标点击接收按钮失败: %s", exc)

        # 策略 3: 如果鼠标点击也拿不到坐标，尝试 AXPress + simulateMove
        if not mouse_ok:
            try:
                platform.automation.click(button, simulateMove=True)
                logger.info("点击接收文件(AXPress+simulateMove)")
                mouse_ok = True
            except Exception:
                pass

        # 点击后验证：1秒后检查按钮是否还在，如果还在说明点击没生效
        if dialog is not None and mouse_ok:
            time.sleep(1.0)
            still_there = False
            for btn_name in _RECEIVE_BUTTON_NAMES:
                try:
                    btn = platform.automation.find_control(
                        dialog,
                        control_type="Button",
                        name=btn_name,
                        search_depth=6,
                        timeout=0.5,
                    )
                    if btn is not None:
                        still_there = True
                        break
                except Exception:
                    pass
            if still_there:
                logger.warning("点击后按钮仍存在，可能未命中，尝试再次点击")
                try:
                    rect = platform.automation.get_bounding_rectangle(button)
                    if rect:
                        x = (rect.left + rect.right) // 2
                        y = (rect.top + rect.bottom) // 2
                        logger.info("再次点击接收文件(鼠标): (%d, %d)", x, y)
                        platform.input.mouse_click(x, y)
                except Exception:
                    pass
            else:
                logger.info("点击后按钮已消失，确认命中")

        return mouse_ok or ax_ok

    def _wait_for_file(self, filename: str, timeout: float) -> Optional[str]:
        """轮询 file_monitor 直到文件出现。"""
        if self.file_monitor is None:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            path = self.file_monitor.resolve(filename)
            if path:
                return path
            time.sleep(_FILE_POLL_INTERVAL)
        return None

    def _close_file_windows(self, filename: str) -> None:
        """关闭由微信自动打开的文件窗口（避免窗口堆积）。

        macOS 微信下载文件后会自动用默认应用打开，此方法在下载完成后
        检测并关闭标题包含文件名的应用窗口。

        支持多种关闭方式兜底：AXCloseButton → button 1 → Cmd+W，
        同时跳过微信自身窗口避免误关聊天窗口。
        """
        if not filename:
            return

        # 提取多个匹配关键字提高命中率：完整文件名、去掉后缀、扩展名
        base_name = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1]
        # 过滤 AppleScript 特殊字符，避免注入/语法错误
        safe_name = base_name.replace('"', '').replace('\\', '')
        safe_ext = ext.replace('"', '').replace('\\', '') if ext else ""
        if not safe_name:
            return

        applescript_closed = 0
        try:
            import subprocess as _sp
            script = f'''
tell application "System Events"
    set closedCount to 0
    set skippedCount to 0
    repeat with proc in (get processes whose background only is false)
        set procName to name of proc
        -- 只跳过 Finder/Dock/System Events，微信的文件预览窗口也需要关闭
        if procName is "Finder" or procName is "Dock" or procName is "System Events" then
            set skippedCount to skippedCount + 1
        else
            try
                repeat with win in (get windows of proc)
                    set winName to name of win
                    -- 同时检查文件名前缀和扩展名，提高带序号文件命中率
                    if winName contains "{safe_name}" or (winName contains "{safe_ext}" and winName contains "{safe_name}") then
                        try
                            -- 方式1: 点击关闭按钮 (AXCloseButton)
                            click (first button of win whose subrole is "AXCloseButton")
                            set closedCount to closedCount + 1
                        on error errMsg1
                            try
                                -- 方式2: 点击窗口第一个按钮（通常是关闭按钮）
                                click button 1 of win
                                set closedCount to closedCount + 1
                            on error errMsg2
                                try
                                    -- 方式3: 激活窗口后发送 Cmd+W
                                    set frontmost of proc to true
                                    delay 0.2
                                    keystroke "w" using command down
                                    set closedCount to closedCount + 1
                                on error errMsg3
                                    -- 关闭失败，记录到日志
                                end try
                            end try
                        end try
                    end if
                end repeat
            end try
        end if
    end repeat
    return closedCount
end tell
'''
            result = _sp.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if result.returncode == 0:
                count_str = result.stdout.strip()
                if count_str:
                    try:
                        applescript_closed = int(count_str)
                    except ValueError:
                        pass
                if applescript_closed > 0:
                    logger.info(
                        "已关闭 %s 个自动打开的文件窗口: %s",
                        applescript_closed, filename,
                    )
                else:
                    logger.debug(
                        "AppleScript 未找到需要关闭的文件窗口: filename=%s, base=%s, ext=%s",
                        filename, safe_name, safe_ext,
                    )
            else:
                logger.debug("关闭文件窗口脚本失败: %s", result.stderr)
        except Exception as exc:
            logger.debug("关闭文件窗口 AppleScript 失败: %s", exc)

        # AX API 兜底：AppleScript 失败或未命中时，用底层 AX 直接操作
        if applescript_closed == 0:
            try:
                ax_closed = self._close_via_ax_api(filename)
                if ax_closed > 0:
                    logger.info(
                        "AX API 已关闭 %s 个文件窗口: %s",
                        ax_closed, filename,
                    )
                else:
                    logger.debug(
                        "AX API 也未找到可关闭的文件窗口: %s",
                        filename,
                    )
            except Exception as exc:
                logger.debug("AX API 关闭文件窗口失败: %s", exc)

    def _close_via_ax_api(self, filename: str) -> int:
        """使用 macOS AX API 直接遍历所有应用窗口并关闭匹配项。

        作为 AppleScript 的兜底方案，对 WPS Office 等非标准 Cocoa 应用更可靠。
        """
        try:
            from ...platform import _macos
            import Cocoa
        except Exception:
            return 0

        base_name = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1]

        # 收集所有用于匹配的标题模式
        match_patterns = [p for p in (filename, base_name, ext) if p]
        # 增加前缀匹配，解决微信加序号的问题（如 "表单号(3).docx" 以 "表单号" 开头）
        if base_name and base_name not in match_patterns:
            match_patterns.append(base_name)
        # 增加 3 字符子串匹配（仅当 base_name >= 3 字），比 2 字符更安全
        if base_name and len(base_name) >= 3:
            for i in range(len(base_name) - 2):
                sub = base_name[i : i + 3]
                if sub not in match_patterns:
                    match_patterns.append(sub)
        # 加上 _wait_for_popup 过程中记录的实际弹窗标题
        recorded_titles = list(self._popup_window_titles)
        self._popup_window_titles.clear()  # 消费后清空，避免累积

        closed = 0
        workspace = Cocoa.NSWorkspace.sharedWorkspace()

        for app in workspace.runningApplications():
            try:
                app_name = str(app.localizedName() or "")
                # 跳过系统进程，但**不跳过微信**——微信自己的文件预览窗口也要关闭
                if app_name in ("Finder", "Dock", "System Events"):
                    continue

                pid = app.processIdentifier()
                if pid <= 0:
                    continue

                app_ref = _macos._ax_create_application(pid)
                if not app_ref:
                    continue

                try:
                    windows, err = _macos._ax_copy_attr(
                        app_ref, _macos._AX_WINDOW_LIST_ATTR
                    )
                    if not windows or err != 0:
                        continue

                    for win in windows:
                        try:
                            wtitle = _macos._ax_get_title(win)
                            if not wtitle:
                                continue

                            # 匹配逻辑：任意模式匹配即可
                            matched = any(p in wtitle for p in match_patterns)
                            if not matched:
                                matched = any(
                                    t in wtitle for t in recorded_titles if t
                                )

                            if not matched:
                                continue

                            # 找到匹配的窗口，尝试关闭
                            # 方式1: 找 AXCloseButton 子控件并执行 AXPress
                            children = _macos._ax_get_children(win)
                            close_btn = None
                            for child in children:
                                try:
                                    subrole = _macos._ax_get_attr(
                                        child, "AXSubrole"
                                    )
                                    if subrole == "AXCloseButton":
                                        close_btn = child
                                        break
                                except Exception:
                                    continue

                            if close_btn and _macos._ax_perform_action(
                                close_btn, "AXPress"
                            ):
                                closed += 1
                                logger.debug(
                                    "AX API 关闭窗口(按钮): title=%s app=%s",
                                    wtitle, app_name,
                                )
                                continue

                            # 方式2: 对窗口本身执行 AXClose
                            if _macos._ax_perform_action(win, "AXClose"):
                                closed += 1
                                logger.debug(
                                    "AX API 关闭窗口(动作): title=%s app=%s",
                                    wtitle, app_name,
                                )
                                continue

                            # 方式3: 先 AXRaise 再尝试按钮
                            _macos._ax_perform_action(win, "AXRaise")
                            time.sleep(0.15)
                            if close_btn and _macos._ax_perform_action(
                                close_btn, "AXPress"
                            ):
                                closed += 1
                                logger.debug(
                                    "AX API 关闭窗口(Raise+按钮): title=%s app=%s",
                                    wtitle, app_name,
                                )
                                continue

                            # 方式4: 获取窗口位置，直接鼠标点击左上角关闭按钮坐标
                            # macOS 标准窗口关闭按钮约在左上角 (left+13, top-13)
                            pos = _macos._ax_get_position(win)
                            size = _macos._ax_get_size(win)
                            if pos and size:
                                btn_x = int(pos[0] + 13)
                                btn_y = int(pos[1] + size[1] - 13)
                                _macos._simulate_mouse_click(btn_x, btn_y)
                                closed += 1
                                logger.debug(
                                    "AX API 关闭窗口(鼠标点击): title=%s app=%s",
                                    wtitle, app_name,
                                )
                        except Exception:
                            continue
                except Exception:
                    continue
            except Exception:
                continue

        return closed

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _find_message_list(root):
        """查找聊天消息列表控件（复用 listener.py 逻辑）。"""
        try:
            msg_list = root.ListControl(AutomationId="chat_message_list")
            if msg_list.Exists(maxSearchSeconds=0.08):
                return msg_list
        except Exception:
            pass

        # Fallback：遍历查找最像消息列表的 ListControl
        try:
            candidates = []
            for control, depth in platform.automation.walk_control(root, include_top=True, max_depth=8):
                try:
                    if control.ControlTypeName != "ListControl":
                        continue
                except Exception:
                    continue
                score = 0
                try:
                    for child in list(control.GetChildren())[-12:]:
                        cls = ""
                        try:
                            cls = child.ClassName or ""
                        except Exception:
                            pass
                        auto_id = ""
                        try:
                            auto_id = child.AutomationId or ""
                        except Exception:
                            pass
                        if cls in {
                            "mmui::ChatTextItemView",
                            "mmui::ChatBubbleItemView",
                        } or auto_id == "chat_bubble_item_view":
                            score += 10
                        elif cls == "mmui::ChatItemView":
                            score += 2
                except Exception:
                    pass
                if score > 0:
                    candidates.append((score, control))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1]
        except Exception:
            pass

        return None

    @staticmethod
    def _find_window_by_title(title_substring: str) -> Optional[Any]:
        """枚举微信应用的所有窗口，返回标题包含指定子串的窗口控件。

        直接调用底层 _macos AX API，避免 PlatformControl 包装层的性能损耗。
        """
        try:
            from ...platform import _macos
        except Exception:
            return None

        try:
            app_ref = _macos._get_wechat_app_ref()
            if not app_ref:
                return None
            windows, _ = _macos._ax_copy_attr(app_ref, _macos._AX_WINDOW_LIST_ATTR)
            if not windows:
                return None
            for window in windows:
                try:
                    wtitle = _macos._ax_get_title(window)
                    if title_substring in wtitle:
                        # 包装为 PlatformControl 以便上层统一使用
                        return platform.automation.control_from_handle(
                            _macos._register_window(window)
                        )
                except Exception:
                    continue
        except Exception:
            pass

        return None
