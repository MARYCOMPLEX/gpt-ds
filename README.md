# gpt-ds

**让 Codex 主 agent 保持当前模型负责规划、审核和流程控制，让原生子 agent 固定使用 CPA 的 DeepSeek。**

执行安装的 agent 可以来自 Codex、Claude Code、Gemini CLI 或其他能读文件和运行 Python 的 harness；**目前配置目标只支持 Codex**，不改变安装宿主自己的模型路由。

## 直接交给你的 agent

~~~text
请拉取 https://github.com/MARYCOMPLEX/gpt-ds.git，阅读 AGENTS.md 和 INSTALL_AGENT.md，
按仓库流程为我选定的 Codex 实例完成安装，而不是只解释步骤。
保留 Cockpit 实际启动参数、主模型、Provider 账号、MCP 和权限；只改一个目标实例。
子 agent 固定 deepseek-v4.1-flash / high / 1M，上下文压缩阈值 900000。
先探测与预览，备份后配置；多个实例时先确认目标。不自动重启。
旧对话只迁移我指定的 UUID。最后报告磁盘配置、重启步骤和原生验收状态。
~~~

拉取后也可直接说：**“按 INSTALL_AGENT.md 安装到我指定的 Codex 实例。”**

克隆本身不执行安装、不注册 Git hooks、不修改系统。`AGENTS.md` 是通用入口，`CLAUDE.md` / `GEMINI.md` 是入口提示；未自动读取这些文件的 harness 请明确要求其阅读 `INSTALL_AGENT.md`。

## 最终效果

~~~text
Cockpit 当前启动模型 / Provider
              │
              ▼
     Codex 主 agent：规划、审核
              │ 原生 V1 委派，带完整任务正文
              ▼
  deepseek_worker：deepseek-v4.1-flash / high
              │
              ▼
       继承同一个 CPA Provider
              │
              └── 返回结果，由主 agent 审核
~~~

| 项目 | 安装行为 |
| --- | --- |
| 主模型 | 保持 Cockpit / 当前会话的实际选择，不强写成某个 GPT 型号 |
| 主 Provider 与认证 | 保留；父、子共用能提供两种模型的 CPA 账号 |
| 子角色 | `deepseek_worker` |
| 子模型 / 思考强度 | `deepseek-v4.1-flash` / `high` |
| 子上下文 / 最大窗口 | `1000000` / `1000000` tokens |
| 自动压缩阈值 | `900000` tokens |
| 有效预算 | 95% 规则下约 `950000`，以重启后运行记录为准 |
| 委派兼容协议 | 主、子目录条目均为 V1；DeepSeek 搜索工具关闭，`tool_mode=null` |
| 改动范围 | 一个明确指定的 `CODEX_HOME`，不改全局注册表或其他实例 |
| 重启 | 用户手动操作；工具从不自动重启 |

子 agent 继承父 Provider。**在角色文件中写另一个 `model_provider` 并不会切换上游。**因此先在 Cockpit 给目标实例绑定 CPA 账号；工具不替你更换账号或传输密钥。[官方 OpenAI 子 agent 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)

## 前置条件

- Python **3.11+**；只用标准库，没有 pip 依赖。
- 目标是使用 `model_catalog_json` 和独立 `agents/*.toml` 角色文件的本地 Codex 实例。
- 目标实例已在 Cockpit 绑定 CPA，使用 Responses API，能提供主模型与 `deepseek-v4.1-flash`。
- 该设备当前 Codex 版本的**完整原生模型目录**，包含 `slug`、`base_instructions` 等字段。工具优先读取实例已配置的目录，否则读取 `models_cache.json`。`/v1/models` 的 ID 列表不适用。

## 手动速查

~~~bash
git clone https://github.com/MARYCOMPLEX/gpt-ds.git
cd gpt-ds
python3 configure.py discover

# 按实际设备设置；不要复制别人的实例哈希。
INSTANCE_DIR="/你的设备/目标Codex实例目录"
MAIN_MODEL="gpt-6.1-sol"  # 填 Cockpit 实际启动模型，并非强制指定此型号

