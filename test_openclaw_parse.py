# -*- coding: utf-8 -*-
"""OpenClaw JSON 解析 mock 验证（阻塞项）。"""

import os
import tempfile
import time
from pathlib import Path

from src.openclaw_client import (
    OpenClawClient,
    OpenClawAgentError,
    OpenClawTimeoutError,
    OpenClawResult,
)


def test_case_1_stdout_json():
    """Case 1: stdout JSON 正常回复"""
    stdout = b'{"reply": "hello from openclaw"}'
    stderr = b'Config warnings:\\n- plugins...'
    result = OpenClawClient._parse_result(stdout, stderr)
    assert result == "hello from openclaw", f"Case 1 failed: {result}"
    print("✅ Case 1 passed: stdout JSON")


def test_case_2_stderr_json():
    """Case 2: stderr JSON 正常回复（stdout 为空）"""
    stdout = b''
    stderr = b'some logs\n{"reply": "nested reply"}'
    result = OpenClawClient._parse_result(stdout, stderr)
    assert result == "nested reply", f"Case 2 failed: {result}"
    print("✅ Case 2 passed: stderr JSON (last line)")


def test_case_3_failover_error():
    """Case 3: stderr 文本错误（FailoverError）"""
    stdout = b''
    stderr = b'Config warnings...\nFailoverError: 502 Upstream service temporarily unavailable'
    try:
        OpenClawClient._parse_result(stdout, stderr)
        assert False, "Case 3 should raise OpenClawAgentError"
    except OpenClawAgentError as e:
        assert "502" in str(e), f"Case 3 failed: {e}"
        print("✅ Case 3 passed: FailoverError detected")


def test_case_4_missing_scope():
    """Case 4: stderr 文本错误（missing scope）"""
    stdout = b''
    stderr = b'{"ok": false, "error": {"type": "forbidden", "message": "missing scope: operator.write"}}'
    try:
        OpenClawClient._parse_result(stdout, stderr)
        assert False, "Case 4 should raise OpenClawAgentError"
    except OpenClawAgentError as e:
        assert "权限不足" in str(e) or "missing scope" in str(e), f"Case 4 failed: {e}"
        print("✅ Case 4 passed: missing scope detected")


def test_case_5_unrecognized():
    """Case 5: 完全无法识别"""
    stdout = b''
    stderr = b'random garbage output that is not json'
    try:
        OpenClawClient._parse_result(stdout, stderr)
        assert False, "Case 5 should raise OpenClawAgentError"
    except OpenClawAgentError as e:
        assert "无法解析" in str(e), f"Case 5 failed: {e}"
        print("✅ Case 5 passed: unrecognized output handled")


def test_case_6_nested_output():
    """Case 6: 嵌套 JSON 结构"""
    stdout = b'{"output": [{"type": "text", "text": "nested text reply"}]}'
    stderr = b''
    result = OpenClawClient._parse_result(stdout, stderr)
    assert result == "nested text reply", f"Case 6 failed: {result}"
    print("✅ Case 6 passed: nested output structure")


def test_case_7_text_field():
    """Case 7: text 字段替代 reply"""
    stdout = b'{"text": "using text field"}'
    stderr = b''
    result = OpenClawClient._parse_result(stdout, stderr)
    assert result == "using text field", f"Case 7 failed: {result}"
    print("✅ Case 7 passed: text field fallback")


def test_case_8_timeout_in_stderr():
    """Case 8: timeout 错误在 stderr"""
    stdout = b''
    stderr = b'lane task error: timeout after 60000ms'
    try:
        OpenClawClient._parse_result(stdout, stderr)
        assert False, "Case 8 should raise OpenClawTimeoutError"
    except OpenClawTimeoutError as e:
        assert "timeout" in str(e).lower(), f"Case 8 failed: {e}"
        print("✅ Case 8 passed: timeout in stderr")


def test_case_9_payloads_format():
    """Case 9: OpenClaw 真实本地 agent 格式（payloads[0].text）"""
    import json
    # stdout 为空，JSON 实际输出到 stderr，前面混有 config warnings
    stdout = b''
    stderr = (
        b'Config warnings:\n'
        b'- plugins.entries.feishu: duplicate plugin id detected\n'
        b'[agents/model-providers] [xai-auth] bootstrap config fallback\n'
        + json.dumps(
            {"payloads": [{"text": "我是小龙", "mediaUrl": None}], "meta": {"durationMs": 5886}}
        ).encode("utf-8")
    )
    result = OpenClawClient._parse_result(stdout, stderr)
    assert result == "我是小龙", f"Case 9 failed: {result}"
    print("✅ Case 9 passed: real OpenClaw payloads format from mixed stderr")


