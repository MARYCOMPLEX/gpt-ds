# 旧对话、回滚与排障

## 旧对话还收不到正文

只改配置并重启不迁移旧对话保存的协议。用户明确选择一条需要继续的对话后：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" migrate --thread UUID
# 用户手动退出目标实例后：
python3 configure.py --instance "$INSTANCE_DIR" migrate --thread UUID --apply
~~~

多条明确目标可重复 `--thread`。工具优先依据 SQLite 当前索引找到 rollout 文件，不猜同 UUID 的多个快照；有多份数据库时明确 `--database`。数据库只读，不写索引。

迁移只将 `session_meta`、`turn_context` 和已知压缩恢复元数据里的 `multi_agent_version=v2` 改为等长 `v1`。保留消息与密文、ID 和字节长度。出现额外未知协议布局则停止。

用户手动启动后，可以继续这条父对话，但必须新建 worker。重启不等于迁移其他所有旧对话；不复用原来失败或预算未更新的子 agent。

## 回滚

使用本工具该次输出的备份目录，先预览：

~~~bash
python3 configure.py --instance "$INSTANCE_DIR" restore \
  --backup "$INSTANCE_DIR/repair_backups/cpa-guide-时间戳"
# 用户手动退出目标实例后加 --apply：
python3 configure.py --instance "$INSTANCE_DIR" restore \
  --backup "$INSTANCE_DIR/repair_backups/cpa-guide-时间戳" --apply
~~~

安装 / 迁移均保存前后 SHA-256。目标文件之后有任何变化时，恢复会先停止，保留后续配置或对话；不要强行恢复旧 rollout 丢掉新历史。配置被 Cockpit 再生成也属于后续变化，需安装 agent 人工核对差异。

备份可能包含原配置凭据和会话正文，留在目标实例内。不要提交仓库、上传排障或混用别的脚本备份格式。

## 常见症状

| 症状 | 核对与处理 |
| --- | --- |
| 子 agent 只有“work on the delegated task”，随后扫描工作区 | 先查父/子协议是否仍 V2、是否空 Payload；迁移指定旧父对话，手动重启后新建 worker |
| 一个旧对话正常，另一个失败 | 每条旧对话保存状态独立，只处理用户明确指定的第二条 |
| Unknown model / 没有可用模型映射 | 完整目录要有准确 ID `deepseek-v4.1-flash`；不是 `/v1/models` 列表；启动确实加载本实例生成的目录 |
| 日志仍显示 121600 或界面显示 128k | 磁盘值与已加载值分开；确认手动重启的是同一实例，并新建子 agent |
| 1M 配置日志显示 950000 | 正常的 95% 有效预算规则；压缩阈值为 900000 |
| 角色写了 Provider，实际上还是父 Provider | 角色不切 Provider；先让父实例绑定包含两种模型的 CPA 账号 |
| 显示 `codex_local_access` | 这是名称，不是地址；查其实际主机名，不因名称而改 Provider ID |
| 检测到实例仍活动 | 请用户只退出该实例；`--offline-confirmed` 不覆盖已检测到的活动进程 |
| TOML 内联表 / 点号布局导致预览停止 | 安装 agent 先备份并整理托管表，校验语义不变，再重跑；不全局替换或覆盖原配置 |
| 配置窗口存在全局覆盖 | 不直接改成 1M；确认后保留主预算到主模型条目，再去掉会影响两者的根级覆盖 |
| 磁盘安装成功但当前聊天没变化 | 目录启动时加载；profile、项目配置或启动参数可能覆盖根文件；核对实际启动选择与当前父对话 |
| HTTP / 独立 CLI 成功，原生失败 | 不是同一测试；用目标 Codex 对话原生新建 worker，再读取父子证据 |
| `verify` 未完成 | 查输出的 false 项，不把文件配置当运行验证；新日志格式不适配时保留记录，更新解析器后再验收 |
| 升级后失效 | 用新版本本机原生目录重跑预览，检查特性与协议语义，手动重启并做原生自检 |

## 数据处理边界

安装工具不请求 CPA、不发送密钥、不安装代理、不执行自动重启。`verify` 只读取用户给定的父子 UUID，不输出原始消息；回复错误时仅输出布尔检查失败，不泄露意外选中会话的正文。
