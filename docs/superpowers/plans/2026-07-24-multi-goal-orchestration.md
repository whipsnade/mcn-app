# 阶段四：复合任务（多 Goal 编排）实施计划

设计依据：`docs/superpowers/specs/2026-07-23-multi-intent-task-artifacts-design.md` §8 复合任务、§14 状态、§15 错误处理与恢复、§19 阶段四。
范围：**只做阶段四**。前端无改动（goal.* 事件已存在且被安全忽略；artifact.updated 已驱动三 Tab）。无数据库迁移（task_goals 的 depends_on_goal_id/result_summary_json/warning_code/trajectory_json 均已在 0022 就位）。

## 关键事实（已核实）

- `executor._run_agent_loop`（executor.py:295-580）当前是单 Goal 循环：`load_task_goal`（sequence=1）→ 建一次 context → 轨迹存 `task.plan_json`（v1：`{"schema":"agent_trajectory_v1","steps","results"}`，step id 为 `step_N`）→ 三种收尾分支（余额不足/零证据/成功）各调 `write_conclusion_message` + `auto_kol_analysis` + `_finalize_goal` + `mark_*`。
- `_PlanArguments.load_arguments`（dependencies.py:147-154）按 `task.plan_json["steps"]` 查参数——崩溃重放的字节级参数来源，多 Goal 必须适配。
- `logical_call_id = uuid5(task.id:step_id)`——多 Goal 下 step id 必须带 goal 命名空间防碰撞。
- `_TaskArtifacts.finalize_goal`（T4）已按 goal_type 分派 kol/brand/campaign 收尾，但内部按 sequence=1 取 goal——需改为按 goal_id。
- enforce 建任务（T1）当前只建 sequence=1 的 goal，丢弃其余记 warning。
- `auto_kol_analysis` 对非 kol goal 天然空转（无 set items 跳过）；`write_conclusion_message` 按 task_id 写 assistant 消息，可按 goal 多次调用。
- planner 语义校验（goals/validation.py）已保证 sequence 连续、类型不重复、依赖前驱；未限制依赖组合。
- retry 路径（TaskService.retry → create）当前默认建 kol_selection goal——重试 brand/campaign 任务会得到错误 goal 类型。

## 总体策略

- **顺序编排，绝不并发**（设计 §8.2）：executor 按 sequence 逐个执行 Goal，终态 Goal 跳过（恢复语义），从第一个非终态 Goal 继续。
- **轨迹 v2**：`task.plan_json` 改为 `{"schema":"agent_trajectory_v2","goals":{goal_id: {steps, results}}}`；step id 命名空间 `g{sequence}_step_{N}`（logical_call_id 天然唯一）；v1 旧任务按原逻辑恢复（legacy 单 Goal 路径零改动）。
- **软依赖**（§8.3）：上游 completed/completed_with_warnings → 下游 context 注入其 `result_summary_json`；上游 failed/skipped/insufficient_balance → 下游仍执行并记 `warning_code="dependency_missing"`；仅当依赖组合非法或下游缺强制参数（kol_selection 无 brand）才 `skipped`。
- **摘要传递**：上游 Goal 成功收尾后由模型生成精简摘要（零积分，一次 `complete_json`），落 `goal.result_summary_json`；下游经 `AgentLoopContext.dependency_summaries` 注入 user JSON。

---

## Task 1：planner 多 Goal 落库 + 依赖组合校验 + artifact_summaries

- `goals/validation.py`：新增依赖组合白名单校验——`depends_on` 仅允许 `brand_analysis→kol_selection`、`campaign_analysis→kol_selection`（即只有 kol_selection 可以有依赖），违反抛 `GoalPlanSemanticError("dependency_combination_not_allowed")`；既有校验不变。
- `TaskService.create`：goal 参数从单个改为列表（`goals: list[GoalSpec-like]`），按 sequence 建多行 TaskGoal，`depends_on_sequence` 解析为 `depends_on_goal_id`（同事务内先建前行再填后行）；默认仍是单 kol_selection（brainstorm 内联/旧调用方不变）。
- `tasks/router.py` enforce 路径：planner `execute` 输出 1-3 个 goal 时全部落库（替代「丢弃其余记 warning」）。
- `goals/context.py`：`artifact_summaries` 从空占位改为真实数据——查该会话 task_artifacts 每 module_key 最新 completed 一条（title/artifact_type/version/scope/created_at，截断紧凑），注入 planner payload（设计 §6.1）。
- 测试：依赖组合校验（合法两例/非法两例）、多 Goal 落库（3 行、依赖 id 解析正确）、enforce 建多 goal、artifact_summaries 注入、单 goal 路径回归。

## Task 2：Goal 摘要生成器

