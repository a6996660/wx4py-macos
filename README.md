<p align="center">
  <img src="docs/images/logo_bg.png" alt="wx4py">
</p>

<p align="center">访问官网查看演示视频：<a href="https://wx4py.biglongxia.com/">wx4py.biglongxia.com</a></p>

<p align="center"><strong>让微信4.x自动化变得简单</strong></p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9+-blue" alt="Python Version"></a>
  <a href="https://pypi.org/project/wx4py/"><img src="https://img.shields.io/pypi/v/wx4py.svg" alt="PyPI version"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-green" alt="License"></a>
  <a href="https://www.microsoft.com/windows"><img src="https://img.shields.io/badge/platform-Windows%2010%7C11-lightgrey" alt="Platform"></a>
  <a href="https://www.apple.com/macos/"><img src="https://img.shields.io/badge/platform-macOS%2012%2B-lightgrey" alt="macOS"></a>
  <a href="https://weixin.qq.com/"><img src="https://img.shields.io/badge/wechat-4.x-orange" alt="WeChat"></a>
</p>

---

## 你是否遇到过这些场景？

- 🔁 **每天给多个群发相同通知** —— 手动一个个发送，浪费时间又容易漏掉
- 📁 **同一个文件要分发到多个群** —— 反复拖拽上传，操作繁琐
- ⏰ **想定时发送消息** —— 比如每天下午5点提醒提交日报，但微信没有定时发送功能
- 📊 **需要分析群聊记录** —— 想统计活跃度、提取关键讨论，却没法导出数据
- 🛠️ **批量管理多个群** —— 设置公告、免打扰、置顶，一个个点太麻烦
- 🤖 **想让 AI 帮我操作微信** —— 不想写代码，只想说一句话就完成操作
- 💬 **想做群聊机器人** —— 多个群同时监听，只在被 @ 时调用 AI 自动回复

如果你有以上任何困扰，**wx4py** 可以帮你解决。

---

## wx4py 能做什么？

### 一句话群发通知

```python
from wx4py import WeChatClient

with WeChatClient() as wx:
    wx.chat_window.batch_send(
        ["技术部", "产品部", "运营部"],
        "【通知】明天下午3点开会",
        target_type='group'
    )
```

**效果**：3个群同时收到通知，告别手动逐个发送。

---

### 定时自动提醒

```python
import schedule

def remind_daily_report():
    with WeChatClient() as wx:
        wx.chat_window.batch_send(
            ["研发一组", "研发二组"],
            "【提醒】请提交日报",
            target_type='group'
        )

schedule.every().day.at("17:00").do(remind_daily_report)
```

**效果**：每天下午5点自动发送，无需人工介入。

---

### 文件批量分发

```python
with WeChatClient() as wx:
    # 一份周报，发送到3个部门群
    wx.chat_window.send_file_to(
        "技术部", r"C:\周报\weekly.pdf", target_type='group'
    )
    wx.chat_window.send_file_to(
        "产品部", r"C:\周报\weekly.pdf", target_type='group'
    )
    wx.chat_window.send_file_to(
        "运营部", r"C:\周报\weekly.pdf", target_type='group'
    )
```

**效果**：同一文件快速分发到多个群，省去反复上传的麻烦。

---

### 群公告一键更新

```python
with WeChatClient() as wx:
    # 批量更新多个群的公告
    for group in ["项目群A", "项目群B", "项目群C"]:
        wx.group_manager.modify_announcement_simple(
            group,
            "本周重点：完成用户模块开发"
        )
```

**效果**：多个群的公告同时更新，保持信息同步。

---

### 聊天记录导出分析

```python
import pandas as pd

with WeChatClient() as wx:
    messages = wx.chat_window.get_chat_history(
        "项目讨论组",
        target_type='group',
        since='week'  # 本周的聊天记录
    )

    # 导出为 CSV
    df = pd.DataFrame(messages)
    df.to_csv("chat_history.csv", index=False)

    # 统计消息类型分布
    print(df['type'].value_counts())
```

**效果**：聊天记录导出为 CSV，可用 Excel 打开分析。

---

### 群成员列表获取

```python
with WeChatClient() as wx:
    members = wx.group_manager.get_group_members("技术交流群")
    print(f"群成员数: {len(members)}")
    # ['张三', '李四', '王五', ...]
```

