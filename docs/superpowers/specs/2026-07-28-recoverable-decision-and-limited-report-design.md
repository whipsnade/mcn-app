# 模型决策可恢复 + 品牌分析最低可交付 设计

日期：2026-07-28
状态：按用户提供的修复计划制定，已核实事故现场

## 背景与事故核实

会话 `55e5c955`（星巴克声量情感分析）的 brand_analysis Goal 失败，用户提供了修复
计划。调查核实（plan_json / model_prompt_logs / task_events / mcp_calls）：

- 前两步已成功：`match.best.tag`（品牌标签）与 `social.statistic.overview`（声量
  151127 等概览数据）均 settled。
- 第 3 轮模型决策：思考文本明确选择 `datatap.insight.social.statistic.trend.v1`，
  但决策 JSON 把 `internal_tool_name` **嵌进了 `arguments` 对象内部**（顶层为 null）
  → `resolve_agent_call` 抛 `AGENT_TOOL_MISSING`；回喂文案笼统（「未通过校验」），
  模型无修正线索原样重犯；`invalid_streak >= 2` 熔断，Goal 失败，已采集证据全部
  丢弃（现有架构没有「goal 失败但有部分证据时降级生成报告」的路径）。
- trend 工具已审核启用（registry.py:73），「工具不允许」不成立，纯粹是决策结构错位。
- 日期问题独立存在：brainstorm/GoalPlanner 的 prompt 与 payload **均无当前日期
  锚点**，模型把「最近 1 个月」折算为 2025-12-01~2026-01-01（真实应为
  2026-06-27~2026-07-27）；`param_profile_period_override` 只校验格式不校验合理性，
  无条件覆写 `requested_period`。

## 修复方案（按用户计划，含核实后的细化）

### 1. 「模型漏填工具名」改为可恢复决策错误

`backend/app/orchestration/loop.py` 的 `resolve_agent_call` 拆分错误码：

- `AGENT_DECISION_MISSING_TOOL_NAME`：action=call_tool 但顶层缺 `internal_tool_name`
  （含本案「误嵌进 arguments」的形态）；
- `TOOL_NOT_ALLOWED`：填了工具名但不在当前可用集合（`loop.py:261-263` 现状即此码，
  保持）。

`backend/app/tasks/executor.py` 的回喂与熔断（`_run_goal_loop:690-707`）：

- `AGENT_DECISION_MISSING_TOOL_NAME` **不终止 Goal、不调用 MCP、不扣积分**，
  回喂一条**结构化错误**（EvidenceNote）：明确本轮未执行、缺少顶层
  `internal_tool_name` 字段（若检测到误嵌于 arguments 内，点名指出该错位）、
  附当前允许的工具名清单（紧凑投影）与本 goal 的目标提示——本案语境下即
  「已拿到 overview，若要判断变化趋势，应明确选择并填写
  `datatap.insight.social.statistic.trend.v1`」。
- **连续修正上限 2 次**：`missing_tool_name_streak` 独立计数——仅该错误发生时 +1、
  任何决策校验成功时清零，与 `invalid_streak` 互不影响（混发时各自计数）。
  第 3 次仍错进入 §2 受限交付，不再 raise。
- 其余 `PlanValidationError`（TOOL_NOT_ALLOWED / INVALID_TOOL_ARGUMENTS / 渠道）
  维持现状：第 2 次熔断。
- 回喂 EvidenceNote 格式：一行错因 + 「允许的工具：internal_name（一行用途）×N」
  （N=`context.tools` 实际长度，品牌场景约十余个）+ 一句本 goal 目标提示。

### 2. 连续修正失败时生成「受限报告」

**范围限定：仅「缺工具名修正失败（连续修正上限耗尽）」这一条路径**触发受限交付；
其他异常（ModelAdapterError、MCP 失败等）维持现状失败路径，不降级。

- Goal 终态 `completed_with_warnings`，警告码落 `goal.warning_code` 列
  （`brand_trend_data_unavailable`；`error_code` 列只用于失败码，不写）。