- `backend/app/model/prompts.py`：新增 `GOAL_SUMMARY_PROMPT`（`goal_summary_v1`）：输入 goal 类型 + scope + 证据摘录，输出紧凑 JSON `{summary: str≤600, highlights: {platforms?, content_types?, audience?, kol_traits?, risks?}}`（设计 §8.1 的字段集合，按 goal 类型裁剪）；`ModelPurpose` 加 `"goal_summary"`；注册 + test_prompts 契约。
- 新建 `backend/app/goals/summary.py`：`build_goal_result_summary(db, model, *, task, goal, report=None, selection_set=None) -> dict`：证据取自该 goal 的 trajectory results（v2 切片）→ 模型摘要 → 返回 `{"summary": ..., "highlights": ..., "artifact": {type, id, version}}`；模型失败回退纯代码摘要（工具名+行数统计），绝不阻塞编排。
- 测试：模型路径、回退路径、字段裁剪。

## Task 3：executor 多 Goal 编排

- 仓储：`get_task_goals(task_id)`（按 sequence 全量）、`save_goal_result_summary(goal_id, summary)`、`mark_goal_terminal(goal_id, status, warning_code, error_code)`（finalize 现有逻辑适配按 goal_id）。
- `_TaskArtifacts.finalize_goal` 改为按 goal_id 定位（内部查询条件从 sequence=1 改 goal_id 入参）；kol set 查询已按 goal_id，无影响。
- `AgentLoopContext` 加 `dependency_summaries: tuple[dict, ...] = ()`（进 user JSON）。
- executor 重构（核心）：
  - `_run_agent_loop`：加载全部 goals；无 goals → legacy 路径（现有代码原样保留）。有 goals → 顺序循环：跳过终态 goal；`_start_goal` → 组装上游摘要（依赖 goal 成功取其 result_summary_json 注入；失败记 dependency_missing warning）→ 调用抽取出的 `_run_goal_loop(task, goal, policy, dependency_summaries) -> GoalOutcome`（现有 while 循环整体平移，轨迹读写走 v2 的 goals[goal_id] 切片，step id 用 `g{seq}_step_{N}`）→ 收尾：`write_conclusion_message`（该 goal 的 finish 结论）→ kol goal 调 `auto_kol_analysis` → `finalize_goal(goal_id, ...)` → 成功则 `build_goal_result_summary` 落库。
  - 软依赖门控：下游 kol_selection 的 params.brand 为空且上游失败 → `skipped`（warning_code=dependency_missing_brand），不执行循环。
  - 余额不足：当前 goal 标 insufficient_balance，其余 goal 保持 pending，任务 mark_insufficient_balance，编排停止。
  - 取消：当前 goal + 全部 pending goal 标 skipped，任务 mark_cancelled。
  - 任务终态聚合（§14.2）：全部 completed → `mark_completed`；有成功有失败/warning → `mark_completed_with_warnings`；全部 failed → `mark_failed("all_goals_failed")`；每个 goal 的 `goal.completed/failed` 事件照发。
- `_PlanArguments.load_arguments`：v2 时按 `plan_step_id`（`g{S}_step_N`）定位 goals 切片查参数；v1 逻辑保留。
- 测试（tests/tasks/test_multi_goal.py 为主）：
  - 两 Goal 顺序执行（campaign → kol_selection）：事件序列 goal.started×2、goal_id 贯穿工具事件与 mcp_calls、各自 artifact 登记、任务 completed
  - 摘要注入：下游 context 的 dependency_summaries 含上游 summary
  - 软依赖：上游 failed 下游仍执行 + warning_code=dependency_missing；下游缺 brand → skipped
  - 余额不足：goal2 保持 pending、任务 insufficient_balance、恢复后从 goal2 继续且不重复扣费（logical_call_id 幂等）
  - 恢复：goal1 终态不重跑、goal2 从轨迹断点续跑
  - 终态聚合三态；legacy 单 goal 与 v1 恢复回归全绿

## Task 4：retry 复制 goal 结构

- `TaskService.retry`：新任务复制原任务的 goal 列表（goal_type/sequence/depends_on 结构/params_json），不重新规划（planner 只在首次创建时跑）。
- 测试：retry 多 goal 任务得到同构 goal 集合、新 goal id、状态 pending。

## Task 5：验证与文档

- `ruff check app tests` + `pytest -q` 全量；前端 `npm run test/lint/build` 兜底（无改动预期）。
- AGENTS.md 阶段四小节；changelog/2026-07-24.md 追加。

## 不做（明确排除）

- 前端多 Goal 进度 UI（goal 事件已在 SSE，TaskFlowNodes 暂不展示 goal 维度）；并发 Goal；Goal 间循环反馈；阶段五的兼容收敛（停双写/停双发）；`artifact_summaries` 之外的新 planner 输入。

## 风险点

- executor 重构是本阶段最大改动面：legacy/v1 路径必须逐字节保持——现有 test_agent_loop.py + test_goal_lifecycle.py 全绿为硬门槛。
- 轨迹 v2 与 `_PlanArguments`、恢复 reconcile 的参数重放强耦合，step id 命名空间解析要有单测覆盖（含 v1 旧 step id）。
- 摘要模型调用增加每个 goal 边界 1 次延迟（几秒），失败回退代码摘要兜底。
