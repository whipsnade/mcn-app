# 思考持久化死锁导致幽灵任务 修复设计

日期：2026-07-27
状态：已确认（用户要求修复）
前置：changelog/2026-07-27.md（模型思考流）、changelog/2026-07-26.md

## 背景与事故

2026-07-27 开发库实测发现会话卡死「分析中」：

1. brainstorm 画像 ready 后在**请求事务内** inline 创建 agent 任务；
2. 随后的思考块持久化（`ThinkingMessageStore._messages` 对整个会话 messages 做
   `SELECT ... FOR UPDATE`）与并发写路径（在跑的 agent loop 的思考持久化）互锁，
   触发 MySQL 1213 死锁（日志出现 3 次）；
3. InnoDB 将死锁牺牲事务**整体回滚**——inline 任务创建被静默撤销；
4. 异常被「思考持久化尽力而为」的 try/except 吞掉，代码继续执行，后续写入在新隐式
   事务中提交成功 → **HTTP 200 返回了已被回滚的 task_id（幽灵任务）**；
5. 前端对该 task_id 轮询 `/tasks/{id}/events` 永远 404，无限重试，会话永远「分析中」。

数据库实证：brainstorm 响应消息 metadata 含 `task_id=3612a5ea…`，但
`analysis_tasks` 中该行不存在。

## 目标

- 思考持久化失败（含死锁）**不得破坏请求事务的一致性**：响应里的 task_id、消息必须
  与数据库真实状态一致。
- 降低死锁发生概率与影响面。
- 前端对「任务不存在」（events 404）停止无限重试并复位运行态。

### 已确认的边界

- 保持「思考持久化尽力而为」语义：持久化失败不阻塞主流程、不改变响应状态码。
- 不改变 `safe_error` 白名单、思考流 SSE、任务状态机。
-  brainstorm/clarify/respond 三条请求内路径统一修复；executor worker 路径（agent loop
  finally）已用独立事务，不在本次范围。
- 锁范围收窄（整会话 FOR UPDATE → turn 级）**不做**：MySQL 无索引 JSON 查询同样
  产生大范围 next-key 锁，收益不确定；以「移出请求事务 + 短事务 + 死锁重试」为主修复，
  锁收窄留作后续观察项。

## 设计

### 1. brainstorm 成功路径：思考持久化移到 commit 后独立事务

- `BrainstormService.respond` 移除 `_persist_completed_blocks` 与
  `attach_turn_to_assistant` 调用（`backend/app/brainstorm/service.py:131-135, 165-177`），
  请求事务只负责业务写入（消息、画像、任务）。
- `brainstorm/router.py` 在 `await db.commit()` 之后调用新 helper
  `persist_brainstorm_thinking(session_factory, *, user_id, session_id, turn_id,
  assistant_message_id)`（`app/thinking/persistence.py` 新增）：
  - 独立 `session_factory.begin()` 事务；
  - 内部：`completed_blocks(only_unpersisted=True)` → `persist_block` 逐块 →
    `mark_blocks_persisted` → `attach_turn_to_assistant`（按 message id 取 assistant 行）；
  - **死锁/OperationalError 重试一次**（1213 是瞬时错误，MySQL 官方建议重试）；
  - 全部异常只记 warning，不影响已提交的响应。
- 响应 DTO 不再携带本轮 thinking metadata（持久化在响应后发生）；实时展示由会话级
  thinking SSE 覆盖，刷新后从消息 metadata 恢复——行为差异可接受。

### 2. tasks 路由 clarify/respond 路径：同样移到 commit 后

- `tasks/router.py` 的 clarify 分支（:369-395）与 respond 分支（:421-447）中的
  `_persist_turn_blocks` + `attach_turn_to_assistant` 移到各自 `await db.commit()` 之后，
  复用第 1 点的独立事务 helper（泛化命名 `persist_turn_thinking`，brainstorm/tasks 共用）。
- `bind_turn`（纯内存操作，无 DB 锁）留在请求内，提前到 planner 之后即可（现状如此，不动）。

### 3. 死锁重试

- `persist_turn_thinking` 捕获 `sqlalchemy.exc.OperationalError` 且 mysql errno 为 1213
  时，用新事务重试一次；再失败记 warning 放弃（尽力而为语义不变）。

### 4. 前端：events 404 停止重试并复位

- `src/api/taskStream.ts`：`SSE_404` 错误与传输错误区分——抛出即带状态码（现状
  `SSE_${status}` 已带，hook 端解析即可，无需改 api 层）。
- `src/hooks/useTaskStream.ts`：catch 到 `SSE_404` 时**跳出重连循环**（404 是永久态，
  任务不存在或无权访问），`connection` 置 `'closed'`，并在 runtime 上标记
  `notFound: true`（`TaskRuntimeState` 加可选字段）。
- `src/hooks/useWorkspace.ts`：effect 监听 `currentTaskRuntime?.notFound` →
  `setActiveTaskId(undefined)`，运行态复位（`isAnalyzing` 变 false）。

## 测试策略

- 后端：
  1. brainstorm ready → 200 且响应 task_id 在 `analysis_tasks` **真实存在**（幻影任务回归）。
  2. monkeypatch `persist_block` 抛 `OperationalError(1213)` → brainstorm 仍 200、任务
     存在、重试被触发（断言调用次数 ≥2）。
  3. monkeypatch 持久化 helper 整体抛错 → brainstorm/clarify/respond 响应不受影响。
  4. 既有 thinking/brainstorm/tasks 测试全绿。
- 前端：
  5. useTaskStream：SSE_404 后不再重连、connection=closed、notFound=true；SSE_500 仍重连。
  6. useWorkspace：notFound 时 activeTaskId 被清空。

## 遗留事项

- `_messages` 整会话 `FOR UPDATE` 锁范围问题留作观察项；若死锁仍频发再评估收窄。
- executor worker 路径的思考持久化已是独立事务，但同样可能死锁——失败后 block 仅
  留存内存（进程内），属既有尽力而为语义。
