# -*- coding: utf-8 -*-
"""配置项"""
import os
from pathlib import Path

# 超时设置（秒）
SEARCH_TIMEOUT = 5
OPERATION_INTERVAL = 0.3
SEARCH_RETRY_COUNT = 3
SEARCH_RETRY_DELAY_MIN = 0.8
SEARCH_RETRY_DELAY_MAX = 1.5
SEND_RETRY_COUNT = 2
SEND_RECONNECT_RETRY_COUNT = 1
BATCH_SEND_INTERVAL_MIN = 2.0
BATCH_SEND_INTERVAL_MAX = 3.0
SEND_JITTER_MIN = 0.2
SEND_JITTER_MAX = 0.6
SEND_DEDUP_WINDOW_SECONDS = 60

# 目标验证
ALLOWED_GROUPS = tuple(
    item.strip()
    for item in os.environ.get("WECHAT_ALLOWED_GROUPS", "").split(",")
    if item.strip()
)

# 日志配置
LOG_LEVEL = os.environ.get('WECHAT_LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = os.environ.get("WECHAT_LOG_FILE", str(Path.cwd() / "wx4py.log"))
SEND_AUDIT_LOG_FILE = os.environ.get(
    "WECHAT_SEND_AUDIT_LOG_FILE",
    str(Path.cwd() / "wx4py_send_audit.jsonl"),
)
