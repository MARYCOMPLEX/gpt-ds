# 原理与兼容性

## 目标和边界

这里适配的是 **Codex 的原生多 agent 流程**。安装宿主只需要读文件和运行 Python：其他 harness 能执行配置，不代表本仓库为它们实现了 DeepSeek 子任务适配器。

主模型保持 Cockpit 实际选择。角色固定 DeepSeek，并继承父会话 Provider、权限与 MCP。Provider ID 只是本机配置名称，判断路由应看其实际地址；名称中含 `local` 不证明请求走 localhost。

## 为什么共用 CPA Provider

当前 Codex 自定义角色允许设定模型、思考强度与指令，但角色里的 `model_provider` 不覆盖父会话。这套已验证方案是“主、子共用包含两种模型的 CPA 账号”，不是从另一个父 Provider 单独跳转子 Provider。[官方 OpenAI 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)

工具保留账号和认证，不发送网络请求，不获取或复制密钥。默认只核对主机名 `cpa.gojia.cloud` 与 Responses 配置；自建兼容端点必须显式传 `--expected-provider-host HOST`。文件核对不等于上游请求能力认证。

## 为什么使用 V1

本机失败时，原生 V2 将任务正文放进 `agent_message.encrypted_content`；CPA→DeepSeek 的路径没有把该正文转为子模型可见内容，子 agent 只得到空 `Payload:` 和固定角色指令。切换 V1 后，委派正文可读，原生 `RESTART_OK` 回路通过。

兼容处理同时覆盖三层：

1. 实例配置：启用多 agent，关闭 V2 特性开关。
2. 模型目录：当前主模型与 DeepSeek 均为 `multi_agent_version=v1`；DeepSeek 的 `supports_search_tool=false`、`tool_mode=null`，使用普通 Responses 路径。
3. 旧对话：仍持久化 V2 时，按用户选择的 UUID 等长迁移已知元数据。

这是特定版本与链路的兼容方案，不是所有外部 Provider 均如此的承诺。复现背景见 [Codex Issue #36586](https://github.com/openai/codex/issues/36586) 和 [Issue #33551](https://github.com/openai/codex/issues/33551)。

## 为什么不分发完整模型目录

`/v1/models` 只列出上游 ID；Codex 的原生目录还包含指令、工具能力等字段。仓库只携带变换逻辑，由目标设备自己的完整目录生成独立文件，避免把某台设备或旧版本的模型指令硬塞给新设备。

源目录中没有 DeepSeek 时，安装 agent 明确选定一个现有条目作为格式模板，再设置 DeepSeek 的模型名和所需能力。复制格式不创建上游模型，也不证明它继承模板模型的所有能力。源目录已有 DeepSeek 时，只更新该子模型的托管字段。

目录在启动时加载，因此改完后由用户手动重启指定实例，并创建新子 agent。[官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

## 1M 和运行预算

- 模型目录声明：`context_window=1000000`、`max_context_window=1000000`。
- 压缩触发阈值：`auto_compact_token_limit=900000`。
- 子模型有效预算百分比：95；运行记录通常报告 `950000`，用于保留预算余量。
- 主模型窗口不变；不往根配置写 1M，避免把主、子模型一起覆盖。

`status` 只核对磁盘状态；`verify` 查阅新原生子线程的运行记录。改目录不会追溯扩大旧子 agent 的已加载预算，也不会恢复已经压缩的消息。

## 主模型选择和版本变化

工具不把主模型固定成 GPT 的某个版本。启动参数或当前会话选择与根配置不同时，传 `--main-model` 指定真实主模型，仅对其目录条目应用 V1。切换到另一个尚未处理过的主模型后，应重新预览配置并验收，不能假定它自动继承前一个模型的协议条目。

这套协议字段属于运行时兼容层，升级可能改变其语义。保留备份，使用新版本生成的原生目录，运行临时样本测试，再做原生自检。失败时优先检查模型映射、角色、协议、父对话保存状态和实际运行记录，不修改二进制或伪造成功结果。