**效果**：一键获取完整成员列表，可用于统计分析。

---

### AI 群聊机器人自动回复

```python
from wx4py import AIClient, AIConfig, AIResponder, AsyncCallbackHandler, WeChatClient

groups = ["测试龙虾1", "测试龙虾2", "测试龙虾3"]

ai = AIClient(
    AIConfig(
        base_url="https://api.siliconflow.cn/v1",
        api_format="completions",
        model="Pro/deepseek-ai/DeepSeek-V3.2",
        api_key="你的 API Key",
        enable_thinking=False,
    )
)

with WeChatClient(auto_connect=True) as wx:
    wx.process_groups(
        groups,
        [
            AsyncCallbackHandler(
                AIResponder(ai, context_size=8, reply_on_at=True),
                auto_reply=True,
            )
        ],
        block=True,
    )
```

**效果**：监听多个群聊，只有被 @ 时才调用 AI 回复；普通消息只监听不打扰。本库发送的回复会自动记录，避免机器人回复触发自己。

<p align="center">
  <img src="docs/images/group-ai-bot-demo.jpg" alt="wx4py AI 群聊机器人自动回复效果" width="360">
</p>

---

### 监听群消息并转发给指定联系人或群

```python
from wx4py import ForwardRuleHandler, GroupForwardRule, WeChatClient

rules = [
    GroupForwardRule(
        source_group="告警群",
        targets=["值班同学"],
        target_type="contact",
        prefix_template="[告警群] ",
    ),
    GroupForwardRule(
        source_group="项目群A",
        targets=["项目群B"],
        target_type="group",
        prefix_template="[项目群A] ",
    ),
]

with WeChatClient(auto_connect=True) as wx:
    wx.process_groups(
        ["告警群", "项目群A"],
        [ForwardRuleHandler(rules)],
        block=True,
    )
```

**效果**：可以监听指定群聊的消息，并把新消息自动转发给指定联系人，或者同步转发到另一个群。适合做告警通知、值班转发、跨群消息同步。

---

### 更多便捷操作

| 你想做的事 | 一行代码 |
|-----------|---------|
| 发消息给联系人 | `wx.chat_window.send_to("张三", "你好")` |
| 发消息给群 | `wx.chat_window.send_to("工作群", "收到", target_type='group')` |
| 发文件 | `wx.chat_window.send_file_to("文件传输助手", r"path\file.pdf")` |
| 搜索联系人/群 | `wx.chat_window.search("张三")` |
| 获取群昵称 | `wx.group_manager.get_group_nickname("工作群")` |
| 设置群昵称 | `wx.group_manager.set_group_nickname("工作群", "我的新昵称")` |
| 开启免打扰 | `wx.group_manager.set_do_not_disturb("工作群", enable=True)` |
| 置顶聊天 | `wx.group_manager.set_pin_chat("重要群", enable=True)` |
| 监听/处理多个群聊 | `wx.process_groups(["群1"], [handler], block=True)` |
| 监听群消息并转发 | `wx.process_groups(["群1"], [ForwardRuleHandler(rules)], block=True)` |
| @ 触发 AI 自动回复 | `wx.process_groups(["群1"], [AsyncCallbackHandler(AIResponder(ai, reply_on_at=True), auto_reply=True)], block=True)` |

---

## 让 AI 帮你操作微信

不想写代码？在 **Claude Code** 或 **OpenClaw** 中直接对话：

```
帮我给文件传输助手发一条消息：测试成功
```

