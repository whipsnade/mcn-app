# 任务阶段事件与会话重跑设计

## 目标

让会话页面实时展示后台真实任务事件，明确当前执行到了哪个阶段；任务失败时在会话中显示安全、可理解的错误；用户可以从已发送的用户消息重新执行同一分析，而不覆盖历史消息和旧任务结果。

## 约束

- 前端不得根据计时器或固定示例推测后台阶段。
- 阶段展示必须来自后端实际持久化并通过 SSE 推送的 `TaskEvent`。
- 事件中不得包含 MCP 原始响应、密钥、接口地址或内部连接信息。
- 重跑必须在同一会话创建新 `AnalysisTask`，保留原消息、旧任务和旧报告。
- 只允许对已结束任务重跑；运行中、规划中、被中断且可恢复的任务不能重复提交。

## 方案

### 1. 真实事件驱动

后端在任务实际边界产生并持久化 canonical 事件，前端仅根据事件 reducer 更新状态。当前 `McpAccounting`/`McpGatewayService` 已有 `mcp_call_settled`、`mcp_call_released`、`mcp_call_unknown` 等内部账务事件；这些事件不能直接暴露给前端。执行器需要在 MCP 调用开始、成功、失败、unknown 的边界追加安全的 `tool.started`、`tool.succeeded`、`tool.failed`、`tool.unknown` canonical 事件，并将账务事件映射为阶段进度。所有 SSE 事件都必须经过同一个持久化事件仓库，支持 `Last-Event-ID` 重放。

确定性映射规则如下：

| 内部事实 | canonical 事件 | 阶段 | 对外字段 |
| --- | --- | --- | --- |
| 任务创建 | `task.pending` | `accepting_data` | `label`、`status` |
| 计划保存 | `plan.ready` | `ai_analysis` | `label`、`status` |
| MCP 调用开始 | `tool.started` | `mcp_query` | `platform`、`step_index`、`step_total` |
| MCP 成功并结算 | `tool.succeeded` | `mcp_query` | `platform`、进度 |
| MCP 失败并结算 | `tool.failed` | `mcp_query` | `platform`、allowlist 错误码/文案 |
| MCP 结果不确定 | `tool.unknown` | `mcp_query` | `platform`、安全错误码/文案 |
| 账务释放 | 不单独改变阶段，仅更新当前事件的 `billing_status` | 当前阶段 | `billing_status` |
| 候选/BI 持久化 | `candidates.updated` / `bi.updated` | `ai_summary` | 版本、进度 |

平台只能来自 `xiaohongshu`、`douyin`、`bilibili`、`weibo`、`wechat` 白名单，并转换为中文展示名；canonical payload 不出现内部工具完整名、logical call ID、MCP call ID 或服务 slug。并行 MCP 批次的 `step_total` 是该批次实际命令数，`step_index` 按已收到的 success/failed/unknown 结果累计，前端按平台聚合状态。

后端在任务实际边界产生并持久化事件，前端仅根据事件 reducer 更新状态：

| 事件 | 真实发生位置 | 前端用途 |
| --- | --- | --- |
| `task.pending` | 创建任务事务 | 接受数据 |
| `plan.ready` | 计划保存成功 | AI 分析 |
| `replan.ready` | 失败后重新规划成功 | 重新规划 |
| `tool.started` | MCP 批次实际开始 | MCP 查询中 |
| `tool.succeeded` | 单个 MCP 调用成功 | 更新平台进度 |
| `tool.failed` / `tool.unknown` | 单个 MCP 调用失败或结果不确定 | 显示失败平台和错误 |
| `candidates.updated` | 候选版本持久化完成 | AI 汇总/候选更新 |
| `bi.updated` | BI 报告持久化完成 | AI 汇总/BI 更新 |
| `message.delta` / `message.completed` | AI 总结流写入消息 | 展示总结生成进度 |
| `task.completed` / `task.completed_with_warnings` | 任务终态提交 | 完成或部分完成 |
| `task.failed` / `task.cancelled` | 任务终态提交 | 错误或取消 |

事件 payload 只包含 `phase`、`label`、`platform`、`step_index`、`step_total`、安全错误码和用户可读错误说明等字段；不得包含 `logical_call_id`、MCP 调用 ID、内部工具完整名、原始响应、诊断路径、密钥或接口地址。前端状态保存 `phase`、`phaseLabel`、`phaseProgress`、`errorMessage` 和 `errorMessageId`，不保存工具原始 payload。

