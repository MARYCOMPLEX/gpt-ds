请执行当前 Codex 对话的原生路由自检：

1. 主模型与 Provider 保持 Cockpit 实际选择，不修改配置。
2. 显式创建新的 deepseek_worker（deepseek-v4.1-flash / high）。
3. 委派正文：只精确返回 RESTART_OK，不调用任何工具、不操作任何文件。
4. 等待子 agent 结束，核对它的原始回复。报告父对话 UUID 和新子对话 UUID。
5. 核对运行记录中的模型、high、V1、父子 Provider 与有效上下文窗口（1M 配置通常为 950000）。

不要用独立 codex exec、HTTP 请求、其他 harness 的子任务或仅查看磁盘配置代替当前对话的原生委派。若没有原生工具，明确报告“配置完成，待用户在 Codex 内验收”，不要标记原生链路通过。