AI 会自动生成代码并执行。详见 [AI Skill 使用指南](#ai-skill-快速使用)。

---

## 为什么选择 wx4py？

| | wx4py | 其他方案 |
|---|---|---|
| **支持微信版本** | 最新 4.x | 多数只支持旧版/Mac版 |
| **安装难度** | pip 一键安装 | 需要配置复杂环境 |
| **使用门槛** | 5分钟上手 | 需要深入了解底层 |
| **稳定性** | 完善的错误处理 | 容易崩溃中断 |
| **监听与回复** | 多群独立窗口监听，发送队列避免抢占 | 通常需自行实现 |
| **AI 集成** | 支持 Claude Code、OpenAI 兼容接口和自定义回调 | 通常需自行封装 |

---

## 快速开始

### 安装

```bash
pip install wx4py
```

### 环境要求

- Windows 10/11 或 macOS 12+
- Python 3.9+
- 微信客户端 4.x（已测试 4.1.7.59、4.1.8.29）

#### macOS 额外要求

macOS 需要安装 PyObjC 依赖并授予辅助功能权限：

```bash
pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa
```

首次运行时会提示授权辅助功能权限，请前往 **系统偏好设置 → 隐私与安全性 → 辅助功能** 中授权你的终端或 Python 解释器。

### 第一次使用

```python
from wx4py import WeChatClient

# 连接微信（需要微信已登录并运行）
with WeChatClient() as wx:
    # 给文件传输助手发条消息测试
    wx.chat_window.send_to("文件传输助手", "wx4py 连接成功！")
```

运行后，你的微信会自动发送这条消息。

如果你是开发者，想查看更多调用示例，请参考 [examples/](./examples/) 目录。
如果你想系统查看完整接口说明，请参考 [docs/guide/API_GUIDE.md](./docs/guide/API_GUIDE.md)。

如果你要提交 Issue，请尽量提供微信版本号、wx4py 版本号、详细复现步骤，并附上相关截图或日志，便于定位问题。

---

## macOS 适配与 AI 群聊机器人

本仓库在原 Windows 自动化能力基础上，补充了 macOS 微信 4.x 的适配与一个可直接运行的群聊 @ 自动回复脚本。

### macOS 已适配能力

- 微信主窗口查找、激活与辅助功能树访问
- 左侧会话列表识别，包括 `session_list`、`session_item_<群名>`
- 基于左侧 `[有人@我]` 预览监听群聊 @ 消息
- 启动时读取当前登录微信昵称，用于判断 `@我`
- 多群消息进入同一个 AI 队列，逐条调用模型，避免并发抢占微信窗口
- 发送队列串行切群、随机延迟发送，降低固定秒回特征
- 按群隔离上下文，避免不同群同名成员导致上下文串用
- 按日期/群名记录 @ 消息审计日志，按日期记录关键错误日志

### macOS 设计约束

macOS 微信主窗口通常只有一个聊天区，多群轮询聊天区容易读串。因此 macOS 端监听策略与 Windows 不同：

- **监听消息**：只读取左侧会话列表的 `[有人@我]` 预览，不轮询当前聊天区。
- **发送消息**：发送前再切换到目标群，找到输入框后粘贴并回车。
- **已在目标群时**：不会重复点击左侧同一个会话项，避免弹出独立聊天窗口。

建议运行时保持微信主窗口打开、不要最小化，尽量不要手动频繁切群或搜索。

### 一键启动 AI 群聊机器人

复制配置模板：

```bash
cp wx4py_ai_config.example.json wx4py_ai_config.json
```

编辑 `wx4py_ai_config.json`，填写监听群、模型服务和 API Key。示例：

```json
{
  "groups": ["交个朋友", "大农村的打工人"],
  "reply_delay_range": [5, 18],
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
      "model": "bot-xxxxxxxx",
      "api_key": "replace-with-your-api-key",
      "temperature": 0.7,
      "max_tokens": 300,
      "timeout": 60
    }
  }
}
```

启动：

```bash
python3 run_ai_group_bot.py
```

脚本会使用单实例锁，避免多个机器人进程同时运行导致重复回复。

### 日志与排错

全局运行日志：

```text
wx4py.log
```

每个群每天的 @ 消息审计日志：

```text
logs/group_mentions/2026-05-02/群名.log
```

关键错误日志：

```text
logs/errors/2026-05-02.log
```

群聊审计日志会记录：

- `received`：收到 @ 消息并进入 AI 队列
- `reply_generated`：模型生成回复
- `reply_sent`：回复发送成功
- `reply_failed`：回复发送失败
- `skipped`：跳过原因，例如未解析到发送人、重复消息、队列满

关键错误日志会额外记录切群失败、AI 调用异常、找不到输入框、微信发送失败弹窗等问题，便于后续定位。

### 使用注意

- 仅建议监听少量明确配置的群聊。
- 建议使用 `reply_on_at=True`，只在被 @ 时回复。
- 不要高频群发或营销式发送；自动回复前应保留随机延迟。
- `ai_max_reply_chars` 会写入模型提示词，让模型自然控制回复长度；程序端只做极端兜底。
- 第三方模型是否能联网查询，取决于你配置的模型服务本身是否开启联网/检索能力。
- `wx4py_ai_config.json` 已被 `.gitignore` 忽略，不要提交真实 API Key。

---

## AI Skill 快速使用

在 Claude Code 或 OpenClaw 中复制以下内容：

```
安装并使用 wx4py skill。

技能地址：https://raw.githubusercontent.com/claw-codes/wx4py/main/wx4-skill/SKILL.md

1、执行命令 pip install wx4py 安装所需库。
2、将 wx4py skill 安装至本地的 skills 目录，并在工具文档和记忆系统中记录该技能。
3、阅读 skill 文档后，向我说明如何与你进行对话以使用该技能。
```

安装后，直接用自然语言对话：

```
帮我给工作群发消息：明天9点开会
```

AI 会自动完成操作。

---

## 常见问题

<details>
<summary><b>Q: 需要保持微信前台运行吗？</b></summary>

建议微信主窗口保持打开且不要最小化。macOS 端不要求微信一直处于最前台，但发送时需要激活/聚焦微信窗口。为了稳定性，建议在专用机器或空闲时段运行自动化任务。

</details>

<details>
<summary><b>Q: 聊天记录能获取发送者吗？</b></summary>

微信 4.x 的 UI 不暴露发送者信息，这是技术限制，暂无法获取。

</details>

<details>
<summary><b>Q: 会被封号吗？</b></summary>

wx4py 模拟真实用户操作，不修改微信客户端。但仍建议：
- 控制发送频率
- 避免大量群发营销内容
- 使用非重要账号测试

</details>

<details>
<summary><b>Q: AI 自动回复会回复所有群消息吗？</b></summary>

不会。推荐使用 `reply_on_at=True`，普通消息只监听和打印，只有群消息 @ 当前群昵称时才会调用回复逻辑。

</details>

<details>
<summary><b>Q: 只能使用 wx4py 内置的 AIClient 吗？</b></summary>

不是。`process_groups()` 接收任意 handler；你可以使用 `AsyncCallbackHandler` 包装自己的 HTTP 客户端、公司内部模型或其他 AI SDK。

</details>

<details>
<summary><b>Q: 支持哪些微信版本？</b></summary>

目前支持微信 4.x 版本，已测试：
- 4.1.7.59
- 4.1.8.29

</details>

---

## 更新记录

完整版本更新说明请查看 [CHANGELOG.md](docs/guide/CHANGELOG.md)。

---

## 许可证

本项目采用 **AGPL-3.0** 许可证，附加商业使用限制。

- ✅ 个人学习、研究、非商业用途
- ❌ 未经授权的商业使用
- 💼 商业授权请联系：sgdygb@gmail.com

详见 [LICENSE](./LICENSE)。

---

## 免责声明

本软件仅用于技术研究和学习目的。使用者需遵守相关法律法规和平台规则。因违规使用导致的任何后果（账号封禁等）由使用者自行承担。

详见 [LICENSE](./LICENSE) 中的完整免责声明。

---

## 致谢

本仓库基于原始项目 [claw-codes/wx4py](https://github.com/claw-codes/wx4py) 进行适配与扩展。原项目作者、贡献者及其知识产权声明应予以保留；本仓库新增的 macOS 适配、AI 群聊机器人启动脚本和相关配置说明，仍遵循原项目许可证与仓库中的授权条款。

感谢 [linux.do 社区](https://linux.do/) 中相关讨论带来的启发，让这个项目的方向和落地方式逐步清晰起来。

同时也感谢 [wxauto](https://github.com/cluic/wxauto) 项目提供的思路参考，为本项目的实现带来了帮助。

也感谢 [yeafel666](https://github.com/yeafel666) 对窗口连接、搜索体验和最小化能力等改进所做的贡献。

---

<p align="center">
  <a href="https://www.star-history.com/#claw-codes/wx4py&Date">
    <img src="https://api.star-history.com/svg?repos=claw-codes/wx4py&type=Date" alt="Star History Chart">
  </a>
</p>

---

<p align="center"><strong>如果这个项目帮你节省了时间，请给一个 Star ⭐</strong></p>

<p align="center">Made with ❤️</p>