- executor 在修正上限耗尽时：先写 `goal.warning_code` 再走既有
  `_finalize_analysis_goal` 报告构建，并把警告码透传给它——构建器按**固定映射**
  （`brand_trend_data_unavailable` → 「趋势数据未成功获取」）在报告 prompt 中注入
  **受限声明**：「已完成品牌概览与情感快照；趋势工具未成功执行，不输出跨期趋势
  结论」，禁止根据 overview 的环比字段伪造完整趋势分析。
- 产物登记 `brand_report` artifact（completed）。
- 任务级终态：走既有 `completed_with_warnings` 路径，reason 从硬编码
  `"mcp_partial_failure"` 扩为按场景取值——本场景用新 reason
  `"decision_recovery_exhausted"`，用户可见消息为
  「部分数据未能获取，已基于已采集数据生成报告。」（SSE 终态事件同构）。
- **零有效证据时才保持 Goal 失败**（现状路径）。

### 3. 强化 brand_loop prompt 与结构化契约

`brand_loop_v1`（`backend/app/model/prompts.py`）：

- 明确：`internal_tool_name` 为 call_tool 的**顶层必填字段**（禁止嵌进 arguments）；
  只有能给出完整工具名时才输出 call_tool；不确定时 finish 并说明证据不足，不得
  输出空工具调用。
- 按阶段排序的工具提示：① 品牌标签匹配 → ② 概览 → ③ 趋势 → ④ 可选话题/受众。
- 循环上下文每轮注入**简短状态**（本 spec 新增工作）：已调用工具清单 +
  剩余证据缺口（扩展 `AgentLoopContext`/user payload，降低模型遗漏字段概率）。

### 4. 相对时间锚点修复（用户要求单列）

- brainstorm 的 user content（`brainstorm/service.py`）与 GoalPlanner 的 payload
  （`goals/planner.py`）注入 `current_date`（`date.today().isoformat()`）；
  两个 prompt 各加一行：「相对时间（最近 N 天/个月）一律以 current_date 为基准折算」。
- `param_profile_period_override`（`tasks/dependencies.py:94-111`）增加合理性校验：
  `end > today`（未来窗口必然错误）或 `end < today - 400 天`（约 13 个月前，
  超出「最近」类表达的合理范围）时**拒绝覆写**（记 warning 并按无覆写处理，回退
  代码解析的 requested_period）。采用「拒绝」而非 loop 内的「钳制」语义：覆写来源
  是模型生成的画像，错误的绝对窗口应整体弃用而非截成更隐蔽的错误；400 天阈值
  保住活动复盘（如去年双 11）等合法旧窗口。

### 不做的事（YAGNI）

- 不自动把误嵌的 `arguments.internal_tool_name` 提升到顶层执行（按用户方案走
  回喂修正，不做静默改写）。
- 不改 TOOL_NOT_ALLOWED / INVALID_TOOL_ARGUMENTS 的现有熔断语义。
- 不改 trend 工具注册与审核流程。

## 测试策略

- loop：`AGENT_DECISION_MISSING_TOOL_NAME` 与 `TOOL_NOT_ALLOWED` 拆分（缺顶层名 /
  误嵌 arguments / 名字不在集合三种形态）。
- executor：漏名决策回喂内容（含错位提示 + 允许工具清单 + goal 目标）、不调 MCP
  不扣积分、连续 2 次后进入受限交付而非 raise；其他 PlanValidationError 熔断不变。
- 受限报告：有 settled 证据 → completed_with_warnings + 警告码 + 报告含受限声明 +
  brand_report artifact；零证据 → 仍失败。
- 日期：brainstorm/planner payload 含 current_date；period 覆写拒绝未来窗口与
  370 天前的窗口、接受合法近期窗口。

## 遗留事项

- 模型是否会读回喂并成功修正，取决于 MiniMax 遵循度，需实测观察（model_prompt_logs）。
- 受限报告的声明文案质量需抽样评审。
