---
name: wx4-skill
description: macOS 微信群 @ AI 自动回复机器人 skill。用于通过 wx4py 在 macOS 微信 4.x 中监听指定群聊的 @ 消息，接入 OpenAI 兼容大模型，按队列生成并发送精炼回复。
---

# wx4py macOS 微信机器人 Skill

## 使用原则

当前项目是 macOS-only 微信群 @ AI 自动回复机器人。OpenClaw 或其他代理在使用本项目时，应优先运行项目根目录的 `run_ai_group_bot.py`，不要生成搜索群名、轮询聊天区或频繁双击会话列表的脚本。

核心策略：
- 只监听 `wx4py_ai_config.json` 中 `groups` 配置的群。
- 只处理左侧会话列表出现 `[有人@我]` 的群消息。
- 多群消息进入串行队列，逐条调用大模型、逐条发送。
- 同群上下文按群名隔离，避免同名成员或不同群消息串上下文。
- 发送前切换目标群，发送后确认回复在当前聊天区可见。
- 关键错误写入 `logs/errors/YYYY-MM-DD.log`，群聊审计写入 `logs/group_mentions/YYYY-MM-DD/群名.log`。

## 前置检查

```bash
cd /Volumes/zt/project/wx4py-main
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

```python
from src import WeChatClient, AIClient, AIConfig, AIResponder
print("wx4py macOS ok")
```

macOS 必须授权：
- 系统设置 -> 隐私与安全性 -> 辅助功能。
- 勾选当前终端、Python 解释器、IDE 或 OpenClaw 所在应用。
- 微信主窗口保持打开且不要最小化。

## 配置文件

复制模板：

```bash
cp wx4py_ai_config.example.json wx4py_ai_config.json
```

常用字段：
- `groups`：监听群聊白名单，只处理这里列出的群。
- `reply_delay_range`：回复前随机等待秒数，例如 `[5, 18]`。
- `ai_queue_size`：大模型消息队列长度，`0` 表示不限制。
- `ai_context_size`：同群上下文条数。
- `ai_max_reply_chars`：写入提示词的目标回复长度，程序只做极端兜底。
- `group_log_root`：群 @ 审计日志目录。
- `error_log_root`：关键错误日志目录。
- `providers`：OpenAI 兼容模型服务配置。

火山引擎 Ark Bots 示例：

```json
{
  "default": "ark",
  "providers": {
    "ark": {
      "base_url": "https://ark.cn-beijing.volces.com/api/v3/bots",
      "api_format": "completions",
      "model": "bot-20241125225426-wzcrl",
      "api_key": "replace-with-your-ark-api-key",
      "temperature": 0.7,
      "max_tokens": 300,
      "timeout": 60
    }
  }
}
```

## 推荐启动方式

```bash
python3 run_ai_group_bot.py
```

启动后应看到：
- 配置文件路径。
- 监听群聊列表。
- 回复随机延迟。
- 大模型队列长度。
- 同群上下文长度。
- 群聊审计日志目录。
- 关键错误日志目录。
- 已恢复的上下文消息条数。

## 代码接入示例

```python
from src import AIClient, AIConfig, AIResponder, AsyncCallbackHandler, WeChatClient

groups = ["交个朋友"]
ai = AIClient(AIConfig.from_file("wx4py_ai_config.json"))
responder = AIResponder(ai, context_size=8, reply_on_at=True, max_reply_chars=180)

with WeChatClient(auto_connect=True) as wx:
    wx.process_groups(
        groups,
        [AsyncCallbackHandler(responder, auto_reply=True, reply_on_at=True)],
        reply_delay_range=(5, 18),
        block=True,
    )
```

## 不要这样做

- 不要用搜索框反复搜索群名来监听群聊；macOS 微信搜索框可能打开网络结果。
- 不要双击左侧会话列表；可能打开独立聊天窗口并卡住发送流程。
- 不要从当前聊天区轮询多个群消息；多群切换时容易读串。
- 不要并发发送多个群的回复；微信主窗口和剪贴板会抢焦点。
- 不要把机器人用于高频营销、刷屏或违反微信规则的用途。

## 回归验证清单

1. 单群 @：消息进入队列，模型回复，随机延迟后发送成功。
2. 多群轮流 @：回复按队列顺序发送，不串群。
3. 同名成员 @：上下文仍按群隔离。
4. 连续多条 @：不会重复回复同一条预览消息。
5. 重启后问“刚才”：能从群审计日志恢复上下文。
6. API Key 错误或网络异常：错误写入 `logs/errors/YYYY-MM-DD.log`，监听线程不崩溃。
7. 微信提示发送失败或发送后不可见：错误日志包含 `wechat_send_failure_dialog` 或 `reply_not_visible_after_send`。

## 版权与致谢

本 skill 面向当前 macOS-only 改造版本。项目基于/受 [claw-codes/wx4py](https://github.com/claw-codes/wx4py) 启发，感谢原作者和贡献者。请保留原项目许可证、署名和知识产权说明。
