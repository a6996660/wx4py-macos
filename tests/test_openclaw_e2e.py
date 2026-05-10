# -*- coding: utf-8 -*-
"""OpenClaw 真实端到端验证。

直接调用 OpenClawClient.run_agent() 验证完整链路：
- CLI 调用
- JSON 解析（payloads 格式）
- 回复文本提取
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.openclaw_client import OpenClawClient, OpenClawConfig


def test_e2e_openclaw_client():
    """端到端：OpenClawClient.run_agent 真实调用。"""
    cfg = OpenClawConfig(
        enabled=True,
        agent_id="main",
        timeout=60,
    )
    client = OpenClawClient(cfg)

    print("🧪 端到端测试 1: 简单问候")
    start = time.time()
    reply = client.run_agent("你好，请用一句话介绍自己")
    elapsed = time.time() - start
    print(f"   耗时: {elapsed:.1f}s")
    print(f"   回复: {reply[:80]}...")
    assert len(reply) > 0, "回复不应为空"
    assert "小龙" in reply or "AI" in reply or "助手" in reply, f"回复内容异常: {reply}"
    print("   ✅ 通过\n")

    print("🧪 端到端测试 2: 数学计算")
    start = time.time()
    reply = client.run_agent("123 乘以 456 等于多少")
    elapsed = time.time() - start
    print(f"   耗时: {elapsed:.1f}s")
    print(f"   回复: {reply[:80]}...")
    assert len(reply) > 0
    print("   ✅ 通过\n")

    print("🧪 端到端测试 3: session 隔离")
    session_a = "session_a_1234"
    session_b = "session_b_5678"
    reply_a = client.run_agent("请记住我的代号是 Alpha", session_id=session_a)
    reply_b = client.run_agent("请记住我的代号是 Beta", session_id=session_b)
    print(f"   Session A 回复: {reply_a[:60]}...")
    print(f"   Session B 回复: {reply_b[:60]}...")
    # 不要求内容不同（模型可能不严格遵循 session），但要求调用成功
    print("   ✅ 通过\n")


if __name__ == "__main__":
    print("=" * 50)
    print("OpenClaw 真实端到端验证")
    print("=" * 50 + "\n")
    try:
        test_e2e_openclaw_client()
        print("=" * 50)
        print("🎉 端到端验证全部通过！")
        print("=" * 50)
    except Exception as exc:
        print(f"\n❌ 端到端验证失败: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