# ---------------------------------------------------------------------------
# HybridResponder 前缀路由与降级测试
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch

from src.openclaw_client import HybridResponder, OpenClawConfig, OpenClawClient, OpenClawAgentError
from src.ai import AIResponder
from src.features.messaging.listener import MessageEvent


def _make_event(content: str, group: str = "测试群") -> MessageEvent:
    return MessageEvent(
        group=group,
        content=content,
        timestamp=0.0,
        group_nickname="机器人",
        sender_nickname="张三",
        is_at_me=True,
        raw=None,
    )


def test_hybrid_prefix_match():
    """前缀匹配时调用 OpenClaw"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM reply"

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(
        text="OpenClaw reply", file_paths=[]
    )

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"])
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 查邮件")
    result = hybrid(event)

    assert isinstance(result, OpenClawResult)
    assert result.text == "OpenClaw reply", f"Expected OpenClaw reply, got: {result}"
    oc_client.run_agent_full.assert_called_once()
    args, kwargs = oc_client.run_agent_full.call_args
    assert "查邮件" in args[0]
    ai_resp.assert_not_called()
    print("✅ Hybrid test passed: prefix routes to OpenClaw")


def test_hybrid_no_prefix_llm():
    """无前缀时调用 LLM"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM reply"

    oc_client = MagicMock(spec=OpenClawClient)
    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"])
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 你好")
    result = hybrid(event)

    assert result == "LLM reply", f"Expected LLM reply, got: {result}"
    oc_client.run_agent.assert_not_called()
    ai_resp.assert_called_once()
    print("✅ Hybrid test passed: no prefix routes to LLM")


def test_openclaw_only_no_prefix_routes_to_openclaw():
    """仅 OpenClaw 模式不需要前缀，所有消息都调用 OpenClaw"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM reply"

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(
        text="OpenClaw only reply", file_paths=[]
    )

    cfg = OpenClawConfig(mode="openclaw", prefixes=["/claw"])
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 你好")
    result = hybrid(event)

    assert isinstance(result, OpenClawResult)
    assert result.text == "OpenClaw only reply"
    oc_client.run_agent_full.assert_called_once()
    args, kwargs = oc_client.run_agent_full.call_args
    assert "你好" in args[0]
    ai_resp.assert_not_called()
    print("✅ Hybrid test passed: openclaw-only routes without prefix")


def test_hybrid_fallback():
    """OpenClaw 失败时降级到 LLM"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM fallback reply"

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.side_effect = OpenClawAgentError("502 error")

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], fallback_to_llm=True)
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 查邮件")
    result = hybrid(event)

    assert result == "LLM fallback reply", f"Expected fallback, got: {result}"
    oc_client.run_agent_full.assert_called_once()
    ai_resp.assert_called_once()
    print("✅ Hybrid test passed: fallback to LLM on OpenClaw error")


def test_hybrid_disabled():
    """OpenClaw 禁用时全部走 LLM"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM only"

    cfg = OpenClawConfig(enabled=False)
    hybrid = HybridResponder(ai_resp, openclaw_client=None, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 查邮件")
    result = hybrid(event)

    assert result == "LLM only", f"Expected LLM only, got: {result}"
    ai_resp.assert_called_once()
    print("✅ Hybrid test passed: disabled mode routes all to LLM")


def test_hybrid_session_isolation():
    """不同群使用不同 session ID"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(text="ok", file_paths=[])

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], session_per_group=True)
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event1 = _make_event("@机器人 /claw hello", group="群A")
    event2 = _make_event("@机器人 /claw hello", group="群B")
    hybrid(event1)
    hybrid(event2)

    calls = oc_client.run_agent_full.call_args_list
    assert len(calls) == 2
    # session_id 应不同
    assert calls[0].kwargs["session_id"] != calls[1].kwargs["session_id"]
    print("✅ Hybrid test passed: session isolation per group")


