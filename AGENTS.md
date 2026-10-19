# gpt-ds：给执行此仓库的 agent

本仓库把**目标 Codex 实例**配置成“当前主模型负责规划/审核，`deepseek_worker` 负责委派实现”。执行安装的 agent 可以来自任意 harness；目前适配的运行目标只有 Codex。读到本文件不等于安装授权：用户要求安装时才进入安装流程；开发、评审和运行测试只使用临时目录。

## 安装入口

先读 `INSTALL_AGENT.md`，再执行 `python3 configure.py discover`。该流程应由你完成，不要只向用户复制一份长教程。

关键不变量：

- 只写明确选定的一个 `CODEX_HOME`。多个实例时先确定目标；禁止逐个批量安装或擅自选择 `~/.codex`。
- 保留 Cockpit 实际启动参数、主模型、Provider ID、账号、密钥、MCP、权限和插件。子 agent 继承父 Provider，角色文件不切换 Provider；主 Provider 必须已绑定提供两种模型的 CPA 账号。
- 子角色固定 `deepseek-v4.1-flash` / `high`；上下文 1,000,000，最大窗口 1,000,000，自动压缩阈值 900,000。主模型窗口不变。
- 原生委派采用 V1：主、子目录条目为 `multi_agent_version=v1`，DeepSeek 为 `supports_search_tool=false` / `tool_mode=null`。
- 安装先预览，写入必须 `--apply`，由工具备份。活动目标实例应由用户手动退出；`--offline-confirmed` 仅用于已手动确认退出且自动检测不可用的情况。
- 不自动退出、杀进程、启动或重启。完成配置后明确告诉用户手动重启哪一个实例。
- 旧对话迁移仅限用户明确选择的 UUID；不扫描并改写全部历史。已正常的 V1 旧对话无需重迁移。重启后新建子 agent。
- 不上传/复制真实配置、模型指令目录、密钥、进程环境、会话、数据库或备份到此仓库。不打印完整配置、启动命令或环境。
- `install` 成功只代表磁盘配置完成；只有当前 Codex 原生委派成功，才报告原生链路验证通过。独立 CLI、HTTP 或文件检查不能替代它。

## 开发与验证

Python 3.11+，标准库，无 pip 依赖。运行 `python3 -m unittest discover -s tests -v`。测试仅使用临时样本，不操作真实 Codex 实例，不调用上游模型。代码变更后同步文档和 `gpt-ds.json`。不要为测试安装而修改执行 agent 自己的实例。
