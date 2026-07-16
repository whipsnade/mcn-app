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

事件 payload 只包含 `phase`、`label`、`platform`、`step_index`、`step_total`、安全错误码和用户可读错误说明等字段。前端状态保存 `phase`、`phaseLabel`、`phaseProgress`、`errorMessage` 和 `errorMessageId`，不保存工具原始 payload。

### 2. 错误持久化

任务失败或部分渠道失败时，后端生成一条带 `task_id` 的 assistant 消息，并在终态事件中返回消息 ID 与脱敏后的错误说明。页面实时追加该消息；刷新或恢复会话时由消息列表重新加载，因此错误不会丢失。

### 3. 用户消息重跑

任务创建时，将任务 ID 写入触发用户消息的 metadata，并维护该消息关联的任务 ID 列表。新增 `POST /api/v1/tasks/{task_id}/retry`：校验任务归属和终态，复用原用户消息，不新增重复用户文本，创建同一会话的新任务并提交 runner。新任务拥有自己的 SSE 流、候选版本、BI 报告和 AI 总结。

前端在带有关联任务 ID 且任务已结束的用户消息上显示“再次执行”。点击后切换到新任务流，清空当前任务的临时 BI/候选引用，但不删除历史消息或旧报告。

## 文件边界

- 后端任务状态/事件：`backend/app/tasks/state.py`、`repository.py`、`executor.py`、`service.py`、`router.py`、`schemas.py`
- 后端会话消息关联：`backend/app/workspace/router.py`、`workspace/service.py`、`workspace/schemas.py`
- 前端事件状态：`src/state/taskEvents.ts`、`src/hooks/useTaskStream.ts`、`src/hooks/useWorkspace.ts`
- 前端消息与阶段 UI：`src/types.ts`、`src/components/ChatArea.tsx`、`src/App.tsx`
- API 契约：`src/api/tasks.ts`、`src/api/contracts.ts`

## 验证

- 后端单测覆盖阶段事件、终态错误消息、重跑权限/并发限制和消息 metadata。
- 前端单测覆盖真实事件到阶段标签的映射、错误消息显示、重跑按钮仅对终态任务可用，以及重跑后旧结果隔离。
- 运行完整后端 pytest、前端 Vitest、TypeScript 检查和生产构建。