### 2. 错误持久化

任务失败或部分渠道失败时，后端通过中心化的错误码白名单映射生成用户文案（限制长度和字符集），生成一条带 `task_id` 的 assistant 消息，并在终态事件中返回消息 ID 与脱敏后的错误说明。不得把异常原文、MCP diagnostic/raw payload、密钥或地址写入 Message、task.error 或事件。页面实时追加该消息；刷新或恢复会话时由消息列表重新加载，因此错误不会丢失。前端以 `message_id` 做幂等，SSE 重连不会重复插入。

### 3. 用户消息重跑

任务创建时，在同一事务中保留原有 `scoring_profile` 并将任务 ID 追加到触发用户消息 metadata 的 `analysis_task_ids` 列表，同时更新 `latest_analysis_task_id`。新增 `POST /api/v1/tasks/{task_id}/retry`：校验任务归属和终态（`completed`、`completed_with_warnings`、`failed`、`insufficient_balance`、`cancelled` 可重跑），复用原用户消息，不新增重复用户文本，创建同一会话的新任务并提交 runner。通过行锁和 active-task 查询保证双击/并发 retry 最多创建一个 active retry；运行中、规划中和可恢复的 interrupted 任务返回冲突。新任务拥有自己的 SSE 流、候选版本、BI 报告和 AI 总结。

前端在带有关联任务 ID 且任务已结束的用户消息上显示“再次执行”。点击后切换到新任务流，立即清空当前任务的临时 BI/候选引用，直到新任务发出匹配的新版本事件；不显示旧 task 的 BI/候选结果，不删除历史消息或旧报告。过期 SSE、重复终态事件和重连重放由 reducer 按 task ID、事件 ID、message ID 幂等处理。

候选和 BI 查询接口必须显式按 `task_id` 及候选/报告版本查询，禁止在新任务未产出版本时回退到会话最新旧版本；前端缓存键包含 task ID 与版本。SSE 的 `Last-Event-ID` 只在同一 user + task 作用域内使用，服务端按单调自增事件 ID 排序回放；跨用户或跨 task 的事件访问返回 404，终态事件也必须可被重放一次。

重跑使用 `retry_key = source_message_id + retry_generation`，在任务表增加唯一约束或独立幂等表，以原子插入确保并发请求最多创建一个 active retry；重复请求返回已经存在的任务 ID。错误 assistant 消息使用 `task_id + error_code` 唯一幂等键，runner 恢复或重复终态事件只能复用同一 message ID。

## 文件边界

- 后端任务状态/事件：`backend/app/tasks/state.py`、`repository.py`、`executor.py`、`service.py`、`router.py`、`schemas.py`
- MCP 事件映射：`backend/app/mcp_gateway/accounting.py`、`backend/app/mcp_gateway/service.py`，必要时补充事件契约迁移
- 查询隔离与 retry 幂等：`backend/app/reporting/router.py`、`backend/app/reporting/service.py`、`backend/app/tasks/models.py` 及对应 Alembic migration
- 后端会话消息关联：`backend/app/workspace/router.py`、`workspace/service.py`、`workspace/schemas.py`
- 前端事件状态：`src/state/taskEvents.ts`、`src/hooks/useTaskStream.ts`、`src/hooks/useWorkspace.ts`
- 前端消息与阶段 UI：`src/types.ts`、`src/components/ChatArea.tsx`、`src/App.tsx`
- API 契约：`src/api/tasks.ts`、`src/api/contracts.ts`

## 验证

- 后端单测覆盖事件顺序与 `Last-Event-ID` 重放/跨 task 拒绝、MCP success/failure/unknown 阶段映射、终态错误消息持久化幂等、敏感数据不泄漏、重跑权限/并发/数据库幂等和 metadata 保留、查询按 task/version 隔离、多平台并行进度（实际 step_total 与各平台 success/failed/unknown）。
- 前端单测覆盖真实事件到阶段标签的映射、错误消息显示及 message_id 幂等、重跑按钮仅对终态任务可用，以及重跑后旧结果隔离、过期事件和重复终态事件。
- 运行完整后端 pytest、前端 Vitest、TypeScript 检查和生产构建。