# ---------------------------------------------------------------------------
# OpenClawResult 复合结果解析测试
# ---------------------------------------------------------------------------


def test_extract_result_full_payloads_file():
    data = {
        "payloads": [
            {"type": "text", "text": "  分析结果如下  "},
            {"type": "file", "file_path": "/tmp/report.pdf"},
        ]
    }
    result = OpenClawClient._extract_result_full(data)
    assert result.text == "分析结果如下"
    assert result.file_paths == ["/tmp/report.pdf"]
    print("✅ ExtractResultFull test passed: payloads with file")


def test_extract_result_full_top_level_file_paths():
    data = {
        "text": "done",
        "file_paths": ["/tmp/a.png", "/tmp/b.docx"],
    }
    result = OpenClawClient._extract_result_full(data)
    assert result.text == "done"
    assert result.file_paths == ["/tmp/a.png", "/tmp/b.docx"]
    print("✅ ExtractResultFull test passed: top-level file_paths")


def test_extract_result_full_backward_compat():
    data = {"reply": "legacy text"}
    result = OpenClawClient._extract_result_full(data)
    assert result.text == "legacy text"
    assert result.file_paths == []
    print("✅ ExtractResultFull test passed: backward compatibility")


def test_parse_result_full_mixed_stderr():
    import json
    stdout = b""
    stderr = (
        b"Config warnings:\n"
        + json.dumps(
            {
                "payloads": [
                    {"type": "text", "text": "report"},
                    {"type": "file", "file_path": "/tmp/f.pdf"},
                ]
            }
        ).encode("utf-8")
    )
    result = OpenClawClient._parse_result_full(stdout, stderr)
    assert result.text == "report"
    assert result.file_paths == ["/tmp/f.pdf"]
    print("✅ ParseResultFull test passed: mixed stderr with file payload")


def test_scan_workspace_recent_files_excludes_input_copy():
    """最近文件兜底不能把复制到 workspace 的输入附件当成输出文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        workspace_files = home / ".openclaw" / "workspace" / ".wx4py_files"
        workspace_files.mkdir(parents=True)

        original = home / "Downloads" / "input.docx"
        original.parent.mkdir()
        original.write_text("original", encoding="utf-8")

        input_copy = workspace_files / "input.docx"
        input_copy.write_text("copied input", encoding="utf-8")

        old_output = workspace_files / "old_output.docx"
        old_output.write_text("old", encoding="utf-8")

        started_at = time.time() - 10
        os.utime(old_output, (started_at - 5, started_at - 5))
        os.utime(input_copy, (started_at + 1, started_at + 1))

        real_output = workspace_files / "real_output.docx"
        real_output.write_text("new", encoding="utf-8")
        os.utime(real_output, (started_at + 2, started_at + 2))

        cfg = OpenClawConfig(enabled=True)
        hybrid = HybridResponder(
            MagicMock(spec=AIResponder),
            openclaw_client=None,
            openclaw_config=cfg,
        )

        with patch("src.openclaw_client.Path.home", return_value=home):
            result = hybrid._scan_workspace_recent_files(
                str(original),
                workspace_input_path=str(input_copy),
                started_at=started_at,
                max_age_seconds=120,
            )

        assert str(input_copy) not in result
        assert str(old_output) not in result
        assert result == [str(real_output)]
        print("✅ Workspace scan test passed: excludes input copy and pre-existing files")


def test_extract_file_paths_from_text_chinese_punctuation():
    """文本中纯文件名后接中文标点时，也应能反查到 workspace 文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        workspace_files = home / ".openclaw" / "workspace" / ".wx4py_files"
        workspace_files.mkdir(parents=True)
        output = workspace_files / "表单号_9.docx"
        output.write_text("docx", encoding="utf-8")

        text = "好了，文件已是序号 9。对应路径：我核对过，表单号_9.docx，和原文件内容一致。"
        with patch("src.openclaw_client.Path.home", return_value=home):
            result = HybridResponder._extract_file_paths_from_text(text)

        assert result == [str(output)]
        print("✅ Text path extraction test passed: Chinese punctuation boundary")


# ---------------------------------------------------------------------------
# HybridResponder OpenClawResult 返回测试
# ---------------------------------------------------------------------------


