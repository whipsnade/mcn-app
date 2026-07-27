# brainstorm 锁窗口收窄（两段式 respond）设计

日期：2026-07-27
状态：已确认（用户批准修复）

## 背景与事故

当日第 4 起同类事故：brainstorm POST 返回 500。死锁现场（MySQL 1213）：

- brainstorm 请求事务开始即 `get_owned_session(for_update=True)` 锁会话行，随后
  `append_message` 插入用户消息（意图锁），**然后拿着这些锁跑 30-120 秒的模型调用**
  （brainstorm 澄清 + enforce GoalPlanner 两次调用），最后才写 assistant 消息。
- 锁窗口横跨整个模型调用期间，任何并发写者（上一轮 commit 后的
  `persist_turn_thinking` 整会话 `FOR UPDATE` 扫描、`record_brainstorm_failure`、
  用户重试的另一个 brainstorm、任务执行器的思考持久化）都必然争抢同一批
  messages 行 → 死锁只是概率问题。此前两次修复（思考持久化移出请求事务、失败落库
  重试）只治外围，未动这个长锁窗口。

## 目标

把模型调用移出锁窗口：`respond()` 改两段式——无锁读阶段（模型调用）+ 短事务写阶段，
锁窗口从分钟级缩到毫秒级。

### 已确认决策

- 只改 brainstorm 路径。tasks 路由 enforce 规划在写操作之前、事务内只有普通 SELECT
  （无 FOR UPDATE），不构成同款长锁窗口，不在本次范围。
- 失败语义不变：模型失败 → router rollback → `record_brainstorm_failure`（已具备
  「用户消息不存在则创建」逻辑）落用户+错误消息。
- 写阶段不重试 1213（毫秒级窗口，先观察；`record_brainstorm_failure` 已有重试）。

## 设计（`backend/app/brainstorm/service.py` 的 `respond`）

### 读阶段（无锁、无写）

1. `get_owned_session(user_id, session_id, for_update=False)`（普通读）。
2. 读画像（filters_snapshot）、`list_messages` + `compress_messages`，**把当前用户消息
   手动拼到压缩列表尾部**（此前靠先落库再 list；镜像 GoalPlannerContextBuilder 做法）。
3. `bind_turn(turn_id, user_id, session_id, task_id=None, trigger_message_id=None)`
   （内存绑定，提前到模型调用前；trigger 待写阶段补绑）。
4. `_complete` brainstorm 模型调用；ready 且 enforce 开启时 `_plan_task_goals`
   （planner 模型调用）——全程无任何行锁、无任何写。

### 写阶段（短事务，FOR UPDATE）

5. 重新 `get_owned_session(for_update=True)`，**重读画像**（并发下可能已被另一请求
   推进），以最新画像为 base 做 `merge_profile`。
6. 落用户消息（sequence 取 max+1）、更新 workspace（profile/title/标量列）、
   ready 时 `TaskService.create(goal_specs)`、落 assistant 消息、flush；
   `bind_turn` 补绑 `trigger_message_id=user_message.id`（bind_turn 幂等更新绑定）。
7. 路由不变：commit → `persist_turn_thinking` → `task_runner.submit`。

### 并发语义

两个并发 brainstorm：读阶段完全并行；写阶段在会话行锁上短暂排队，后到者基于
**重读的最新画像** merge（platforms 并集、标量后写胜），语义与现状一致。
模型失败发生在读阶段：请求事务无任何写入，router rollback 后由
`record_brainstorm_failure` 独立落用户+错误消息（该函数已处理用户消息不存在的情况）。

### 不做的事（YAGNI）

- 不动 tasks 路由、executor、quick 路径。
- 写阶段不加 1213 重试（窗口毫秒级；复发再议）。
- 不改 merge_profile 语义、不改响应 DTO。

## 测试策略

- 既有 brainstorm 测试（59 个）全绿——成功/ready/失败/多轮/标题路径行为不变
  （这是最强的回归网）。
- 新增：模型失败时请求事务零写入（断言失败落库由 record_brainstorm_failure 独立
  完成且用户消息只有一条）；ready 路径写阶段重读画像（并发推进画像时以最新为准，
  可用先写一条 profile 再调 respond 的方式模拟）。
- thinking 绑定：写阶段补绑 trigger_message_id 后，持久化块正确挂到 assistant 消息
  （既有恢复断言覆盖）。

## 遗留事项

- 思考持久化的整会话 `FOR UPDATE` 扫描（`_messages`）仍是锁放大器，锁窗口收窄后
  碰撞概率已很低；如复发再评估 turn 级收窄。
- tasks 路由 enforce 规划若未来引入前置 FOR UPDATE 写，需按同原则两段式。
