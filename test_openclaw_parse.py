# -*- coding: utf-8 -*-
"""OpenClaw JSON 解析 mock 验证（阻塞项）。"""

from src.openclaw_client import OpenClawClient, OpenClawAgentError, OpenClawTimeoutError


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

from unittest.mock import MagicMock

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
    oc_client.run_agent.return_value = "OpenClaw reply"

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"])
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 查邮件")
    result = hybrid(event)

    assert result == "OpenClaw reply", f"Expected OpenClaw reply, got: {result}"
    oc_client.run_agent.assert_called_once()
    args, kwargs = oc_client.run_agent.call_args
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


def test_hybrid_fallback():
    """OpenClaw 失败时降级到 LLM"""
    ai_resp = MagicMock(spec=AIResponder)
    ai_resp.reply_on_at = True
    ai_resp.return_value = "LLM fallback reply"

    oc_client = MagicMock(spec=OpenClawClient)
    oc_client.run_agent.side_effect = OpenClawAgentError("502 error")

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], fallback_to_llm=True)
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event = _make_event("@机器人 /claw 查邮件")
    result = hybrid(event)

    assert result == "LLM fallback reply", f"Expected fallback, got: {result}"
    oc_client.run_agent.assert_called_once()
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
    oc_client.run_agent.return_value = "ok"

    cfg = OpenClawConfig(enabled=True, prefixes=["/claw"], session_per_group=True)
    hybrid = HybridResponder(ai_resp, openclaw_client=oc_client, openclaw_config=cfg)

    event1 = _make_event("@机器人 /claw hello", group="群A")
    event2 = _make_event("@机器人 /claw hello", group="群B")
    hybrid(event1)
    hybrid(event2)

    calls = oc_client.run_agent.call_args_list
    assert len(calls) == 2
    # session_id 应不同
    assert calls[0].kwargs["session_id"] != calls[1].kwargs["session_id"]
    print("✅ Hybrid test passed: session isolation per group")


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
    print("\n✅ T7 集成测试全部通过！")