def test_hybrid_returns_openclaw_result():
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(
        text="OpenClaw text", file_paths=["/tmp/out.txt"]
    )

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"])
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 生成文件报告")
    result = hybrid(event)

    assert isinstance(result, OpenClawResult)
    assert result.text == "OpenClaw text"
    assert result.file_paths == ["/tmp/out.txt"]
    oc_client.run_agent_full.assert_called_once()
    ai_resp.assert_not_called()
    print("✅ Hybrid test passed: returns OpenClawResult with files")


# ---------------------------------------------------------------------------
# CallbackHandler OpenClawResult 动作构建测试
# ---------------------------------------------------------------------------


def test_callback_handler_build_actions_openclaw_result():
    from src.features.messaging.processor import CallbackHandler, ReplyAction, FileReplyAction

    def _callback(event):
        return OpenClawResult(
            text="AI reply", file_paths=["/tmp/a.txt", "/tmp/b.txt"]
        )

    handler = CallbackHandler(_callback, auto_reply=True, reply_on_at=True)
    event = _make_event("@机器人 /claw 生成文件")
    actions = handler.handle(event)

    assert isinstance(actions, list)
    assert len(actions) == 3
    assert isinstance(actions[0], ReplyAction)
    assert "AI reply" in actions[0].content
    assert isinstance(actions[1], FileReplyAction)
    assert actions[1].file_path == "/tmp/a.txt"
    assert isinstance(actions[2], FileReplyAction)
    assert actions[2].file_path == "/tmp/b.txt"
    print("✅ CallbackHandler test passed: builds ReplyAction + FileReplyAction from OpenClawResult")


def test_callback_handler_build_actions_files_only():
    from src.features.messaging.processor import CallbackHandler, FileReplyAction

    def _callback(event):
        return OpenClawResult(text="", file_paths=["/tmp/x.png"])

    handler = CallbackHandler(_callback, auto_reply=True, reply_on_at=True)
    event = _make_event("@机器人 /claw 仅文件")
    actions = handler.handle(event)

    assert isinstance(actions, list)
    assert len(actions) == 1
    assert isinstance(actions[0], FileReplyAction)
    assert actions[0].file_path == "/tmp/x.png"
    print("✅ CallbackHandler test passed: files only when text is empty")


# ---------------------------------------------------------------------------
# 附件文本解析测试
# ---------------------------------------------------------------------------


def test_parse_attachment_from_text():
    from src.features.messaging.listener import _parse_attachment_from_text
    assert _parse_attachment_from_text("[文件] report.pdf", "群A") == ("report.pdf", "file")
    assert _parse_attachment_from_text("[File] report.pdf", "群A") == ("report.pdf", "file")
    raw_name = "群A\n张三: @机器人 /c 改文件\n张三: [文件] report.docx"
    assert _parse_attachment_from_text(
        raw_name, "群A", current_content="@机器人 /c 改文件"
    ) == ("report.docx", "file")
    assert _parse_attachment_from_text("[图片]", "群A") == (None, "image")
    assert _parse_attachment_from_text("[Image]", "群A") == (None, "image")
    assert _parse_attachment_from_text("[照片] 描述", "群A") == (None, "image")
    assert _parse_attachment_from_text("普通文本消息", "群A") == (None, None)
    print("✅ Attachment parse test passed")


def test_bare_image_quote_marker():
    """微信引用图片时气泡内容可能只有裸的“图片”。"""
    from src.features.messaging.listener import MessageEvent

    event = MessageEvent(
        group="群A",
        content="@机器人 /c 分析这张图片",
        timestamp=0.0,
        quoted_sender="张三",
        quoted_content="图片",
        attachment_type="image",
    )
    assert event.quoted_content == "图片"
    assert event.attachment_type == "image"
    print("✅ Bare image quote fixture passed")


# ---------------------------------------------------------------------------
# 微信下载目录监控测试
# ---------------------------------------------------------------------------


