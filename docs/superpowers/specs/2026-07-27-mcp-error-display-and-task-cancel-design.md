# MCP 错误详情展示 + 会话任务暂停按钮设计

日期：2026-07-27
状态：已确认（用户评审通过）

## 背景与目标

两个会话体验问题：

1. 执行流程面板中，MCP 工具调用失败的节点只显示白名单笼统文案（如「社媒数据服务返回错误，请稍后重试。」），而 DataTap 返回的具体错误信息（已脱敏落库）没有展示，用户无法判断失败原因。
2. 任务运行中无法停止：发送按钮在分析期间被禁用，用户只能等任务自然结束或失败。

目标：

- 失败节点在白名单文案下方追加展示 MCP 上游错误详情（已有脱敏数据，仅打通展示链路）。
- 输入框发送按钮改为图标按钮；任务运行中变为暂停图标，点击取消当前任务（协作式取消）。

### 已与用户确认的决策

- 错误详情：白名单文案保留，上游错误详情作为附加一行直接展示（不做折叠）。
- 暂停过渡态：点击后按钮立即禁用并呈「取消中」状态，等 SSE `task.cancelled` 事件收敛后恢复可输入（协作式取消，需等当前 MCP/模型调用结束）。

## 现状

- 上游错误原文已落库：`mcp_gateway/service.py` 的 `_finish_failed` 在 `result.is_error` 时把 `result.error_text` 经 `safe_upstream_text` 脱敏（含 http/bearer/token/api_key 等标记直接丢弃，否则归一化空白截断 300 字符）后写入 `mcp_calls.error_message` 与 `evidence_json["upstream_error_message"]`。该值目前只回喂模型（executor 拼进 EvidenceNote），不进 SSE。
- 工具失败事件：`executor.py:180-200` `build_tool_event_payload`，失败时 payload 只有白名单 `error_code` + `message`；调用点 `executor.py:788-802` 持有 `mcp_calls` 行（`row`）。事件经 `append_event` 落库，SSE 重放与实时同 payload。
- 前端失败节点：`src/state/taskEvents.ts:110-157` `withFlowNode` 在 `tool.failed/tool.unknown` 时把 `payload.message` 存为 `node.detail`；`src/components/TaskFlowNodes.tsx:33-37` 渲染 detail（failed 红色）。
- 取消已就绪未接线：`POST /api/v1/tasks/{task_id}/cancel`（`tasks/router.py:589-600` → `TaskService.cancel` 打 `cancel_requested_at` 标记，幂等，立即返回）；executor 每轮循环开头检查标记，边界处 `mark_cancelled` 并发 `task.cancelled` SSE。前端 `src/api/tasks.ts:55-57` 已有 `cancelTask(taskId)`，无调用方。
- 输入区：`ChatArea.tsx:463-490` 文字「发送」按钮（submit，analyzing 时禁用）；`Send` 图标已 import 未使用；`useWorkspace` 暴露 `activeTaskId`/`isAnalyzing`，无 cancel 方法。

## 设计

### 功能 1：失败节点展示 MCP 上游错误详情

**后端**（`backend/app/tasks/executor.py`）：

- `build_tool_event_payload` 增加可选参数 `upstream_message: str | None = None`；非空时 payload 加 `upstream_message` 键（值取自 `mcp_calls` 行 `evidence_json.get("upstream_error_message")`，调用点已有 `row`，仅需 dict 读取；缺失/非 str/空白 → 不带该键）。
- 白名单 `message` 与 `error_code` 完全不动（`errors.py`「绝不回显异常详情」的设计意图不破：上游原文已是 `safe_upstream_text` 脱敏产物）。
- 传输级失败（超时/熔断/连接错误）无上游原文，自然不带该字段；工具级失败（`upstream_tool_error`）有原文才带。

**前端**：

- `src/state/taskEvents.ts`：`FlowNode`（或等价类型）加 `upstreamDetail?: string`；`withFlowNode` 在 `tool.failed/tool.unknown` 时读取 `payload.upstream_message`（str 且非空才存）。
- `src/components/TaskFlowNodes.tsx`：`node.upstreamDetail` 非空时，在白名单 detail 行下方再渲染一行更小号的浅灰等宽文本（`text-xs text-slate-500 font-mono`，failed/unknown 共用相同样式，不与白名单文案混淆）。

### 功能 2：发送/暂停图标按钮

**`useWorkspace`**（`src/hooks/useWorkspace.ts`）：

- 新增 state `cancelling: boolean` 与方法 `cancelActiveTask(): Promise<void>`：
  - 无 `activeTaskId` 或已在 cancelling → 直接返回。
  - `setCancelling(true)` → `await cancelTask(activeTaskId)` → 成功不手动改任务状态（等 SSE `task.cancelled` 收敛：`taskRuntime.status` 变 `cancelled` → `isAnalyzing` 变 false）；`finally setCancelling(false)`。
  - API 失败：记 warning 并恢复（`cancelling` 复位，按钮回到 Pause），不抛出打断 UI。
- `App.tsx`：把 `cancelActiveTask` 与 `cancelling` 传入 ChatArea。

**`ChatArea.tsx`**：

- props 新增 `onCancelTask: () => Promise<unknown>`、`isCancelling: boolean`。
- 发送按钮改图标按钮（`aria-label` 三态：`发送`/`暂停`/`正在取消`）：
  - 非 analyzing：`type="submit"`，`Send` 图标，`disabled={!inputText.trim()}`，样式沿用 indigo。
  - analyzing 且非 cancelling：`type="button"`，`Pause` 图标（lucide-react），`onClick={() => void onCancelTask()}`。
  - cancelling：`Loader2` 旋转图标（或 Pause 灰化），disabled。
- textarea 的 analyzing placeholder 与建议 chips 禁用逻辑不变。

### 不做的事（YAGNI）

- 不改白名单文案体系（`errors.py`）、不改 `safe_error` 语义。
- 不做强制中断（kill 执行中的 MCP HTTP 请求）；保持协作式取消。
- 不做取消后自动重试/编辑重发。
- brainstorm 阶段无任务运行，不涉及暂停。

## 降级与失败语义

| 场景 | 表现 |
| --- | --- |
| 失败工具调用无上游原文（传输级） | 只显示白名单文案，与现状一致 |
| 上游原文被 `safe_upstream_text` 判为含敏感标记 | 不落库 → 事件不带 `upstream_message`，只显示白名单文案 |
| cancel API 失败（网络/409） | 按钮恢复 Pause，可再次点击 |
| 取消标记后任务恰已终态 | `TaskService.cancel` 幂等返回；SSE 终态事件正常收敛 |

## 测试策略

- 后端：
  - 工具级失败（is_error + upstream_error_message）事件 payload 带 `upstream_message`；传输级失败不带；`evidence_json` 无该键/空白时不带。
  - 事件重放（恢复路径）payload 同样携带（append_event 落库即天然一致，断言落库 payload）。
- 前端：
  - reducer：failed 事件存 `upstreamDetail`；无字段时节点无 upstreamDetail。
  - TaskFlowNodes：两行文案都渲染；无 upstreamDetail 时只有一行。
  - ChatArea：三态按钮（Send 可点/运行中 Pause/取消中禁用）；点 Pause 调 onCancelTask。
  - useWorkspace：`cancelActiveTask` 调 cancelTask API、cancelling 状态切换、无 activeTaskId 时 no-op、API 失败恢复。