# 仅预览。源目录无 DeepSeek 时，明确用现有主模型条目复制格式。
python3 configure.py --instance "$INSTANCE_DIR" plan \
  --main-model "$MAIN_MODEL" --worker-template "$MAIN_MODEL"

# 用户手动完全退出目标实例后才应用。
python3 configure.py --instance "$INSTANCE_DIR" install \
  --main-model "$MAIN_MODEL" --worker-template "$MAIN_MODEL" --apply
~~~

Windows 可用 `py -3` 或 `python` 替代 `python3`；设置实际的 Windows 路径。自动检测不了进程状态时，在用户已手动退出目标实例后加 `--offline-confirmed`。它不会绕过已检测到的活动进程。

如果 Cockpit 选择独立 profile 文件，使用实际生效的 `--config-file PROFILE.config.toml`；若启动参数覆盖模型，用 `--main-model` 传入实际值，它只用于准备对应主模型的 V1 条目，不修改启动参数或根 `model`。内嵌 profile 或启动参数覆盖了目录/功能项时，安装 agent 应先核对覆盖关系，不将根文件的成功写入当成实际生效。

## 会自动配置什么

安装前统一预览；`install --apply` 在目标实例内备份，并写入：

1. 选定配置文件：指向独立 `models.gpt-ds-v1.json`，开启兼容的多 agent 设置与 DeepSeek 默认子模型。
2. 独立模型目录：登记准确的 DeepSeek ID、1M 窗口、900k 压缩阈值与 V1 能力；保留主模型原有窗口和其他模型条目。
3. `agents/deepseek_worker.toml`：固定角色模型与 `high`；已有自定义角色指令会保留。
4. 实例级 `AGENTS.md` 中的托管区块：主 agent 规划、明确任务正文、调用角色、等待结果与最终审核。已有 `AGENTS.override.md` 时修改其托管区块，保留区块外的指令。

不写账号、密钥、MCP、权限、插件或 SQLite。文件写入失败时回滚已写文件；重复安装无差异时不改文件。备份可能含原配置密钥，留在本机，不提交或分享。

## 怎么生效

1. 用户在 Cockpit **手动完全退出并重开同一实例**，其他实例保持原样。
2. 原来已正常的 V1 父对话可以继续；**新建** `deepseek_worker`，不复用改动前的子 agent。
3. 在目标 Codex 对话里执行 [原生自检提示词](templates/native-self-check.md)。仅查看文件、独立 CLI 或 HTTP 成功不等于原生委派通过。
4. 拿到父子 UUID 后只读核验：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" verify \
  --parent-thread PARENT_UUID --thread CHILD_UUID
~~~

`verify` 要求原生父子来源关联、正确模型 / high / V1 / Provider、任务完成、精确 `RESTART_OK`、子 agent 无工具调用，以及新加载的百万级窗口。只读取记录，不调用模型。日志结构不匹配会报告验证未完成，不会自动改写历史。

## 旧对话、回滚和开发

- [安装 agent 的完整执行流程](INSTALL_AGENT.md)：实例识别、自动配置、失败处理与报告格式。
- [原理与兼容性](docs/design.md)：为什么 V1、模型目录和旧对话分别处理。
- [旧对话迁移 / 回滚 / 排障](docs/troubleshooting.md)：只处理明确的 UUID，保留消息与后续历史。
- [机器可读清单](gpt-ds.json)：目标、默认值、不变量与验证状态。

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" status
python3 -m unittest discover -s tests -v
~~~

测试只使用临时样本。仓库提供 macOS / Linux / Windows、Python 3.11–3.13 的 CI 测试矩阵；不代表在每种平台上做过真实 CPA 原生验收。本方案来自 macOS 的 Codex `0.162.0-alpha.17.2` 成功委派与 `0.163.0-alpha.10.2` 配置核对；2026-10-19 将客户端窗口调整为 1M。升级后按原生自检重新验收，不随仓库分发本机的完整模型指令目录。