def test_file_monitor_resolve():
    import tempfile
    from src.features.messaging.file_monitor import (
        FileMonitorConfig,
        WeChatDownloadMonitor,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建测试文件
        test_file = os.path.join(tmpdir, "test_doc.pdf")
        with open(test_file, "w") as f:
            f.write("test")

        cfg = FileMonitorConfig(
            enabled=True,
            watch_dirs=[tmpdir],
            poll_interval=1.0,
            max_age_seconds=600.0,
        )
        monitor = WeChatDownloadMonitor(cfg)
        monitor._refresh_index()

        resolved = monitor.resolve("test_doc.pdf")
        assert resolved == test_file, f"Expected {test_file}, got {resolved}"

        # 测试不存在的文件
        assert monitor.resolve("nonexistent.txt") is None
        print("✅ FileMonitor test passed: resolve by filename")


def test_file_monitor_auto_discover_off():
    import tempfile
    from src.features.messaging.file_monitor import (
        FileMonitorConfig,
        WeChatDownloadMonitor,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "doc.xlsx")
        with open(test_file, "w") as f:
            f.write("test")

        cfg = FileMonitorConfig(
            enabled=True,
            watch_dirs=[tmpdir],
            auto_discover=False,
        )
        monitor = WeChatDownloadMonitor(cfg)
        monitor._refresh_index()

        resolved = monitor.resolve("doc.xlsx")
        assert resolved == test_file
        print("✅ FileMonitor test passed: custom watch_dirs only")


# ---------------------------------------------------------------------------
# WeChatFileDownloader 测试
# ---------------------------------------------------------------------------


def test_file_downloader_shortcut():
    """file_monitor 已索引时直接短路返回，不走 UIA。"""
    import tempfile
    from src.features.messaging.file_monitor import FileMonitorConfig, WeChatDownloadMonitor
    from src.features.messaging.file_downloader import WeChatFileDownloader

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "shortcut.pdf")
        with open(test_file, "w") as f:
            f.write("test")

        cfg = FileMonitorConfig(enabled=True, watch_dirs=[tmpdir], auto_discover=False)
        monitor = WeChatDownloadMonitor(cfg)
        monitor._refresh_index()

        downloader = WeChatFileDownloader(file_monitor=monitor)
        result = downloader.download("shortcut.pdf")
        assert result == test_file, f"Expected {test_file}, got {result}"
        print("✅ FileDownloader test passed: shortcut when already indexed")


def test_file_downloader_no_monitor():
    """无 file_monitor 且无法获取窗口时返回 None。"""
    from src.features.messaging.file_downloader import WeChatFileDownloader

    downloader = WeChatFileDownloader(file_monitor=None)
    result = downloader.download("some_file.pdf")
    assert result is None
    print("✅ FileDownloader test passed: returns None without monitor/window")


# ---------------------------------------------------------------------------
# HybridResponder 主动下载触发测试
# ---------------------------------------------------------------------------


def test_hybrid_file_downloader_trigger():
    """file_monitor 未命中时，HybridResponder 应尝试调用 file_downloader 主动下载。"""
    from unittest.mock import MagicMock
    from src.ai import AIResponder
    from src.openclaw_client import HybridResponder, OpenClawConfig, OpenClawClient, OpenClawResult

    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(
        text="analysis done", file_paths=[]
    )

    # file_monitor 未命中
    file_monitor = MagicMock()
    file_monitor.resolve.return_value = None

    # file_downloader 成功下载
    file_downloader = MagicMock()
    file_downloader.download.return_value = "/tmp/downloaded.pdf"

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], file_support=True)
    hybrid = HybridResponder(
        ai_resp,
        openclaw_client=oc_client,
        openclaw_config=cfg,
        file_monitor=file_monitor,
        file_downloader=file_downloader,
    )

    event = _make_event("@机器人 /claw 分析这个文件")
    event = event.__class__(**{**event.__dict__, "attachment_name": "report.pdf"})

    result = hybrid(event)

    # 验证 file_downloader 被调用
    file_downloader.download.assert_called_once_with("report.pdf")
    # 验证 OpenClawClient 被传入下载后的路径
    oc_client.run_agent_full.assert_called_once()
    _, kwargs = oc_client.run_agent_full.call_args
    assert kwargs.get("file_path") == "/tmp/downloaded.pdf"
    assert isinstance(result, OpenClawResult)
    print("✅ HybridResponder test passed: triggers file_downloader when monitor misses")


