以下路由规则适用于根主 agent；子 agent 按自己的角色直接完成委派任务，不把原任务重新转派给同名角色。
本对话的主 agent 使用 Cockpit 当前选择的模型与 Provider，负责规划、拆分任务、审阅结果和最终验收。
需要委派实施时，显式调用 deepseek_worker；该角色使用 deepseek-v4.1-flash，思考强度 high。
把具体目标、涉及文件、允许的修改范围和验收标准写入委派正文，不要只写“继续”或“work on the delegated task”。
先完成一次不操作文件的路由自检，再委派实际任务。
自检时只让子 agent 返回指定字符串；等待并核对原始返回内容。
如果子 agent 报告任务正文缺失，停止重复派发，检查父对话与子 agent 的协议版本。
配置迁移后重新创建子 agent，不复用此前 V2 子 agent。
