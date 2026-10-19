# 给安装 agent 的执行流程

目标：由你识别并配置**一个 Codex 实例**，而不是要求用户逐行手工粘贴。目标运行时只支持 Codex；你的宿主 harness 不限。先读 `AGENTS.md` 和 `gpt-ds.json`。以下目录均为目标设备自己的目录，切勿复制来源设备的用户名、实例哈希或凭据。

## 1. 探测并选择目标

~~~bash
python3 --version
python3 configure.py discover
~~~

要求 Python 3.11+。`discover` 只列出 `CODEX_HOME`、默认 Codex 目录和常见 Cockpit 实例目录，不安装、不自动选中。自定义位置用 `--instance` 明确传入。

- 用户明确指定的实例优先。
- “当前 Codex 实例”可结合当前环境的 `CODEX_HOME` 和 Cockpit 的实际启动配置判断；在其他 harness 中执行时不要把自己的配置目录当成目标。
- 多个候选且意图不明确时，只问“配置哪一个实例 / CODEX_HOME？”，不要遍历写入。
- 没有候选时询问路径，或让用户先在 Cockpit 创建目标 Codex 实例。不要为了得到成功结果创建一个实际上未被使用的假目录。

## 2. 核对实际启动选择与前置条件

读取目标配置，仅提取必要字段，不打印完整文件或进程环境：

- Cockpit 实际启动模型；有 `--model` 或会话选择覆盖时记住真实值。
- 当前 Provider ID、对应主机名、`wire_api=responses`，以及账号已由 Cockpit 绑定。默认期望主机为 `cpa.gojia.cloud`。
- Codex 当前生效的配置文件、profile、`model_catalog_json` 路径；全局 / 项目 / 启动参数存在覆盖时，先判断真正生效的一层。
- 当前版本的完整模型目录；缺少时让用户先正常启动一次以生成缓存，或选择本机已有原生完整目录。禁止使用 `/v1/models` 的模型 ID 列表伪造完整目录。
- 若父 Provider 还不是包含主、子模型的 CPA，告诉用户先在 Cockpit 绑定账号。**不要**向仓库上传凭据，不另建 localhost 代理，不把新 Provider 写进角色冒充独立路由。

工具默认操作实例根 `config.toml`。Codex 选择独立 profile 文件时，可用全局参数 `--config-file NAME.config.toml`；工具读取根配置与该文件的覆盖关系，只改选中的配置文件。不要借此擅自改 Cockpit 的 profile 选择。旧式内嵌 profile、项目配置或启动参数覆盖目录/功能开关时，先核对其影响；工具不能证明未传入的启动覆盖已生效。

存在根级 `model_context_window` / `model_auto_compact_token_limit` 时，工具停止自动合并，以免将主模型预算一起改成 1M。你应向用户说明冲突，在确认后将主模型预算保留到它自己的目录条目，再去掉相应全局覆盖并重跑预览；不要直接删除值或改变主模型窗口。

## 3. 生成完整预览

以下示例中的变量由你按实际环境设置。`MAIN_MODEL` 既可作为当前主模型，也可在目录缺少 DeepSeek 时作为格式模板；必须确实存在于源目录。复制格式不会在上游创建模型。

~~~bash
INSTANCE_DIR="/目标设备/目标Codex实例目录"
MAIN_MODEL="实际启动的模型ID"
python3 configure.py --instance "$INSTANCE_DIR" plan \
  --main-model "$MAIN_MODEL" --worker-template "$MAIN_MODEL"
~~~

优先从配置的目录读取源，否则读取 `models_cache.json`。自动选择失败时加 `--source /完整/原生模型目录.json`。`--main-model` 不修改根模型或启动参数。默认子模型固定 `deepseek-v4.1-flash` / `high`；已有 DeepSeek 条目也更新到 1M。

核对预览：

- `instance` 是选定目标；`main_model_for_catalog` 是真实主模型；`provider_host` 匹配目标账号的地址。
- `files_to_change` 只落在目标实例。主模型窗口、Provider、认证、权限、MCP、插件和历史均保留。
- 源目录可来自共享只读位置，生成的模型目录必须是本实例独立文件。
- 只会写所选配置、独立目录、worker 角色、实例指令托管区块；不将本仓库路径写成运行依赖。

有 TOML 结构冲突时，在**写入前**停止并给出具体冲突；若要整理内联表 / 点号写法，先备份并证明语义保持，再重新预览，不用字符串全局替换。

## 4. 自动应用，保留手动重启

目标实例仍活动时，请用户手动完全退出这一实例。不要关闭其他实例、不要杀进程，不要伪造退出确认。若你正在目标 Codex 本身运行，先报告已完成预览；用户退出后应在另一个 harness / 终端应用，或按用户后续指示继续。

确认退出后执行与预览相同的参数，加 `install --apply`：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" install \
  --main-model "$MAIN_MODEL" --worker-template "$MAIN_MODEL" --apply
~~~

`--source`、`--config-file` 等预览参数有使用时，应用时也应保持一致。Windows 或自动进程检测不可用的平台，在用户确实已退出目标实例后加 `--offline-confirmed`；检测到活动目标进程时此参数不会强行写入。

工具在实例 `repair_backups/cpa-guide-时间戳/` 内备份原文件、存储前后 SHA-256，再原子写入。它只改与目标不同的文件。安装成功是 `configured_pending_manual_restart`，不是原生链路已验收。

## 5. 可选：只迁移指定旧对话

用户需要继续某条仍保存 V2 的旧对话时，在退出状态下按其明确的 UUID 操作：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" migrate --thread UUID
python3 configure.py --instance "$INSTANCE_DIR" migrate --thread UUID --apply
~~~

可重复 `--thread` 指定多条；不要自动枚举全量历史。工具只改已知运行元数据的 `v2` 为等长 `v1`，保留消息正文、密文、ID 和文件长度，不改数据库。已有多份数据库时用 `--database state_实际编号.sqlite` 指定。异常存储结构应停止，不猜文件或重写正文。

只将窗口从 128k 调整到 1M，不需要重新迁移已经正常的 V1 对话。迁移不重放之前丢失的任务，不恢复已压缩的全文。

## 6. 告诉用户手动生效，并做真正验收

请用户在 Cockpit 启动**同一个实例**。可以继续原来的 V1 父对话，但要新建 worker，不复用旧子 agent。

进入目标 Codex 对话后，使用 `templates/native-self-check.md`：保持主模型、显式创建 `deepseek_worker`，委派只回复 `RESTART_OK`、无工具调用的任务。等待结束，取得真实父子 UUID，再执行：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" verify \
  --parent-thread PARENT_UUID --thread CHILD_UUID
~~~

主模型被启动参数覆盖时同时加 `--main-model 实际模型`；独立配置或自建兼容主机也要传入与安装相同的参数。`verify` 只读当前索引指向的运行记录，校验来源父子关联、模型、高强度、V1、Provider、完成事件、原始回复和有效预算。

如果你的宿主没有目标 Codex 的原生委派工具，提供该提示词让用户在 Codex 中验收；不要用你的宿主子任务、HTTP 请求或另起 `codex exec` 冒充通过。证据不足时保持“配置完成、原生验证待进行”。

## 7. 最终报告

简洁报告：目标实例、实际主模型 / Provider ID、worker 模型和 high、1M / 900k、改动文件、备份路径、是否需要迁移指定旧对话、用户应手动重启什么、原生验证状态。

禁止输出密钥或完整配置。禁止将“文件安装成功”写成“当前对话已原生验证通过”。不要在没有用户要求时上传运行记录、数据库或备份。
