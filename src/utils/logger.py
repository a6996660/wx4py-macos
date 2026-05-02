# -*- coding: utf-8 -*-
"""日志工具"""
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from ..config import LOG_LEVEL, LOG_FORMAT, LOG_FILE, SEND_AUDIT_LOG_FILE, GROUP_MENTION_LOG_ROOT


def _ensure_parent_dir(file_path: str) -> None:
    """在需要时创建日志文件的父目录。"""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)


def get_logger(name: str) -> logging.Logger:
    """
    获取已配置的日志器实例。

    Args:
        name: 日志器名称（通常使用 __name__）

    Returns:
        logging.Logger: 已配置的日志器
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(stream_handler)

        _ensure_parent_dir(LOG_FILE)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)
        logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
        logger.propagate = False

    return logger


def get_send_audit_logger() -> logging.Logger:
    """获取专用于发送审计记录的结构化日志器。"""
    logger = logging.getLogger("wx4py.send_audit")

    if not logger.handlers:
        _ensure_parent_dir(SEND_AUDIT_LOG_FILE)
        file_handler = logging.FileHandler(SEND_AUDIT_LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger


def log_send_audit(payload: dict) -> None:
    """写入一条结构化发送审计记录（JSONL 格式）。"""
    get_send_audit_logger().info(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _safe_log_filename(name: str) -> str:
    """把群名转换为适合文件名的字符串。"""
    safe = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", str(name or "").strip())
    safe = safe.strip(" .")
    return safe or "未命名群聊"


def log_group_mention_audit(group: str, payload: dict) -> Path:
    """按日期和群聊写入 @ 自动回复审计日志。

    路径形如：logs/group_mentions/2026-05-02/群名.log
    每行是一条 JSON，便于后续检索、统计或导入分析。
    """
    now = datetime.now()
    date_dir = now.strftime("%Y-%m-%d")
    root = Path(os.environ.get("WECHAT_GROUP_MENTION_LOG_ROOT", GROUP_MENTION_LOG_ROOT))
    file_path = root / date_dir / f"{_safe_log_filename(group)}.log"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "time": now.isoformat(timespec="seconds"),
        "group": group,
        **payload,
    }
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
    return file_path