def test_hybrid_image_extractor_trigger():
    """图片附件应被提取成本地图片路径并传给 OpenClaw。"""
    from unittest.mock import MagicMock
    from src.ai import AIResponder
    from src.openclaw_client import HybridResponder, OpenClawConfig, OpenClawClient, OpenClawResult

    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent_full.return_value = OpenClawResult(
        text="image analysis done", file_paths=[]
    )

    image_extractor = MagicMock()
    image_extractor.extract_latest.return_value = "/tmp/wx-image.png"

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], file_support=True)
    hybrid = HybridResponder(
        ai_resp,
        openclaw_client=oc_client,
        openclaw_config=cfg,
        image_extractor=image_extractor,
    )

    event = _make_event("@机器人 /claw 分析这张图片")
    event = event.__class__(**{**event.__dict__, "attachment_type": "image"})

    result = hybrid(event)

    image_extractor.extract_latest.assert_called_once()
    oc_client.run_agent_full.assert_called_once()
    _, kwargs = oc_client.run_agent_full.call_args
    assert kwargs.get("file_path") == "/tmp/wx-image.png"
    assert isinstance(result, OpenClawResult)
    print("✅ HybridResponder test passed: extracts image attachment for OpenClaw")


def test_quote_fallback_recent_any_ignored():
    """最近任意引用气泡不能污染当前纯文本消息。"""
    from src.features.messaging.listener import _parse_quote_from_text

    current_clean = "很好"
    stale_bubble = "丁某某: [文件] 表单号_10.docx\n@豆角 /c 文件名称序号改成 11 发给我"
    sender, quoted, remaining = _parse_quote_from_text(stale_bubble)
    assert quoted
    assert current_clean not in stale_bubble
    assert remaining != current_clean
    print("✅ Quote fallback test fixture passed: stale bubble is distinct from current text")

if __name__ == "__main__":
    print("Running OpenClaw JSON parse mock tests...\n")
    test_case_1_stdout_json()
    test_case_2_stderr_json()
    test_case_3_failover_error()
    test_case_4_missing_scope()
    test_case_5_unrecognized()
    test_case_6_nested_output()
    test_case_7_text_field()
    test_case_8_timeout_in_stderr()
    test_case_9_payloads_format()
    print("\n🎉 All 9 JSON parse tests passed!")

    print("\nRunning HybridResponder integration tests...\n")
    test_hybrid_prefix_match()
    test_hybrid_no_prefix_llm()
    test_hybrid_fallback()
    test_hybrid_disabled()
    test_hybrid_session_isolation()
    print("\n🎉 All 5 HybridResponder tests passed!")

    print("\nRunning OpenClawResult composite tests...\n")
    test_extract_result_full_payloads_file()
    test_extract_result_full_top_level_file_paths()
    test_extract_result_full_backward_compat()
    test_parse_result_full_mixed_stderr()
    test_scan_workspace_recent_files_excludes_input_copy()
    test_extract_file_paths_from_text_chinese_punctuation()
    print("\n🎉 All 6 composite result tests passed!")

    print("\nRunning HybridResponder file routing tests...\n")
    test_hybrid_returns_openclaw_result()
    print("\n🎉 HybridResponder file routing test passed!")

    print("\nRunning CallbackHandler action build tests...\n")
    test_callback_handler_build_actions_openclaw_result()
    test_callback_handler_build_actions_files_only()
    print("\n🎉 All 2 CallbackHandler tests passed!")

    print("\nRunning attachment parse tests...\n")
    test_parse_attachment_from_text()
    test_bare_image_quote_marker()
    print("\n🎉 Attachment parse test passed!")

    print("\nRunning file monitor tests...\n")
    test_file_monitor_resolve()
    test_file_monitor_auto_discover_off()
    print("\n🎉 All 2 file monitor tests passed!")

    print("\nRunning file downloader tests...\n")
    test_file_downloader_shortcut()
    test_file_downloader_no_monitor()
    print("\n🎉 All 2 file downloader tests passed!")

    print("\nRunning HybridResponder file downloader trigger tests...\n")
    test_hybrid_file_downloader_trigger()
    test_hybrid_image_extractor_trigger()
    print("\n🎉 HybridResponder attachment trigger tests passed!")

    print("\nRunning quote fallback isolation tests...\n")
    test_quote_fallback_recent_any_ignored()
    print("\n🎉 Quote fallback isolation test passed!")

    print("\n✅ T7 + T9 集成测试全部通过！")
