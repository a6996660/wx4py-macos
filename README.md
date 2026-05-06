# wx4py macOS 微信群 @ AI 自动回复机器人

wx4py 现在定位为 **macOS-only 微信 4.x 自动化项目**，主线能力是监听指定微信群左侧会话列表中的 `[有人@我]` 提示，串行调用 OpenAI 兼容大模型，并在微信里自动回复 @ 你的成员。

本仓库基于/受原始项目 [claw-codes/wx4py](https://github.com/claw-codes/wx4py) 启发，感谢原作者 claw-codes 及贡献者提供的思路与基础工作。请继续尊重原项目许可证、作者署名和知识产权声明。当前仓库是面向 macOS 微信机器人的改造版本，不再承诺 Windows 支持。

## 能力范围

- macOS 微信 4.x 主窗口自动化，基于辅助功能 Accessibility。
- 只监听配置文件中列出的群聊，且只在群消息 @ 当前登录昵称时触发。
- 不搜索群名、不轮询聊天区作为多群消息来源，避免搜索框打开网络结果或多群串消息。
- 多群 @ 消息进入同一个串行队列，逐条调用大模型、逐条切群发送。
- 同群上下文隔离，支持从 `logs/group_mentions` 恢复最近上下文。
- 回复前随机延迟，发送后做可见性确认，关键错误额外写入按天错误日志。
- 支持 OpenAI-compatible 接口，例如火山引擎 Ark Bots。
- 支持 OpenClaw 双引擎模式：普通 @ 消息走 LLM；带配置前缀的消息（如 `/c`、`/claw`）转给本地 OpenClaw agent。
- 支持引用文件交给 OpenClaw 处理，并把 OpenClaw 修改或生成的文件自动发送回微信群。

## 环境要求

- macOS 12 或更高版本。
- Python 3.9 或更高版本。
- 微信 macOS 4.x，已登录，主窗口保持打开且不要最小化。
- 给运行脚本的终端、Python 解释器或 OpenClaw 授予「系统设置 -> 隐私与安全性 -> 辅助功能」权限。

## 安装

```bash
cd /Volumes/zt/project/wx4py-main
python3 -m pip install -r requirements.txt
python3 -m pip install -e .
```

首次运行前建议确认导入正常：

```bash
python3 - <<'PY'
from src import WeChatClient, AIClient, AIConfig, AIResponder
print("ok")
PY
```

## 配置

复制配置模板：

```bash
cp wx4py_ai_config.example.json wx4py_ai_config.json
```

编辑 `wx4py_ai_config.json`：

```json
{
  "groups": ["交个朋友", "生活如此美好"],
  "reply_delay_range": [2, 5],
  "ai_queue_size": 0,
  "ai_context_size": 8,
  "ai_max_reply_chars": 180,
  "group_log_root": "logs/group_mentions",
  "error_log_root": "logs/errors",
  "default": "ark",
  "providers": {
    "ark": {
      "base_url": "https://ark.cn-beijing.volces.com/api/v3/bots",
      "api_format": "completions",
      "model": "bot-20241125225426-wzcrl",
      "api_key": "replace-with-your-ark-api-key",
      "temperature": 0.7,
      "max_tokens": 220,
      "timeout": 20
    }
  }
}
```

`groups` 是监听白名单。脚本只处理这些群的 `[有人@我]` 提示，其他群不会自动回复。

## 启动

```bash
python3 run_ai_group_bot.py
```

启动后会打印配置文件、监听群聊、随机延迟范围、上下文恢复条数、群日志目录和错误日志目录。按 `Ctrl+C` 停止。

## 聊天示例

普通 AI 回复：

```text
丁某某：@豆角 今天项目测试环境要注意什么？
豆角：@丁某某 可以先确认服务状态、依赖配置和最近错误日志...
```

OpenClaw 处理文件：

```text
丁某某：引用「表单号_10.docx」并发送：@豆角 /c 文件名称序号改成 11 发给我
豆角：@丁某某 已改成 11 了，文件路径：/Users/.../.openclaw/workspace/.wx4py_files/表单号_11.docx
豆角：发送文件「表单号_11.docx」
```

继续纯文本对话：

```text
丁某某：@豆角 /c 很好
豆角：@丁某某 谢谢
```

重置当前群会话：

```text
丁某某：@豆角 /new
豆角：@丁某某 已重置对话，开始新会话～
```

OpenClaw 前缀以配置文件为准。推荐把短前缀配置为 `/c`，保留 `/claw` 作为更明确的备用写法。

## 文件处理链路

当群成员引用微信文件卡片并 @ 机器人使用 OpenClaw 前缀时，机器人会尽量读取引用气泡中的附件名称，把文件下载或定位到本地，复制到 OpenClaw workspace 下的 `.wx4py_files` 目录，再把附件名称、相对路径和用户指令一起发给 OpenClaw。

OpenClaw 返回后，机器人会按以下顺序查找需要回传的文件：结构化 JSON 中的文件字段、回复文本里的文件路径或文件名、workspace 最近生成文件兜底扫描。输入文件副本会被排除，避免把原始附件误当成输出文件发回去。

## 日志

- `wx4py.log`：项目运行日志。
- `logs/group_mentions/YYYY-MM-DD/群名.log`：每个群每天的审计日志，记录收到谁 @、消息内容、模型回复、发送结果。
- `logs/errors/YYYY-MM-DD.log`：关键错误日志，包括 AI 调用异常、发送前切群失败、微信提示发送失败、发送后不可见等。

这些日志会用于启动时恢复同群上下文，所以重启后用户问“刚才他说什么”时，模型仍能看到最近的同群 @ 对话。

OpenClaw 文件排查建议优先看 `wx4py.log`，里面会记录引用气泡原始文本、附件解析候选、最终事件字段、OpenClaw 命令、workspace 扫描和文件发送结果。

## macOS 使用注意

- 微信必须已登录，主窗口需要保持打开，建议不要最小化。发送、下载文件和读取引用气泡时脚本会激活微信。
- 机器人运行期间最好不要手动操作微信窗口，尤其不要切群、点击聊天区、输入文字、拖拽文件或打开独立聊天窗口；这些操作可能抢走焦点，导致读错气泡或发到错误窗口。
- 如果微信出现登录页、弹窗、独立聊天窗口或辅助功能控件树短暂不可用，脚本会尽量恢复或写入错误日志。
- 普通文字回复最稳定；OpenClaw 文件处理已经支持，但依赖微信 macOS 当前 UI、辅助功能权限、文件下载状态和窗口焦点稳定性。
- 要让 OpenClaw 处理文件，建议直接引用微信文件卡片再发送命令，例如 `@豆角 /c 文件名称序号改成 12 发给我`。单独说“这个文件”但没有引用气泡时，机器人通常无法可靠判断是哪一个文件。
- OpenClaw 生成的文件不要在机器人发送前手动移动或删除。
- 群公告 Markdown 会通过 macOS 剪贴板写入 HTML 和纯文本 fallback，富文本效果取决于微信 macOS 当前 UI。
- 不建议用于高频营销、骚扰或违反微信规则的场景。

## 代码调用

推荐直接使用 `run_ai_group_bot.py`。如果需要嵌入自己的程序，可以使用核心 API：

```python
from src import AIClient, AIConfig, AIResponder, AsyncCallbackHandler, WeChatClient

groups = ["交个朋友"]
ai = AIClient(AIConfig.from_file("wx4py_ai_config.json"))
responder = AIResponder(ai, context_size=8, reply_on_at=True, max_reply_chars=180)

with WeChatClient(auto_connect=True) as wx:
    wx.process_groups(
        groups,
        [AsyncCallbackHandler(responder, auto_reply=True, reply_on_at=True)],
        reply_delay_range=(2, 5),
        block=True,
    )
```

## 回归验证建议

1. 启动 `python3 run_ai_group_bot.py`，确认当前登录微信昵称识别正确。
2. 在配置群里 @ 当前昵称，确认消息进入队列、模型生成回复、随机延迟后发送。
3. 多个群轮流 @，确认回复不串群，同名成员也按群上下文隔离。
4. 连续多条 @，确认按队列顺序回复，不并发抢焦点。
5. 重启脚本后询问“刚才问的什么”，确认能从群审计日志恢复上下文。
6. 人为断网或填错 API Key，确认 `logs/errors/YYYY-MM-DD.log` 有 AI 错误记录。
7. 发送失败或发送后聊天区不可见时，确认错误日志出现对应事件。
8. 引用一个 docx 文件并发送 `@豆角 /c 文件名称序号改成 12 发给我`，确认 OpenClaw 命令里带有附件信息，且最终发送的是新生成文件。
9. 在上一条文件任务后发送 `@豆角 /c 很好`，确认 OpenClaw 命令里不再携带上一条附件。
10. 让 OpenClaw 回复包含 `表单号_12.docx，...` 这类中文标点后的文件名，确认机器人仍能解析并发送文件。

## 最近更新

- **2026-05-06**：完善 OpenClaw 文件引用和文件回传链路。支持从微信引用气泡解析附件、传入 OpenClaw、识别生成文件并自动发送；修复纯文本消息误继承旧附件、真实引用消息丢附件、输入文件副本被误发、中文标点后文件名无法解析等问题。README 新增聊天示例、文件链路和 macOS 操作注意事项。
- **2026-05-03**：新增 OpenClaw 本地 agent 双引擎模式。普通 @ 消息继续走 LLM 秒回；带 `/claw` 前缀的 @ 消息自动转给本地 OpenClaw agent 处理（支持工具调用，能力更强），失败时自动降级回 LLM。同时新增引用消息识别能力、修复切群发送偶发卡顿问题。详见 [CHANGELOG](docs/guide/CHANGELOG.md)。

## 许可证与致谢

本项目沿用 AGPL-3.0-or-later 许可证。感谢 [claw-codes/wx4py](https://github.com/claw-codes/wx4py) 原项目提供灵感、命名和早期自动化思路；当前仓库的 macOS-only 机器人改造不代表原项目官方支持范围。
