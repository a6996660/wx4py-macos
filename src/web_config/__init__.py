# -*- coding: utf-8 -*-
"""wx4py 本地网页配置界面。

启动本地 HTTP 服务器提供配置页面，用户通过浏览器完成配置后保存并启动机器人。
"""

from .server import run_config_server

__all__ = ["run_config_server"]
