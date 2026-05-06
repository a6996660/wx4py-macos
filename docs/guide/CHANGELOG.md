# 更新日志

本文档记录项目各版本的重要变更。

版本号遵循语义化版本的基本原则：
- `PATCH`：修复问题、稳定性优化、兼容性调整
- `MINOR`：新增功能且保持兼容
- `MAJOR`：存在破坏性变更

## [macOS-only] - 2026-05-06

### 新增

- **OpenClaw 文件引用处理链路**：群成员引用微信文件卡片并使用 OpenClaw 前缀时，机器人会解析附件名称、定位或下载文件、复制到 workspace，并把附件路径随用户指令一起传给 OpenClaw。
- **OpenClaw 文件回传链路**：支持从 OpenClaw 结构化 JSON、回复文本中的文件路径或文件名、workspace 最近生成文件扫描中识别输出文件，并自动发送回微信群。
- `MessageEvent` 新增附件相关字段，用于在消息事件中携带引用文件名称和类型。
- 新增更详细的文件诊断日志，记录左侧会话原始文本、右侧聊天气泡原始文本、附件解析候选、最终事件字段、OpenClaw 命令、workspace 扫描和文件发送结果。

### 优化

- OpenClaw 调用增加 gateway/local 兜底：gateway 返回空输出时自动 fallback 到 `--local`。
- workspace 扫描只接受本次 OpenClaw 调用开始之后产生的文件，并排除输入文件副本，降低误发原文件概率。
- 引用气泡匹配改为优先按当前用户消息内容命中；当 macOS 微信气泡原始文本缺少发送者时仍能识别真实引用附件。
- 旧的“最近任意引用气泡”兜底改为诊断日志，不再直接作为附件来源，避免纯文本消息继承上一条文件引用。
- 文件名解析兼容中文标点，例如 `表单号_12.docx，...` 可以正确识别为 `表单号_12.docx`。

### 修复

- 修复 OpenClaw 只修改或生成文件但机器人没有发送文件的问题。
- 修复纯文本 OpenClaw 消息（如“不错”“很好”）错误携带上一条附件的问题。
- 修复真实引用文件消息因为右侧气泡缺少发送者字段而没有把附件发给 OpenClaw 的问题。
- 修复 workspace 兜底扫描把原始输入文件副本当成生成文件发送的问题。
- 修复中文标点紧跟文件名时，文件路径解析失败导致文件未发送的问题。

### 文档

- README 新增聊天示例、OpenClaw 文件处理链路、macOS 微信窗口操作注意事项和文件场景回归验证建议。

## [macOS-only] - 2026-05-03

### 新增

- **OpenClaw 本地 agent 双引擎模式**：普通 @ 消息走 LLM 秒回，带 `/claw` 或 `\claw` 前缀的 @ 消息自动转给本地 OpenClaw agent 处理（支持工具调用、搜索、代码执行等能力，耗时较长）。
- 新增 `OpenClawClient`：通过 `subprocess` 调用 `openclaw agent --local --json` 与本地 agent 交互，支持自定义 CLI 路径（`cli_path`）、超时控制和按群 session 隔离。
- 新增 `OpenClawConfig`：配置文件 `wx4py_ai_config.json` 新增 `openclaw` 节，支持 `enabled`、`agent_id`、`prefixes`、`timeout`、`fallback_to_llm`、`session_per_group`、`cli_path` 等选项。
- 新增 `HybridResponder`：双引擎路由调度器，根据消息前缀自动分流到 OpenClaw 或 LLM；OpenClaw 异常时自动降级到 LLM，保证服务不中断。
- OpenClaw JSON 输出 4 级容错解析：stdout JSON → stderr 混合 JSON 提取 → 结构化错误识别 → 兜底异常，兼容 OpenClaw 实际把 JSON 输出到 stderr 的行为。
- 新增示例脚本 `examples/messaging/reply_groups_with_openclaw.py`，演示双引擎接入方式。
- **引用消息识别**：群成员引用历史消息后 @ 机器人，AI 能识别被引用的原始内容和发送者，并针对被引用内容回答。支持从左侧会话列表预览和聊天窗口消息气泡两种途径提取引用信息。
- 新增 `_fetch_quote_from_chat` 方法，在左侧预览未包含引用内容时，临时打开聊天窗口读取消息气泡提取引用。
- `MessageEvent` 新增 `quoted_sender` 和 `quoted_content` 字段，AI 提示词中明确告知模型"被引用的内容已直接提供，不需要再去上下文里找"。

### 优化

- OpenClaw 按群 session 隔离：不同微信群使用不同 session ID（SHA256 前 16 位），防止上下文串群。
- OpenClaw 失败自动降级：调用超时或 agent 异常时，若 `fallback_to_llm=true` 则无缝降级到 LLM 秒回；若关闭 fallback 则返回配置占位语。
- `AIResponder._sanitize_reply` 支持 `max_chars=0` 表示不截断，OpenClaw 长回复不会被强制截断到 180 字。
- `_fetch_quote_from_chat` 性能优化：将固定 `sleep(0.8)` 改为 `sleep(0.3)` 配合重试机制，减少扫描线程阻塞时间。
- 避免重复切群：`_fetch_quote_from_chat` 点击切群后更新 `self._current_send_group`，发送流程可直接复用当前窗口。
- 读取聊天气泡时优先匹配发送者和内容，避免读到非目标消息的引用。
- 将内部诊断日志从 `info` 降级为 `debug`，减少日志噪音。

### 修复

- 修复 OpenClaw JSON 解析不支持真实 `payloads[0].text` 格式的问题，新增对 `payloads` 列表的提取逻辑。
- 修复 OpenClaw JSON 被输出到 stderr（stdout 为空）导致解析失败的问题，新增从 stderr 混合输出中提取第一个 `{` 到最后一个 `}` 的容错逻辑。
- 修复 `AIResponder._sanitize_reply` 中 `max_chars=0` 被 Python `or` 操作误判为 `180` 的截断问题，改为 `max_chars > 0` 才执行截断。
- 修复 `_parse_quote_from_text` 解析微信气泡 `name` 时 `sender` 被错误解析为包含回复前缀的混乱字符串的问题（新增 pattern0 专门匹配 `引用 X 的消息 : Y` 格式）。
- 修复切群发送时偶发的 AX 控件竞争卡顿问题：将 `GROUP_SWITCH_FALLBACK_ACCEPT_SECONDS` 从 `0.2` 秒增加到 `0.5` 秒，给 macOS 微信控件树足够重建时间。
- 修复 `_fetch_quote_from_chat` 中错误调用未定义函数 `_find_chat_message_list` 的问题（改为 `_find_message_list`）。

## [macOS-only] - 2026-05-02

本版本将项目主线重构为 macOS 微信群 @ AI 自动回复机器人，不再保留可运行的 Windows 后端。

### 破坏性变更

- 移除 Windows 后端、Win32/UIAutomation 运行依赖和隐藏到托盘恢复逻辑。
- 平台层只允许在 macOS 上运行，非 macOS 会给出清晰错误。
- README、Skill 和配置说明改为 macOS-only 机器人使用手册。

### 优化

- macOS 群监听固定使用左侧会话列表 `[有人@我]` 提示，不通过搜索框轮询群名。
- 自动回复进入串行队列，按群隔离上下文，并在发送后确认回复可见。
- 启动时从群审计日志恢复同群上下文，保留重启后的短期记忆。
- 关键错误额外写入按天错误日志，便于定位 AI 调用、切群和发送失败问题。

