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
- **四条请求内路径统一修复**：brainstorm 成功路径、tasks 路由的 clarify 分支、respond
  分支、**execute 分支（`planner_attempted` 时的 `_persist_turn_blocks`，与 brainstorm
  事故同型：死锁回滚 → 202 幽灵 task_id + `task_runner.submit` 幻影提交）**。
  executor worker 路径（agent loop finally）已用独立事务，不在本次范围。
- 锁范围收窄（整会话 FOR UPDATE → turn 级）**不做**：MySQL 无索引 JSON 查询同样
  产生大范围 next-key 锁，收益不确定；以「移出请求事务 + 短事务 + 死锁重试」为主修复，
  锁收窄留作后续观察项。

## 设计

### 1. brainstorm 成功路径：思考持久化移到 commit 后独立事务

- `BrainstormService.respond` 移除 `_persist_completed_blocks` 与
  `attach_turn_to_assistant` 调用（`backend/app/brainstorm/service.py:131-135, 165-177`），
  请求事务只负责业务写入（消息、画像、任务）。
- `brainstorm/router.py` 在 `await db.commit()` 之后调用新 helper
  `persist_turn_thinking(session_factory, *, user_id, session_id, turn_id,
  assistant_message_id)`（`app/thinking/persistence.py` 新增，brainstorm/tasks 共用）：
  - 调用前先从 thinking service 取 `completed_blocks(only_unpersisted=True)` **并捕获列表**；
  - 独立 `session_factory.begin()` 事务：逐块 `persist_block` →
    `attach_turn_to_assistant`（`assistant_message_id` 为 None 时跳过 attach，
    供 execute 分支复用）；
  - **commit 成功后才 `mark_blocks_persisted`**（内存标记不得先于事务成功，
    否则死锁重试时 block 永久丢失）；
  - **死锁/OperationalError（mysql errno 1213）用捕获的同一 blocks 列表重试一次**
    （`_thinking_metadata` 按 (operation_id, attempt) 去重，重复 persist 安全）；
    再失败记 warning 放弃（尽力而为语义不变）。
- 响应 DTO 变化（明示，不算回归）：
  - brainstorm 响应 message 不再携带本轮 thinking metadata；且 `turn_id` 此前完全靠
    attach 补写，修复后响应 DTO 的 message 也没有 `turn_id`（前端对
    `response.message.turnId` 无直接依赖，chips 用 brainstorm/clarify options，思考
    展示走会话级 thinking SSE + 刷新恢复）。
  - clarify/respond 响应同样不再携带 thinking metadata（现状 attach 在 commit 前）。
    前端 `toMessage(response.message)` 并入 thinking 仅影响「本轮思考即时回放」，
    由会话级 thinking SSE 覆盖。

### 2. tasks 路由三分支：同样移到 commit 后

- `tasks/router.py` 的 clarify 分支（:369-395）、respond 分支（:421-447）与
  **execute 分支（`planner_attempted` 的 `_persist_turn_blocks`，:503-524）**中的
  `_persist_turn_blocks` + `attach_turn_to_assistant` 移到各自 `await db.commit()` 之后，
  统一调用第 1 点的 `persist_turn_thinking`（execute 分支无 assistant 消息，
  `assistant_message_id=None`；`task_runner.submit` 仍在 persist 之后按现状顺序）。
- `bind_turn`（纯内存操作，无 DB 锁）留在请求内，位置不动。

### 3. 死锁重试

- 见 §1 helper 语义：捕获 blocks 列表 → 独立事务 → commit 成功后 mark；
  1213 死锁用同一列表在新事务重试一次；再失败记 warning。

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
     存在、重试被触发（断言调用次数 ≥2）；重试复用同一 blocks 列表，commit 成功后才
     `mark_blocks_persisted`（首轮死锁时 mark 不被调用）。
  3. monkeypatch 持久化 helper 整体抛错 → brainstorm/clarify/respond/execute 响应不受影响，
     且 execute 分支任务真实落库、`task_runner.submit` 正常。
  4. 既有 thinking/brainstorm/tasks 测试全绿。
- 前端：
  5. useTaskStream：SSE_404 后不再重连、connection=closed、notFound=true；SSE_500 仍重连。
  6. useWorkspace：notFound 时 activeTaskId 被清空。

## 遗留事项

- `_messages` 整会话 `FOR UPDATE` 锁范围问题留作观察项；若死锁仍频发再评估收窄。
- executor worker 路径的思考持久化已是独立事务，但同样可能死锁——失败后 block 仅
  留存内存（进程内），属既有尽力而为语义。
- helper 部分失败终态：persist 成功、mark 已调、但 attach 重试后仍失败时，assistant
  行缺 `turn_id` 且 user 消息可能残留 `thinking_pending`（blocks 已在
  assistant.metadata.thinking 里，刷新恢复基本可用），属尽力而为可接受范围。
