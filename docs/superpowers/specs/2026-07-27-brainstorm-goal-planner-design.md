# brainstorm ready 内联建任务接入 GoalPlanner 设计

日期：2026-07-27
状态：已确认（用户要求修复）

## 背景与事故

用户在新会话发送「分析蓉李记最近1个月在各平台的声量变化和用户情感趋势」（明确的品牌
分析需求），最终任务却按 kol_selection（圈选达人）执行。诊断（`model_prompt_logs` +
`task_goals` 实证）：

- GoalPlanner enforce 只在 `POST /sessions/{id}/tasks` 路由运行；
- brainstorm 澄清流程 ready 后，`brainstorm/service.py` **内联创建任务且固定
  `goal_type="kol_selection"`**（`TaskService.create` 不传 goal_specs），GoalPlanner
  从未被调用（日志无任何 planner 记录）；
- 因此**凡经 brainstorm 澄清自动创建的任务永远按圈选执行**，品牌/活动分析需求被
  错误执行为圈选达人。这是 brainstorm（先于 GoalPlanner 存在）遗留的设计缺口。

## 目标

brainstorm ready 内联建任务时同样经 GoalPlanner 规划：execute 按规划 goals 建任务；
clarify/失败回退现状（默认 kol_selection 单 goal）。

### 已确认决策

- planner 输出 clarify 时**不**再进入澄清（画像刚确认 ready，再澄清体验怪异），
  回退默认 kol_selection 兜底。
- 仅 `GOAL_PLANNER_ENFORCE_ENABLED=true` 时启用；关闭时行为与现状完全一致。
- planner 规划调用复用同一 turn 的思考流（purpose=goal_planner，label「正在规划分析目标」）。

## 设计

### brainstorm service（`backend/app/brainstorm/service.py`）

ready 分支（`service.py:100-131`）改为：

1. **planner 上下文**：`GoalPlannerContextBuilder().build_for_message(user_id, session_id,
   payload.content, db=self.db)`——workspace 行在同一会话内，画像标量列虽已写内存未
   commit，build 读到的是最新值（同一事务）。
2. **规划调用**（新增私有方法 `_plan_task_goals`，镜像 `tasks/router.py` 的
   `_plan_goal_or_fallback` 的容错语义）：
   - `GoalPlannerService(model=self.model, context_builder=None).plan_context(context,
     thinking_sink=sink)`；sink 由 `self.thinking_service.create_sink` 创建
     （purpose=goal_planner、label=正在规划分析目标、同 turn_id），创建失败静默禁用；
   - 输出 execute 且 goals 非空 → 按 sequence 排序转 `goal_specs=[{goal_type, sequence,
     depends_on_sequence, params}]`（与 tasks/router enforce 同构），返回 goal_specs；
   - 输出 clarify、goals 为空、或任何异常（除 LookupError 上抛 404）→ 记 warning
     返回 None（回退）。
3. **建任务**：`TaskService.create(..., goal_specs=goal_specs)`；goal_specs 为 None 时
   与现状完全一致（默认 kol_selection 单 goal）。
4. `bind_turn`（task_id 回填）与其余流程不变。

### 开关

`get_settings().goal_planner_enforce_enabled` 为 false 时跳过规划（不构建上下文、不调
模型），行为零变化。

### 不做的事（YAGNI）

- 不改 tasks 路由现有 enforce 路径；不抽公共 helper 到第三处（两处同构 ~30 行，
  可接受；若未来出现第三调用点再抽）。
- clarify 不落消息（直接回退建任务，见已确认决策）。
- 不改 brainstorm 的画像/标题/写回逻辑。

## 测试策略

- ready + planner execute(brand_analysis) → 任务 goal 为 brand_analysis（本 bug 回归）。
- ready + planner execute 多 goal（brand_analysis → kol_selection 依赖）→ 多 goal 落库
  且依赖正确。
- ready + planner 输出 clarify → 回退 kol_selection 单 goal，brainstorm 200。
- ready + planner 抛异常 → 回退 kol_selection，brainstorm 200。
- enforce 关闭 → planner 未被调用，默认 kol_selection。
- 既有 brainstorm 测试全绿。

## 遗留事项

- planner 与 brainstorm 是两次独立模型调用（澄清 + 规划），ready 时延增加一次调用
  耗时；后续可评估合并。
- planner 输出 clarify 的回退意味着「规划认为信息仍不足」时按圈选执行，可能仍非用户
  意图；用 model_prompt_logs 观测该分支发生率后再优化。
