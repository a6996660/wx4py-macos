# -*- coding: utf-8 -*-
"""微信下载目录文件监控。

通过轮询微信数据目录，建立文件名→完整路径索引，
为 MessageEvent.attachment_name 提供实际磁盘路径解析能力。
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Dict, List, Optional

from ...utils.logger import get_logger

logger = get_logger(__name__)


# macOS 微信（App Store / 官网版）常见数据目录模式
# 新版微信 4.x 使用 xwechat_files 结构，旧版使用 Application Support 结构
_MACOS_WECHAT_BASE_PATTERNS: List[str] = [
    # 新版微信 xwechat_files（App Store 沙箱版）
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*",
    # 旧版微信目录（保留 fallback 兼容）
    "~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/*",
    "~/Library/Application Support/com.tencent.xinWeChat/*",
]

# 在这些子目录中搜索文件
# 新版结构: msg/file/YYYY-MM, msg/image/YYYY-MM
# 旧版结构: Message/MessageTemp, Message
_WECHAT_FILE_SUBDIRS: List[str] = [
    "msg/file",
    "msg/image",
    "Message/MessageTemp",
    "Message",
]


@dataclass(frozen=True)
class FileMonitorConfig:
    """文件监控配置。"""

    enabled: bool = False
    watch_dirs: List[str] = field(default_factory=list)
    poll_interval: float = 5.0
    max_age_seconds: float = 600.0
    auto_discover: bool = True

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "FileMonitorConfig":
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=bool(data.get("enabled", False)),
            watch_dirs=list(data.get("watch_dirs", [])),
            poll_interval=float(data.get("poll_interval", 5.0)),
            max_age_seconds=float(data.get("max_age_seconds", 600.0)),
            auto_discover=bool(data.get("auto_discover", True)),
        )


class WeChatDownloadMonitor:
    """微信下载目录监控器。

    通过后台轮询维护文件名索引，支持根据文件名快速查找实际路径。
    """

    def __init__(self, config: FileMonitorConfig):
        self.config = config
        self._stop_event = Event()
        self._worker: Optional[Thread] = None
        # filename -> (full_path, mtime)
        self._index: Dict[str, tuple[str, float]] = {}
        self._index_lock = False  # 轻量标记锁，避免极端并发问题

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台轮询线程。"""
        if not self.config.enabled:
            logger.info("文件监控未启用，跳过启动")
            return
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = Thread(target=self._scan_loop, daemon=True)
        self._worker.start()
        logger.info(
            "微信下载目录监控已启动: interval=%.1fs, max_age=%.0fs",
            self.config.poll_interval,
            self.config.max_age_seconds,
        )

    def stop(self) -> None:
        """停止监控线程。"""
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        logger.info("微信下载目录监控已停止")

    # ------------------------------------------------------------------
    # 公开查询接口
    # ------------------------------------------------------------------

    def resolve(self, filename: str) -> Optional[str]:
        """根据文件名查找最新的完整路径。

        优先返回索引中的匹配项；若索引中无匹配且未启用后台线程，
        则执行一次即时扫描作为 fallback。

        注意：已下载到本地的微信文件，只要仍然存在就视为有效，
        不受 max_age_seconds 限制（该限制仅用于控制后台索引大小）。
        """
        if not filename:
            return None
        name = filename.strip()

        # 1. 查索引（文件存在即返回，不检查 mtime —— 已下载的文件一直可用）
        entry = self._index.get(name)
        if entry:
            path, _mtime = entry
            if os.path.isfile(path):
                logger.debug("文件索引命中: %s -> %s", name, path)
                return path

        # 2. 即时 fallback 扫描（不受 max_age 限制，直接遍历目录）
        logger.debug("文件索引未命中，执行即时扫描: %s", name)
        for d in self._discover_dirs():
            if not os.path.isdir(d):
                continue
            try:
                for root, _dirs, files in os.walk(d):
                    if name in files:
                        path = os.path.join(root, name)
                        if os.path.isfile(path):
                            self._index[name] = (path, os.path.getmtime(path))
                            logger.debug("即时扫描命中: %s -> %s", name, path)
                            return path
            except Exception as exc:
                logger.debug("即时扫描目录失败 %s: %s", d, exc)

        logger.debug("文件解析失败: %s", name)
        return None

    def resolve_batch(self, filenames: List[str]) -> Dict[str, str]:
        """批量解析文件名，返回 {filename: path} 字典（仅含成功项）。"""
        result: Dict[str, str] = {}
        for name in filenames:
            path = self.resolve(name)
            if path:
                result[name] = path
        return result

    # ------------------------------------------------------------------
    # 内部扫描逻辑
    # ------------------------------------------------------------------

    def _scan_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._refresh_index()
            except Exception as exc:
                logger.warning("文件索引刷新失败: %s", exc)
            self._stop_event.wait(timeout=self.config.poll_interval)

    def _refresh_index(self) -> None:
        """扫描所有监控目录，更新文件名索引。"""
        dirs = self._discover_dirs()
        new_index: Dict[str, tuple[str, float]] = {}
        cutoff = time.time() - self.config.max_age_seconds

        for d in dirs:
            if not os.path.isdir(d):
                continue
            try:
                for root, _dirs, files in os.walk(d):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        try:
                            mtime = os.path.getmtime(fpath)
                        except OSError:
                            continue
                        if mtime < cutoff:
                            continue
                        # 保留最新修改时间的同名文件
                        existing = new_index.get(fname)
                        if existing is None or mtime > existing[1]:
                            new_index[fname] = (fpath, mtime)
            except Exception as exc:
                logger.debug("扫描目录失败 %s: %s", d, exc)

        self._index = new_index
        logger.debug("文件索引已刷新: %d 个文件", len(new_index))

    def _discover_dirs(self) -> List[str]:
        """获取需要扫描的目录列表。"""
        dirs: List[str] = []

        # 用户自定义目录
        for d in self.config.watch_dirs:
            expanded = os.path.expanduser(d)
            if os.path.isdir(expanded):
                dirs.append(expanded)

        # 自动发现微信目录
        if self.config.auto_discover:
            for pattern in _MACOS_WECHAT_BASE_PATTERNS:
                for base in glob.glob(os.path.expanduser(pattern)):
                    if not os.path.isdir(base):
                        continue
                    for sub in _WECHAT_FILE_SUBDIRS:
                        full = os.path.join(base, sub)
                        if os.path.isdir(full):
                            dirs.append(full)

        # 去重并保持顺序
        seen: set = set()
        unique_dirs: List[str] = []
        for d in dirs:
            if d not in seen:
                seen.add(d)
                unique_dirs.append(d)
        return unique_dirs


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------


def create_monitor_from_config(config_dict: Optional[Dict]) -> WeChatDownloadMonitor:
    """从配置字典创建监控器。"""
    cfg = FileMonitorConfig.from_dict(config_dict)
    return WeChatDownloadMonitor(cfg)
